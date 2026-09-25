"""Trap channel: a configured channel that punishes whoever posts in it.

A member (without ``manage_messages``) sending a message into the trap
channel has that message deleted immediately and receives the configured
consequence, never announced in the channel itself (the trap stays silent):

* ``none``: only the offending message is deleted;
* ``kick``: expelled (messages untouched);
* ``softban``: banned with the native ``delete_message_seconds`` purge and
  unbanned right away (fresh join, history gone server-side, instantly);
* ``ban``: banned with the same native purge window.

Ban/softban deletion uses Discord's own ``delete_message_seconds``
(hour/day/week window chosen in ``trap.delete_window``), never the /limpar
engine: it is server-side, instant and covers every channel. Staff
(``manage_messages``) and bots are exempt; the bot checks its own
permissions and role hierarchy before acting, logs one audit card
(``audit.trap_enabled``), and can DM the victim
(``trap.dm_enabled`` + theme-customizable ``trap.dm``).

The channel also carries a persistent notice card (``trap.notice``
CardSpec) explaining the trap to anyone opening it: posted on first setup
and repainted through the panels registry (theme or setting changes edit
it in place; disabling the trap retires it). A staff log card
(``trap.logs.failed``) fires whenever the configured consequence could not
be applied (missing permission, hierarchy, API error): the trap must never
fail silently.
"""

import contextlib
import logging
from pathlib import Path

import discord
from discord.ext import commands

from rosemary.core.cards import log_description
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting
from rosemary.core.storage import GuildStorage

log = logging.getLogger(__name__)

#: ``trap.delete_window`` choice -> native ``delete_message_seconds``.
DELETE_WINDOWS: dict[str, int] = {
    "none": 0,
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
}


class TrapStore:
    """Trap notice pointer in its own ``trap.json`` (per-guild)."""

    def __init__(self, data_dir: Path | str) -> None:
        self.storage = GuildStorage(
            Path(data_dir), filename="trap.json", use_defaults=False
        )

    async def get_notice(self, guild_id: int) -> int | None:
        doc = await self.storage.get(guild_id)
        notice = doc.get("notice_message_id")
        return int(notice) if notice else None

    async def set_notice(self, guild_id: int, message_id: int | None) -> None:
        await self.storage.set(guild_id, "notice_message_id", message_id)


