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
"""

import contextlib
import logging

import discord
from discord.ext import commands

from rosemary.core.settings import get_setting

log = logging.getLogger(__name__)

#: ``trap.delete_window`` choice -> native ``delete_message_seconds``.
DELETE_WINDOWS: dict[str, int] = {
    "none": 0,
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
}


class TrapCog(commands.Cog):
    """The honeypot channel: silent deletion plus a configured consequence."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        try:
            if not await get_setting(self.bot.storage, message.guild.id, "trap.enabled"):
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

    #: consequence ---------------------------------------------------------

    async def _punish(self, guild: discord.Guild, member: discord.Member) -> None:
        """Apply the configured consequence, then DM and log (best-effort)."""
        guild_id = guild.id
        action = await get_setting(self.bot.storage, guild_id, "trap.action")
        window = await get_setting(self.bot.storage, guild_id, "trap.delete_window")
        seconds = DELETE_WINDOWS.get(str(window), 0)
        bot_permissions = guild.me.guild_permissions
        reason = await self.bot.translator.t(guild_id, "trap.reason")

        acted = False
        if action == "ban" and bot_permissions.ban_members:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await guild.ban(
                    member,
                    reason=reason,
                    delete_message_seconds=seconds or None,
                )
                acted = True
        elif action == "softban":
            can_ban = bot_permissions.ban_members and member.top_role < guild.me.top_role
            if can_ban:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await guild.ban(
                        member,
                        reason=reason,
                        delete_message_seconds=seconds or None,
                    )
                    if await self._unban(guild, member.id):
                        acted = True
        elif (
            action == "kick"
            and bot_permissions.kick_members
            and member.top_role < guild.me.top_role
        ):
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await member.kick(reason=reason)
                acted = True

        if acted:
            with contextlib.suppress(Exception):
                await self._dm_victim(guild_id, member, action)
        await self._log_trap(guild, member, action, acted)

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
        translated = await self.bot.translator.t(guild_id, "trap.dm", **common)
        text = await text_or(self.bot, guild_id, "trap.dm", translated, **common)
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await member.send(text)

    async def _log_trap(
        self, guild: discord.Guild, member: discord.Member, action: str, acted: bool
    ) -> None:
        """One audit card (``audit.trap``) when the toggle is on."""
        from rosemary.cogs.audit import AuditCog

        audit = self.bot.get_cog("AuditCog")
        if not isinstance(audit, AuditCog):
            return
        try:
            channel = await audit._channel(guild.id)
            if channel is None or not await audit._on(guild.id, "audit.trap_enabled"):
                return
            from rosemary.cogs.audit import _variables

            action_label = await self.bot.translator.t(
                guild.id, f"trap.audit.action.{action}"
            )
            outcome = await self.bot.translator.t(
                guild.id, "trap.audit.applied" if acted else "trap.audit.skipped"
            )
            await audit._send(
                channel,
                guild.id,
                "trap",
                {
                    **_variables(member, guild),
                    "channel": getattr(
                        guild.get_channel(
                            await get_setting(
                                self.bot.storage, guild.id, "trap.channel"
                            )
                        ),
                        "mention",
                        "",
                    ),
                    "action": action_label,
                    "outcome": outcome,
                },
            )
        except Exception:
            log.exception("trap audit card failed")
