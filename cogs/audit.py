"""Registro de Eventos: channel cards for moderation-relevant activity.

One master channel (``audit.channel``), one toggle per event, one customizable
card per event (``audit.<event>``). Default layouts follow the welcome-cog
event style (avatar section, emoji title, divider, server footer); message
content travels in fenced blocks (```), added by the send site so theme
overrides inherit it. Bot messages and the bot's own deletes are skipped.
Bulk deletes coalesce for a few seconds into one card plus a ``.txt`` file
with the original messages (so ``/limpar`` batches do not flood the channel).
"""

from __future__ import annotations

import asyncio
import io
import logging
import time

import discord
from discord.ext import commands

from rosemary.core.settings import get_setting

log = logging.getLogger(__name__)

#: How long bulk-delete events are coalesced before flushing one summary.
BULK_FLUSH_SECONDS = 3.0
#: How long a note_purge_context stamp stays valid for attribution.
PURGE_CONTEXT_SECONDS = 600.0

#: Audit-log action ids for ban / unban / message-delete lookups.
AUDIT_BAN = 25
AUDIT_UNBAN = 26
AUDIT_MESSAGE_DELETE = 72


def _fence(value: str) -> str:
    """Wrap text in a fenced block, neutralizing backtick runs inside."""
    if not value:
        return "``` ```"
    cleaned = value.replace("```", "`\\`\\`")
    return f"```{cleaned}```"


def _variables(user: discord.abc.User, guild: discord.Guild) -> dict:
    """The shared identity block every audit card receives."""
    return {
        "user": user.mention,
        "user_name": getattr(user, "display_name", None) or user.name,
        "user_avatar": user.display_avatar.url,
        "server": guild.name,
    }


async def default_audit_document(
    bot, guild_id: int, *, title_key: str, body_key: str, color: str, emoji_token: str
) -> dict:
    """Catalog-default audit card, mirroring the welcome-cog event layout."""
    from rosemary.core.cards import ECHO_VARIABLES, safe_format

    mapping = {**bot.theme.emojis, **ECHO_VARIABLES}

    async def raw(key: str) -> str:
        return safe_format(await bot.translator.raw(guild_id, key), mapping)

    title = await raw(title_key)
    if not title.startswith("#"):
        emoji = bot.theme.emojis.get(emoji_token, "")
        title = f"# {f'{emoji} ' if emoji else ''}{title}".strip()
    body = await raw(body_key)
    return {
        "v": 1,
        "blocks": [
            {
                "type": "container",
                "color": color,
                "children": [
                    {
                        "type": "section",
                        "accessory": {"type": "thumbnail", "url": "{user_avatar}"},
                        "children": [
                            {"type": "text", "body": title},
                            {"type": "text", "body": body},
                        ],
                    },
                    {"type": "divider"},
                    {"type": "text", "body": "-# {server}"},
                ],
            }
        ],
    }


_AUDIT_EVENTS = (
    ("ban", "card.audit.ban.title", "card.audit.ban.body", "danger", "ban"),
    ("unban", "card.audit.unban.title", "card.audit.unban.body", "success", "check"),
    (
        "message_delete",
        "card.audit.message_delete.title",
        "card.audit.message_delete.body",
        "warning",
        "trash",
    ),
    (
        "message_edit",
        "card.audit.message_edit.title",
        "card.audit.message_edit.body",
        "info",
        "pencil",
    ),
    (
        "bulk_delete",
        "card.audit.bulk_delete.title",
        "card.audit.bulk_delete.body",
        "warning",
        "trash",
    ),
    ("nickname", "card.audit.nickname.title", "card.audit.nickname.body", "info", "swap"),
    ("avatar", "card.audit.avatar.title", "card.audit.avatar.body", "info", "frame"),
    ("roles", "card.audit.roles.title", "card.audit.roles.body", "info", "tag"),
    ("timeout", "card.audit.timeout.title", "card.audit.timeout.body", "danger", "mute"),
    ("voice_join", "card.audit.voice_join.title", "card.audit.voice_join.body", "success", "door"),
    ("voice_leave", "card.audit.voice_leave.title", "card.audit.voice_leave.body", "info", "door"),
)


