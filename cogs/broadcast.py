"""Live broadcast: relay one channel's messages as DMs to every member.

Admin-only and disabled by default. Starting opens a session bound to the
channel where the command ran: a header DM goes out first, then every
non-command message the starter posts in that channel is fanned out (paced,
attachments linked, failures counted). ``/broadcast_stop`` ends the session
with stats. Only one session runs per guild at a time.
"""

import asyncio
import logging
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from rosemary.core.cards import log_description
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting

log = logging.getLogger(__name__)


@dataclass
class _Session:
    channel_id: int
    author_id: int
    sent: int = 0
    failed: int = 0
    relayed: int = 0
    task: asyncio.Task | None = field(default=None, compare=False)


class BroadcastCog(commands.Cog):
    """Live mass-DM relay sessions."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._sessions: dict[int, _Session] = {}

    @discord.slash_command(
        name="broadcast_start",
        description="[ADMIN] Start a live broadcast from this channel",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def broadcast_start(
        self,
        ctx: discord.ApplicationContext,
        title: discord.Option(str, description="Broadcast title"),
    ) -> None:
        """Open a relay session bound to the current channel."""
        guild_id = ctx.guild_id
        if not await get_setting(self.bot.storage, guild_id, "broadcast.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "broadcast.error_disabled"),
                ephemeral=True,
            )
        if guild_id in self._sessions:
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "broadcast.error_active"),
                ephemeral=True,
            )
        await ctx.response.defer(ephemeral=True)
        session = _Session(channel_id=ctx.channel_id, author_id=ctx.author.id)
        self._sessions[guild_id] = session
        header = await self.bot.translator.t(
            guild_id, "broadcast.header", title=title, server=ctx.guild.name
        )
        session.task = asyncio.create_task(self._fan_out(guild_id, header, session))
        await session.task
        await send_channel_log(
            self.bot,
            guild_id,
            await self.bot.translator.t(guild_id, "broadcast.logs.started.title"),
            await log_description(
                self.bot,
                guild_id,
                "broadcast.logs.started.description",
                moderator=ctx.author.mention,
                title=title,
                channel=ctx.channel.mention,
            ),
            color="info",
            card_key="broadcast.logs.started.description",
            mention_user_ids=[ctx.author.id],
        )
        await ctx.respond(
            await self.bot.translator.t(
                guild_id, "broadcast.started", sent=session.sent, failed=session.failed
            ),
            ephemeral=True,
        )

    @discord.slash_command(
        name="broadcast_stop",
        description="[ADMIN] Stop the live broadcast",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def broadcast_stop(self, ctx: discord.ApplicationContext) -> None:
        """End the running session and report stats."""
        guild_id = ctx.guild_id
        session = self._sessions.pop(guild_id, None)
        if session is None:
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "broadcast.error_none"),
                ephemeral=True,
            )
        if session.task is not None and not session.task.done():
            session.task.cancel()
        await send_channel_log(
            self.bot,
            guild_id,
            await self.bot.translator.t(guild_id, "broadcast.logs.ended.title"),
            await log_description(
                self.bot,
                guild_id,
                "broadcast.logs.ended.description",
                moderator=ctx.author.mention,
                relayed=session.relayed,
                sent=session.sent,
                failed=session.failed,
            ),
            color="info",
            card_key="broadcast.logs.ended.description",
            mention_user_ids=[ctx.author.id],
        )
        await ctx.respond(
            await self.bot.translator.t(
                guild_id,
                "broadcast.ended",
                relayed=session.relayed,
                sent=session.sent,
                failed=session.failed,
            ),
            ephemeral=True,
        )

    async def _fan_out(self, guild_id: int, text: str, session: _Session) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        delay = await get_setting(self.bot.storage, guild_id, "broadcast.delay_seconds")
        for member in list(guild.members):
            if member.bot:
                continue
            try:
                await member.send(text)
                session.sent += 1
            except (discord.Forbidden, discord.HTTPException):
                session.failed += 1
            await asyncio.sleep(max(delay, 0))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Relay the session owner's channel messages to every member."""
        if message.guild is None or message.author.bot:
            return
        session = self._sessions.get(message.guild.id)
        if session is None:
            return
        if message.channel.id != session.channel_id or message.author.id != session.author_id:
            return
        if message.content.startswith(("!", "/")):
            return
        try:
            text = message.content or ""
            if message.attachments:
                links = "\n".join(attachment.url for attachment in message.attachments)
                text = f"{text}\n{links}".strip()
            if not text:
                return
            session.relayed += 1
            await self._fan_out(message.guild.id, text, session)
            await message.add_reaction("✅")
        except Exception as exc:
            log.error("Broadcast relay failed: %s", exc)
            with _suppress():
                await message.add_reaction("⏳")


def _suppress():
    import contextlib

    return contextlib.suppress(discord.Forbidden, discord.HTTPException)


def setup(bot) -> None:
    bot.add_cog(BroadcastCog(bot))
