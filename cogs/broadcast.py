"""Live broadcast: relay one channel's messages as DMs to every member.

Admin-only and disabled by default. Starting opens a session bound to the
channel where the command ran: a header DM goes out first, then every
message the starter posts in that channel is fanned out immediately (paced,
real file attachments when they fit, failures counted). ``/broadcast stop``
ends the session with stats. Only one session runs per guild at a time.

The session is stored in ``data/<guild_id>/broadcast.json`` so it survives a
restart. Messages that arrived while the bot was offline are not replayed;
the session itself stays active with its target, delay and counters.
"""

import asyncio
import contextlib
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import discord
from discord.ext import commands

from rosemary.core.broadcast import BroadcastSession, BroadcastStore
from rosemary.core.command_guards import require_admin
from rosemary.core.debug import send_log
from rosemary.core.mass_dm import DmAttachment, fan_out, with_role
from rosemary.core.settings import get_setting

log = logging.getLogger(__name__)

# Keep downloaded payloads bounded so one giant upload cannot pin memory for
# the whole fan-out. Files above the cap fall back to their original URL.
_MAX_FILE_BYTES = 24 * 1024 * 1024
_MAX_TOTAL_BYTES = 48 * 1024 * 1024


@dataclass
class _Session(BroadcastSession):
    """Persisted session plus in-process fan-out coordination."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock, compare=False, repr=False)
    tasks: set[asyncio.Task] = field(default_factory=set, compare=False, repr=False)
    cancelled: bool = field(default=False, compare=False, repr=False)


class BroadcastCog(commands.Cog):
    """Live mass-DM relay sessions."""

    broadcast_group = discord.SlashCommandGroup(
        "broadcast",
        description="Live broadcast sessions",
        contexts={discord.InteractionContextType.guild},
        # Every subcommand is admin-only. The API reads the restriction only
        # on the top-level command, so declare it here for real client-side
        # hiding and pair it with require_admin() in each handler.
        default_member_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = BroadcastStore(bot.storage.data_dir)
        self._sessions: dict[int, _Session] = {}
        self._loaded: set[int] = set()
        self._load_lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        """Restore persisted sessions once the bot has joined its guilds."""
        if self._started:
            return
        self._started = True
        for guild in list(getattr(self.bot, "guilds", ())):
            await self._restore(guild.id)

    async def _restore(self, guild_id: int) -> None:
        """Load one guild's session from disk at most once per process."""
        if guild_id in self._loaded:
            return
        async with self._load_lock:
            if guild_id in self._loaded:
                return
            state = await self.store.load(guild_id)
            self._loaded.add(guild_id)
            if state is None:
                return
            self._sessions[guild_id] = _Session(**asdict(state))
            log.info("Restored broadcast session in guild %s", guild_id)

    async def _persist(self, guild_id: int, session: _Session) -> None:
        if session.cancelled:
            return
        try:
            await self.store.save(guild_id, session)
        except OSError:
            log.exception("Could not persist broadcast session in guild %s", guild_id)

    def _spawn(self, guild_id: int, session: _Session, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        session.tasks.add(task)

        def _done(done: asyncio.Task) -> None:
            session.tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                log.error(
                    "Broadcast task failed in guild %s", guild_id, exc_info=error
                )

        task.add_done_callback(_done)
        return task

    async def _react(self, message: discord.Message, kind: str) -> None:
        fallback = {"success": "✅", "error": "❌", "loading": "⏳"}.get(kind, "✅")
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await message.add_reaction(self.bot.theme.emoji(kind) or fallback)

    @broadcast_group.command(
        name="start",
        description="[ADMIN] Start a live broadcast from this channel",
        default_member_permissions=discord.Permissions(administrator=True),
    )
    async def broadcast_start(
        self,
        ctx: discord.ApplicationContext,
        title: discord.Option(str, description="Broadcast title"),
        role: discord.Option(
            discord.Role,
            description="Role to receive this session (default: server setting)",
            required=False,
            default=None,
        ),
        delay: discord.Option(
            int,
            description="Seconds between DMs (default: server setting)",
            required=False,
            default=None,
            min_value=0,
            max_value=30,
        ),
    ) -> None:
        """Open a relay session bound to the current channel."""
        if not await require_admin(ctx):
            return
        guild_id = ctx.guild_id
        if guild_id is None:
            return
        if not await get_setting(self.bot.storage, guild_id, "broadcast.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "broadcast.error_disabled"),
                ephemeral=True,
            )
        await self._restore(guild_id)
        if guild_id in self._sessions:
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "broadcast.error_active"),
                ephemeral=True,
            )
        await ctx.response.defer(ephemeral=True)
        default_delay = await get_setting(
            self.bot.storage, guild_id, "broadcast.delay_seconds"
        )
        default_target = await get_setting(
            self.bot.storage, guild_id, "broadcast.target_role"
        )
        session_delay = int(delay) if delay is not None else int(default_delay or 0)
        session_delay = max(0, min(30, session_delay))
        target_role_id = role.id if role is not None else default_target
        if target_role_id is not None:
            target_role_id = int(target_role_id)
        title = title.strip() or getattr(ctx.channel, "name", "Broadcast")
        session = _Session(
            channel_id=ctx.channel_id,
            author_id=ctx.author.id,
            title=title,
            target_role_id=target_role_id,
            delay_seconds=session_delay,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self._sessions[guild_id] = session
        await self._persist(guild_id, session)
        server = getattr(ctx.guild, "name", "Discord")
        header = await self.bot.translator.t(
            guild_id, "broadcast.header", title=title, server=server
        )
        self._spawn(
            guild_id,
            session,
            self._relay(guild_id, session, header, ()),
        )
        await send_log(
            self.bot,
            guild_id,
            "broadcast.logs.started",
            moderator=ctx.author.mention,
            title=title,
            channel=ctx.channel.mention,
        )
        if role is not None:
            target_label = role.mention
        elif target_role_id:
            target_label = f"<@&{target_role_id}>"
        else:
            target_label = await self.bot.translator.t(
                guild_id, "broadcast.target_all"
            )
        await ctx.respond(
            await self.bot.translator.t(
                guild_id,
                "broadcast.started",
                channel=ctx.channel.mention,
                target=target_label,
            ),
            ephemeral=True,
        )

    @broadcast_group.command(
        name="stop",
        description="[ADMIN] Stop the live broadcast",
        default_member_permissions=discord.Permissions(administrator=True),
    )
    async def broadcast_stop(self, ctx: discord.ApplicationContext) -> None:
        """End the running session and report stats."""
        if not await require_admin(ctx):
            return
        guild_id = ctx.guild_id
        if guild_id is None:
            return
        await self._restore(guild_id)
        session = self._sessions.get(guild_id)
        if session is None:
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "broadcast.error_none"),
                ephemeral=True,
            )
        await ctx.response.defer(ephemeral=True)
        session.cancelled = True
        self._sessions.pop(guild_id, None)
        tasks = list(session.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.store.clear(guild_id)
        except OSError:
            log.exception("Could not clear broadcast session in guild %s", guild_id)
        await send_log(
            self.bot,
            guild_id,
            "broadcast.logs.ended",
            moderator=ctx.author.mention,
            relayed=session.relayed,
            sent=session.sent,
            failed=session.failed,
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

    async def _relay(
        self,
        guild_id: int,
        session: _Session,
        text: str,
        attachments: tuple[DmAttachment, ...],
        source: discord.Message | None = None,
    ) -> None:
        """Deliver one payload to the session target, serialized per session."""
        sent = 0
        try:
            async with session.lock:
                if session.cancelled:
                    return
                guild = self.bot.get_guild(guild_id)
                if guild is not None:
                    base_sent = session.sent
                    base_failed = session.failed

                    def progress(count_ok: int, count_failed: int) -> None:
                        session.sent = base_sent + count_ok
                        session.failed = base_failed + count_failed

                    sent, _ = await fan_out(
                        with_role(guild.members, session.target_role_id),
                        text,
                        session.delay_seconds,
                        attachments=attachments,
                        on_progress=progress,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Broadcast relay failed in guild %s", guild_id)
        finally:
            await self._persist(guild_id, session)
        if source is not None:
            # Individual closed DMs are expected in a large guild; only mark
            # the message as failed when nobody received it at all.
            await self._react(source, "success" if sent else "error")

    async def _payload(
        self, message: discord.Message
    ) -> tuple[str, tuple[DmAttachment, ...]]:
        """Download attachments once, falling back to links when too large."""
        text = (message.content or "").strip()
        links: list[str] = []
        downloaded: list[DmAttachment] = []
        total = 0
        for attachment in message.attachments:
            url = str(getattr(attachment, "url", "") or "")
            size = int(getattr(attachment, "size", 0) or 0)
            if size > _MAX_FILE_BYTES or total + size > _MAX_TOTAL_BYTES:
                links.append(url)
                continue
            try:
                data = await attachment.read()
            except (discord.HTTPException, OSError):
                links.append(url)
                continue
            if len(data) > _MAX_FILE_BYTES or total + len(data) > _MAX_TOTAL_BYTES:
                links.append(url)
                continue
            total += len(data)
            is_spoiler = getattr(attachment, "is_spoiler", None)
            downloaded.append(
                DmAttachment(
                    filename=str(getattr(attachment, "filename", "") or "attachment"),
                    data=data,
                    url=url,
                    spoiler=bool(is_spoiler()) if callable(is_spoiler) else False,
                )
            )
        if links:
            text = "\n".join([text, *links]).strip()
        return text, tuple(downloaded)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Relay the session owner's channel messages to every member."""
        if message.guild is None or message.author.bot:
            return
        guild_id = message.guild.id
        await self._restore(guild_id)
        session = self._sessions.get(guild_id)
        if session is None or session.cancelled:
            return
        if message.channel.id != session.channel_id or message.author.id != session.author_id:
            return
        if session.last_message_id is not None and message.id <= session.last_message_id:
            return
        prefix = await get_setting(self.bot.storage, guild_id, "general.prefix")
        if message.content.startswith((prefix or "!", "/")):
            return
        try:
            text, attachments = await self._payload(message)
            if not text and not attachments:
                return
            if session.cancelled or self._sessions.get(guild_id) is not session:
                return
            session.last_message_id = message.id
            session.relayed += 1
            await self._persist(guild_id, session)
            await self._react(message, "loading")
            self._spawn(
                guild_id,
                session,
                self._relay(guild_id, session, text, attachments, source=message),
            )
        except Exception:
            log.exception("Broadcast relay failed in guild %s", guild_id)
            await self._react(message, "error")