def _register_default_builders() -> None:
    from rosemary.core.cards import set_default_builder

    for name, title_key, body_key, color, emoji in _AUDIT_EVENTS:

        async def _builder(bot, guild_id, *, t=title_key, b=body_key, c=color, e=emoji):
            return await default_audit_document(
                bot, guild_id, title_key=t, body_key=b, color=c, emoji_token=e
            )

        set_default_builder(f"audit.{name}", _builder)


_register_default_builders()


class AuditCog(commands.Cog):
    """Registro de Eventos: registry cards for moderation-relevant activity."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._pending_bulk: dict[int, list[discord.Message]] = {}
        self._bulk_task: dict[int, asyncio.Task] = {}
        #: guild_id -> (member_id, monotonic expiry) for /limpar attribution.
        self._purge_context: dict[int, tuple[int, float]] = {}

    # -- plumbing ------------------------------------------------------------

    async def _channel(self, guild_id: int) -> discord.TextChannel | None:
        """The master audit channel, or None when the feature is off."""
        if not await get_setting(self.bot.storage, guild_id, "audit.enabled"):
            return None
        channel_id = await get_setting(self.bot.storage, guild_id, "audit.channel")
        channel = self.bot.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _on(self, guild_id: int, toggle_key: str) -> bool:
        return await get_setting(self.bot.storage, guild_id, toggle_key)

    async def _send(self, channel, guild_id: int, key: str, variables: dict) -> None:
        """Render ``audit.<event>`` through the shared pipeline and send it."""
        from rosemary.core.card_service import CardPayload, render_card_message, send_card
        from rosemary.core.cards import maybe_text
        from rosemary.core.mentions import allowed_for_ids
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        card_key = f"audit.{key}"
        try:
            payload, allowed = await render_card_message(self.bot, guild_id, card_key, variables)
            if payload is None:
                # No theme override and the default builder failed: catalog
                # text fallback, still a themed container.
                text = await maybe_text(self.bot, guild_id, card_key, **variables)
                view = DesignerView(store=False)
                if text is None:
                    t = self.bot.translator.t
                    theme = self.bot.theme
                    view.add_item(
                        designer_container(
                            theme.color("info"),
                            TextDisplay(
                                theme.md(
                                    "title",
                                    title=await t(guild_id, f"{card_key}.title"),
                                )
                            ),
                            TextDisplay(await t(guild_id, f"{card_key}.body", **variables)),
                        )
                    )
                else:
                    view.add_item(TextDisplay(text))
                payload = CardPayload(view=view)
                allowed = await allowed_for_ids(self.bot, guild_id, card_key, user_ids=[])
            await send_card(channel, payload, allowed)
        except discord.HTTPException as exc:
            log.warning("Audit card %s failed in guild %s: %s", key, guild_id, exc)
        except Exception:
            log.exception("Audit card %s failed unexpectedly", key)

    async def _audit_log_context(self, guild: discord.Guild, user_id: int, action: int):
        """Best-effort (moderator mention, reason) from the Discord audit log."""
        try:
            async for entry in guild.audit_logs(limit=6, action=action):
                if entry.target and getattr(entry.target, "id", None) == user_id:
                    return entry.user.mention, entry.reason or ""
        except (discord.Forbidden, discord.HTTPException):
            pass
        return "", ""

    # -- ban / unban ---------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        try:
            channel = await self._channel(guild.id)
            if channel is None or not await self._on(guild.id, "audit.ban_enabled"):
                return
            moderator, reason = await self._audit_log_context(guild, user.id, AUDIT_BAN)
            await self._send(
                channel,
                guild.id,
                "ban",
                {**_variables(user, guild), "moderator": moderator, "reason": reason},
            )
        except Exception:
            log.exception("on_member_ban audit failed")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        try:
            channel = await self._channel(guild.id)
            if channel is None or not await self._on(guild.id, "audit.unban_enabled"):
                return
            moderator, reason = await self._audit_log_context(guild, user.id, AUDIT_UNBAN)
            await self._send(
                channel,
                guild.id,
                "unban",
                {**_variables(user, guild), "moderator": moderator, "reason": reason},
            )
        except Exception:
            log.exception("on_member_unban audit failed")

    # -- messages ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        try:
            if message.guild is None or message.author.bot:
                return
            guild = message.guild
            channel = await self._channel(guild.id)
            if channel is None or not await self._on(guild.id, "audit.message_delete_enabled"):
                return
            # A /limpar run in progress: singles are covered by the bulk
            # summary, one card per message would flood the channel.
            stamp = self._purge_context.get(guild.id)
            if stamp and stamp[1] > time.monotonic():
                return
            await self._send(
                channel,
                guild.id,
                "message_delete",
                {
                    **_variables(message.author, guild),
                    "message_author": message.author.mention,
                    "message_author_name": message.author.display_name,
                    "message": _fence(message.content or ""),
                    "channel": message.channel.mention,
                    "message_link": message.jump_url,
                },
            )
        except Exception:
            log.exception("on_message_delete audit failed")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        try:
            if before.guild is None or before.author.bot:
                return
            if (before.content or "") == (after.content or ""):
                return
            guild = before.guild
            channel = await self._channel(guild.id)
            if channel is None or not await self._on(guild.id, "audit.message_edit_enabled"):
                return
            await self._send(
                channel,
                guild.id,
                "message_edit",
                {
                    **_variables(before.author, guild),
                    "message_author": before.author.mention,
                    "message_author_name": before.author.display_name,
                    "message": _fence(before.content or ""),
                    "new_message": _fence(after.content or ""),
                    "channel": before.channel.mention,
                    "message_link": after.jump_url,
                },
            )
        except Exception:
            log.exception("on_message_edit audit failed")

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        """Coalesce batches: one card + one .txt after a short quiet window."""
        try:
            if not messages:
                return
            guild = messages[0].guild
            if guild is None:
                return
            human = [m for m in messages if not m.author.bot]
            if not human:
                return
            bucket = self._pending_bulk.setdefault(guild.id, [])
            bucket.extend(human)
            if guild.id not in self._bulk_task or self._bulk_task[guild.id].done():
                self._bulk_task[guild.id] = asyncio.create_task(
                    self._flush_bulk(guild.id, BULK_FLUSH_SECONDS)
                )
        except Exception:
            log.exception("on_bulk_message_delete audit failed")

    async def _flush_bulk(self, guild_id: int, delay: float) -> None:
        """Send one bulk summary for everything coalesced in the window.

        The window re-arms while new batches keep arriving, so a long
        ``/limpar`` run produces a single summary, not one per 100-batch.
        """
        try:
            awaited = len(self._pending_bulk.get(guild_id, []))
            while True:
                await asyncio.sleep(delay)
                bucket_now = self._pending_bulk.get(guild_id, [])
                if len(bucket_now) == awaited:
                    break
                awaited = len(bucket_now)
            bucket = self._pending_bulk.pop(guild_id, [])
            channel = await self._channel(guild_id)
            if not bucket or channel is None:
                return
            if not await self._on(guild_id, "audit.bulk_delete_enabled"):
                return
            guild = self.bot.get_guild(guild_id) or bucket[0].guild
            count = len(bucket)
            moderator = await self._bulk_moderator(guild, bucket)
            t = self.bot.translator.t
            channel_label = getattr(bucket[0].channel, "mention", "") or guild.name
            if count > 1 and len({m.channel.id for m in bucket}) > 1:
                channel_label = await t(guild_id, "audit.bulk_multiple_channels",
                                        count=len({m.channel.id for m in bucket}))
            file_url = ""
            if await self._on(guild_id, "audit.bulk_file_enabled"):
                file_url = await self._bulk_file(channel, bucket)
            await self._send(
                channel,
                guild_id,
                "bulk_delete",
                {
                    **_variables(bucket[0].author, guild),
                    "count": count,
                    "channel": channel_label,
                    "file_url": file_url,
                    "moderator": moderator,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bulk flush audit failed")

    async def _bulk_moderator(self, guild: discord.Guild, bucket: list) -> str:
        """Purge attribution: /limpar stamp first, audit-log lookup second."""
        stamp = self._purge_context.get(guild.id)
        if stamp and stamp[1] > time.monotonic():
            member = guild.get_member(stamp[0])
            if member is not None:
                return member.mention
        first = bucket[0]
        moderator, _ = await self._audit_log_context(
            guild, first.author.id, AUDIT_MESSAGE_DELETE
        )
        return moderator

    async def _bulk_file(self, channel, messages: list) -> str:
        """Post the ``.txt`` transcript of purged messages; return its URL."""
        lines = []
        for m in sorted(messages, key=lambda x: x.created_at):
            stamp = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"[{stamp}] {m.author} ({m.id}): {m.content or ''}")
        data = io.BytesIO("\n".join(lines).encode("utf-8"))
        filename = f"purga-{messages[0].guild.id}-{int(time.time())}.txt"
        try:
            msg = await channel.send(file=discord.File(data, filename=filename))
            return msg.attachments[0].url if msg.attachments else ""
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Bulk delete transcript failed: %s", exc)
            return ""

    def note_purge_context(self, guild: discord.Guild, moderator) -> None:
        """Stamp who is running a purge so the bulk summary can attribute it."""
        self._purge_context[guild.id] = (moderator.id, time.monotonic() + PURGE_CONTEXT_SECONDS)

    # -- member changes ------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Nickname, avatar (best-effort), role and timeout diffs."""
        try:
            if after.bot:
                return
            guild = after.guild
            channel: discord.TextChannel | None = None

            if before.nick != after.nick and await self._on(guild.id, "audit.nickname_enabled"):
                channel = channel or await self._channel(guild.id)
                if channel is not None:
                    t = self.bot.translator.t
                    no_nick = await t(guild.id, "audit.no_nickname")
                    await self._send(
                        channel,
                        guild.id,
                        "nickname",
                        {
                            **_variables(after, guild),
                            "old_name": before.nick or no_nick,
                            "new_name": after.nick or no_nick,
                        },
                    )

            if (
                before.display_avatar.url != after.display_avatar.url
                and await self._on(guild.id, "audit.avatar_enabled")
            ):
                channel = channel or await self._channel(guild.id)
                if channel is not None:
                    await self._send(
                        channel,
                        guild.id,
                        "avatar",
                        {
                            **_variables(after, guild),
                            "old_avatar": before.display_avatar.url,
                            "new_avatar": after.display_avatar.url,
                        },
                    )

            if before.roles != after.roles and await self._on(guild.id, "audit.roles_enabled"):
                added = [r for r in after.roles if r not in before.roles]
                removed = [r for r in before.roles if r not in after.roles]
                if added or removed:
                    channel = channel or await self._channel(guild.id)
                    if channel is not None:
                        t = self.bot.translator.t
                        lines = []
                        if added:
                            lines.append(
                                await t(
                                    guild.id,
                                    "audit.roles_gained",
                                    role=", ".join(r.mention for r in added),
                                )
                            )
                        if removed:
                            lines.append(
                                await t(
                                    guild.id,
                                    "audit.roles_lost",
                                    role=", ".join(r.mention for r in removed),
                                )
                            )
                        await self._send(
                            channel,
                            guild.id,
                            "roles",
                            {**_variables(after, guild), "role": "\n".join(lines)},
                        )

            old_to = before.timed_out_until
            new_to = after.timed_out_until
            if old_to != new_to and await self._on(guild.id, "audit.timeout_enabled"):
                channel = channel or await self._channel(guild.id)
                if channel is not None:
                    t = self.bot.translator.t
                    if new_to is not None:
                        duration = f"<t:{int(new_to.timestamp())}:f>"
                        state = await t(guild.id, "audit.timeout_applied", duration=duration)
                    else:
                        duration = ""
                        state = await t(guild.id, "audit.timeout_removed")
                    await self._send(
                        channel,
                        guild.id,
                        "timeout",
                        {**_variables(after, guild), "duration": duration, "reason": state},
                    )
        except Exception:
            log.exception("on_member_update audit failed")

    # -- voice ---------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after) -> None:
        try:
            if member.bot or before.channel == after.channel:
                return
            guild = member.guild
            channel = await self._channel(guild.id)
            if channel is None or not await self._on(guild.id, "audit.voice_enabled"):
                return
            if after.channel is not None:
                await self._send(
                    channel,
                    guild.id,
                    "voice_join",
                    {**_variables(member, guild), "channel": after.channel.mention},
                )
            elif before.channel is not None:
                await self._send(
                    channel,
                    guild.id,
                    "voice_leave",
                    {**_variables(member, guild), "channel": before.channel.mention},
                )
        except Exception:
            log.exception("on_voice_state_update audit failed")