class TrapCog(commands.Cog):
    """The honeypot channel: silent deletion plus a configured consequence."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = TrapStore(bot.storage.data_dir)
        self._started = False

    async def start(self) -> None:
        """Register the notice repaint once per process (boot repost)."""
        if self._started:
            return
        self._started = True
        from rosemary.core.panels import register as register_panel

        register_panel(
            "trap_notice",
            self.repaint_notice,
            setting_keys=("trap.enabled", "trap.channel"),
        )

    async def enabled(self, guild_id: int) -> bool:
        return bool(await get_setting(self.bot.storage, guild_id, "trap.enabled"))

    async def trap_channel(self, guild: discord.Guild):
        channel_id = await get_setting(self.bot.storage, guild.id, "trap.channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        try:
            if not await self.enabled(message.guild.id):
                return
            channel_id = await get_setting(
                self.bot.storage, message.guild.id, "trap.channel"
            )
            if not channel_id or message.channel.id != channel_id:
                return
            member = message.author
            if not isinstance(member, discord.Member):
                return
            if member.guild_permissions.manage_messages:
                return
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await message.delete()
            await self._punish(message.guild, member)
        except Exception:
            log.exception("trap channel check failed")

    #: notice card ==========================================================

    async def _notice_variables(self, guild: discord.Guild) -> dict:
        action = await get_setting(self.bot.storage, guild.id, "trap.action")
        action_text = await self.bot.translator.t(
            guild.id, f"trap.notice.action.{action}"
        )
        return {"server": guild.name, "action": action_text}

    async def _fallback_notice(self, guild: discord.Guild):
        """Code-built notice card (no theme override / builder failure)."""
        from rosemary.core.card_service import CardPayload
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        t = self.bot.translator.t
        theme = self.bot.theme
        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                theme.color("danger"),
                TextDisplay(
                    theme.md("title", title=await t(guild.id, "card.trap.notice.title"))
                ),
                TextDisplay(
                    await t(guild.id, "trap.notice.text", **await self._notice_variables(guild))
                ),
            )
        )
        return CardPayload(view=view)

    async def post_notice(self, guild: discord.Guild) -> None:
        """Post the notice card, retiring any earlier notice message."""
        from rosemary.core.card_service import render_card_message, send_card

        channel = await self.trap_channel(guild)
        if channel is None:
            return
        payload, allowed = await render_card_message(
            self.bot, guild.id, "trap.notice", await self._notice_variables(guild)
        )
        if payload is None:
            payload = await self._fallback_notice(guild)
            allowed = discord.AllowedMentions.none()
        try:
            message = await send_card(channel, payload, allowed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("trap notice post failed in %s: %s", guild.id, exc)
            return
        stale = await self.store.get_notice(guild.id)
        if stale and stale != message.id:
            with contextlib.suppress(discord.NotFound, discord.HTTPException):
                await channel.get_partial_message(stale).delete()
        await self.store.set_notice(guild.id, message.id)

    async def retire_notice(self, guild: discord.Guild) -> None:
        """Delete the notice card when the trap is disabled."""
        channel = await self.trap_channel(guild)
        notice = await self.store.get_notice(guild.id)
        await self.store.set_notice(guild.id, None)
        if channel is None or notice is None:
            return
        with contextlib.suppress(discord.NotFound, discord.HTTPException):
            await channel.get_partial_message(notice).delete()

    async def repaint_notice(self, bot, guild_id: int) -> None:
        """Panels-registry callback for the trap notice card.

        Enabled: posts when the notice is missing (first setup) or edits it
        in place when the stored message still exists. Disabled: retires the
        card and clears the pointer. (A channel change leaves the old
        channel's notice behind: the bot may lack message management there,
        and the pointer now targets the new channel.)
        """
        if not await self.enabled(guild_id):
            guild = bot.get_guild(guild_id)
            if guild is not None:
                await self.retire_notice(guild)
            return
        guild = bot.get_guild(guild_id)
        if guild is None:
            return
        channel = await self.trap_channel(guild)
        if channel is None:
            return
        notice_id = await self.store.get_notice(guild.id)
        if notice_id is not None:
            try:
                message = await channel.fetch_message(notice_id)
            except discord.NotFound:
                message = None
            except discord.HTTPException:
                return  # transient: retry on the next fan-out
            if message is not None:
                from rosemary.core.card_service import render_card_message

                payload, _allowed = await render_card_message(
                    self.bot, guild.id, "trap.notice", await self._notice_variables(guild)
                )
                if payload is None:
                    payload = await self._fallback_notice(guild)
                try:
                    kwargs = payload.message_kwargs()
                    kwargs["attachments"] = []
                    await message.edit(**kwargs)
                    return
                except (discord.Forbidden, discord.HTTPException):
                    pass  # fall through to a fresh post
        await self.post_notice(guild)

    #: consequence ==========================================================

    async def _punish(self, guild: discord.Guild, member: discord.Member) -> tuple[bool, str]:
        """Apply the consequence; returns ``(acted, failure_key)``."""
        guild_id = guild.id
        action = await get_setting(self.bot.storage, guild_id, "trap.action")
        window = await get_setting(self.bot.storage, guild_id, "trap.delete_window")
        seconds = DELETE_WINDOWS.get(str(window), 0)
        bot_permissions = guild.me.guild_permissions
        reason = await self.bot.translator.t(guild_id, "trap.reason")
        t = self.bot.translator.t

        acted = False
        failure = ""
        if action == "ban":
            if bot_permissions.ban_members:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await guild.ban(
                        member,
                        reason=reason,
                        delete_message_seconds=seconds or None,
                    )
                    acted = True
            if not acted:
                failure = await t(guild_id, "trap.failure.no_ban")
        elif action == "softban":
            if not bot_permissions.ban_members or member.top_role >= guild.me.top_role:
                failure = await t(guild_id, "trap.failure.no_ban")
            else:
                banned = False
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await guild.ban(
                        member,
                        reason=reason,
                        delete_message_seconds=seconds or None,
                    )
                    banned = True
                if banned:
                    if await self._unban(guild, member.id):
                        acted = True
                    else:
                        failure = await t(guild_id, "trap.failure.no_unban")
                else:
                    failure = await t(guild_id, "trap.failure.no_ban")
        elif action == "kick":
            if not bot_permissions.kick_members:
                failure = await t(guild_id, "trap.failure.no_kick")
            elif member.top_role >= guild.me.top_role:
                failure = await t(guild_id, "trap.failure.no_hierarchy")
            else:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await member.kick(reason=reason)
                    acted = True
                if not acted:
                    failure = await t(guild_id, "trap.failure.no_kick")

        if acted:
            with contextlib.suppress(Exception):
                await self._dm_victim(guild_id, member, action)
        await self._log_trap(guild, member, action, acted, failure)
        return acted, failure

    async def _unban(self, guild: discord.Guild, user_id: int) -> bool:
        """Unban right after a softban ban; ``False`` when it failed."""
        try:
            user = discord.Object(id=user_id)
            await guild.unban(user, reason="Softban: trap channel")
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Softban unban failed in %s: %s", guild.id, exc)
            return False

    async def _dm_victim(self, guild_id: int, member: discord.Member, action: str) -> None:
        """Best-effort DM explaining the trap, gated by ``trap.dm_enabled``."""
        if not await get_setting(self.bot.storage, guild_id, "trap.dm_enabled"):
            return
        from rosemary.core.cards import text_or

        common = {
            "server": member.guild.name,
            "action": await self.bot.translator.t(guild_id, f"trap.dm.action.{action}"),
        }
        translated = await self.bot.translator.t(guild_id, "trap.dm.text", **common)
        text = await text_or(self.bot, guild_id, "trap.dm.text", translated, **common)
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await member.send(text)

    async def _log_trap(
        self, guild: discord.Guild, member: discord.Member, action: str, acted: bool,
        failure: str = "",
    ) -> None:
        """One audit card (``audit.trap``) plus a staff log on failure."""
        from rosemary.cogs.audit import AuditCog, _variables

        audit = self.bot.get_cog("AuditCog")
        if isinstance(audit, AuditCog):
            try:
                channel = await audit._channel(guild.id)
                if channel is not None and await audit._on(guild.id, "audit.trap_enabled"):
                    action_label = await self.bot.translator.t(
                        guild.id, f"trap.audit.action.{action}"
                    )
                    outcome = await self.bot.translator.t(
                        guild.id, "trap.audit.applied" if acted else "trap.audit.skipped"
                    )
                    trap_channel = await self.trap_channel(guild)
                    await audit._send(
                        channel,
                        guild.id,
                        "trap",
                        {
                            **_variables(member, guild),
                            "channel": getattr(trap_channel, "mention", ""),
                            "action": action_label,
                            "outcome": outcome,
                        },
                    )
            except Exception:
                log.exception("trap audit card failed")
        if failure:
            with contextlib.suppress(Exception):
                await self._log_failure(guild, member, action, failure)

    async def _log_failure(
        self, guild: discord.Guild, member: discord.Member, action: str, failure: str
    ) -> None:
        """Staff log card: the trap punished nobody and staff must know."""
        guild_id = guild.id
        t = self.bot.translator.t
        await send_channel_log(
            self.bot,
            guild_id,
            await t(guild_id, "trap.logs.failed.title"),
            await log_description(
                self.bot,
                guild_id,
                "trap.logs.failed.description",
                user=member.mention,
                action=await t(guild_id, f"trap.audit.action.{action}"),
                reason=failure,
                user_label=await t(guild_id, "trap.audit.user_label"),
                action_label=await t(guild_id, "trap.audit.action_label"),
                reason_label=await t(guild_id, "trap.audit.reason_label"),
            ),
            color="danger",
            card_key="trap.logs.failed.description",
            mention_user_ids=[member.id],
        )
