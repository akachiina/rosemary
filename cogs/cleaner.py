"""Message cleaner: keyword, regex or last-N purge with a live panel.

Scans the scoped text channels (one channel or the whole guild), deletes
messages matching the criterion (bulk delete under 14 days, single delete
above), and reports progress through an ephemeral panel that updates while
the run is active, with a Stop button. Destructive, so it always asks for
confirmation, and the audit cog attributes the whole run through
``note_purge_context`` (renewed per channel so long runs keep attribution).
"""

import asyncio
import contextlib
import dataclasses
import logging
import re
import time
from datetime import UTC, datetime

import discord
from discord.ext import commands

from rosemary.core.cards import log_description, text_or
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting
from rosemary.ui.containers import ActionRow, TextDisplay, designer_container
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)

#: Bounds for the ``last`` option (mirrored by Discord's own option range).
LAST_MIN = 1
LAST_MAX = 1000

#: The purge engine reports through ``progress()`` at channel boundaries;
#: channel-level progress alone is too coarse (a single-channel run finishes
#: before the first channel edit lands). The timer below also edits the panel
#: with the live counters between channel boundaries.
PANEL_EDIT_INTERVAL = 2.5

#: Pause between channels so bulk deletes never trip the rate limit.
CHANNEL_PAUSE_SECONDS = 1.0

#: Consecutive failed timer edits before the panel freezes (token expiry,
#: rate limit): after that, stop editing and stop warning on every tick.
PANEL_EDIT_MAX_FAILURES = 4


@dataclasses.dataclass
class PurgeRequest:
    """What to delete and where: validated once at the command boundary.

    ``mode`` is ``"word"``, ``"regex"`` or ``"last"``; ``last`` doubles as a
    cap on matches when combined with a content criterion (the scan walks
    backwards and stops when the cap fills). ``criterion`` is the localized
    human summary shown on the panel and the audit card.
    """

    mode: str
    word: str = ""
    pattern: re.Pattern | None = None
    last: int | None = None
    channel: discord.TextChannel | None = None
    author_id: int | None = None
    criterion: str = ""


def _format_elapsed(seconds: float) -> str:
    """``mm:ss`` for the panel's elapsed line."""
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


@dataclasses.dataclass
class PurgeResult:
    """Outcome of one run: criteria matches apart from API-confirmed deletes.

    ``matched`` counts every message the criteria selected (including ones
    whose delete call failed or was abandoned); ``deleted`` counts only what
    Discord confirmed. The gap is honest reporting, not a bug.
    """

    deleted: int = 0
    matched: int = 0


class CleanerPurgeView(MenuView):
    """Ephemeral purge panel: confirm, live progress with Stop, then summary.

    The panel is pure UI: the purge engine lives on the cog (``runner``) and
    reports through the ``progress`` callback; a timer edit refreshes the
    counters without a component interaction.
    """

    def __init__(
        self,
        bot,
        guild_id: int,
        *,
        owner_id: int,
        request: PurgeRequest,
    ) -> None:
        super().__init__(author_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.request = request
        self.state = "confirm"  # confirm | running | done
        self.cancel_event = asyncio.Event()
        self.current_channel = ""
        self.channels_done = 0
        self.channels_total = 0
        self.scanned = 0
        self.deleted = 0
        self.matched = 0
        self.started_at = 0.0
        self.final_text = ""
        #: The purge engine (``CleanerCog._purge``), injected by the command.
        self.runner = None
        #: (guild, moderator) passed through to the engine for the audit stamp.
        self.purge_guild = None
        self.moderator = None
        self._interaction = None  # confirming interaction, for timer edits
        self._last_edit = 0.0
        self._edit_failures = 0
        self._task: asyncio.Task | None = None
        self._timer_task: asyncio.Task | None = None
        self.register("cleaner_yes", self._confirm)
        self.register("cleaner_no", self._cancel)
        self.register("cleaner_stop", self._stop)

    async def build_items(self) -> list[discord.ui.ViewItem]:
        """Confirm card, live counters or the final summary, per state."""
        from rosemary.core.card_service import menu_heading_items

        t = self.bot.translator.t
        gid = self.guild_id
        heading = await menu_heading_items(self.bot, gid, "cleaner.panel") or [
            TextDisplay(
                self.bot.theme.md(
                    "title", title=await t(gid, "cleaner.panel_title")
                )
            )
        ]
        parts = list(heading)
        if self.state == "confirm":
            parts.append(
                TextDisplay(
                    await t(gid, "cleaner.progress_criterion", criterion=self.request.criterion)
                )
            )
            body = await text_or(
                self.bot,
                gid,
                "cleaner.confirm",
                await t(gid, "cleaner.confirm_text"),
            )
            parts.append(TextDisplay(body))
        elif self.state == "running":
            parts.append(
                TextDisplay(
                    await t(gid, "cleaner.progress_criterion", criterion=self.request.criterion)
                )
            )
            if self.current_channel:
                parts.append(
                    TextDisplay(
                        await t(gid, "cleaner.progress_current", channel=self.current_channel)
                    )
                )
            parts.append(
                TextDisplay(
                    await t(
                        gid,
                        "cleaner.progress_done",
                        done=self.channels_done,
                        total=self.channels_total,
                    )
                )
            )
            parts.append(
                TextDisplay(await t(gid, "cleaner.progress_scanned", count=self.scanned))
            )
            parts.append(
                TextDisplay(
                    await t(
                        gid,
                        "cleaner.progress_counts",
                        deleted=self.deleted,
                        matched=self.matched,
                    )
                )
            )
            parts.append(
                TextDisplay(
                    await t(
                        gid,
                        "cleaner.progress_elapsed",
                        elapsed=_format_elapsed(time.monotonic() - self.started_at),
                    )
                )
            )
        else:
            parts.append(TextDisplay(self.final_text))
            # Honest reporting: matches the API could not delete.
            if self.matched != self.deleted:
                parts.append(
                    TextDisplay(
                        await t(
                            gid,
                            "cleaner.result_gap",
                            gap=self.matched - self.deleted,
                        )
                    )
                )
        container = designer_container(self.bot.theme.color("warning"), *parts)

        rows: list[discord.ui.ViewItem] = [container]
        if self.state == "confirm":
            rows.append(
                ActionRow(
                    self.make_button(
                        custom_id="cleaner_yes",
                        label=await t(gid, "cleaner.confirm"),
                        style=discord.ButtonStyle.danger,
                    ),
                    self.make_button(
                        custom_id="cleaner_no",
                        label=await t(gid, "cleaner.cancel"),
                    ),
                )
            )
        elif self.state == "running":
            rows.append(
                ActionRow(
                    self.make_button(
                        custom_id="cleaner_stop",
                        label=await t(gid, "cleaner.stop"),
                        style=discord.ButtonStyle.danger,
                        disabled=self.cancel_event.is_set(),
                    )
                )
            )
        return rows

    #: handlers -------------------------------------------------------------

    async def _confirm(self, interaction: discord.Interaction) -> None:
        """ACK, switch to the running panel and start the engine task."""
        self.state = "running"
        self.started_at = time.monotonic()
        # Timer edits go through the interaction's webhook: the response is
        # ephemeral, and the channel endpoint 404s (10008) on it.
        self._interaction = interaction
        self._last_edit = time.monotonic()
        await self.rerender(interaction)
        self._task = asyncio.create_task(self._run())
        self._timer_task = asyncio.create_task(self._panel_timer())

    async def _cancel(self, interaction: discord.Interaction) -> None:
        """Close the panel without deleting anything."""
        self.state = "done"
        self.final_text = await self.bot.translator.t(self.guild_id, "cleaner.cancelled")
        await self.rerender(interaction)
        self.stop()

    async def _stop(self, interaction: discord.Interaction) -> None:
        """Ask the engine to stop between the current batch and the next."""
        self.cancel_event.set()
        await self.rerender(interaction)
    #: engine plumbing -------------------------------------------------------

    async def _run(self) -> None:
        """Drive the purge task and land the panel on the final summary."""
        t = self.bot.translator.t
        gid = self.guild_id
        try:
            result = await self.runner(
                self.request,
                self.purge_guild,
                self.moderator,
                progress=self._progress,
                cancel_event=self.cancel_event,
            )
        except Exception:
            log.exception("purge run failed")
            self._stop_timer()
            with contextlib.suppress(Exception):
                await self._edit_panel()
            return
        self._stop_timer()
        self.deleted = result.deleted
        self.matched = result.matched
        self.state = "done"
        if self.cancel_event.is_set():
            self.final_text = await t(
                gid,
                "cleaner.stopped",
                count=self.deleted,
                matched=self.matched,
                criterion=self.request.criterion,
            )
        else:
            self.final_text = await text_or(
                self.bot,
                gid,
                "cleaner.result",
                await t(
                    gid,
                    "cleaner.result",
                    count=self.deleted,
                    matched=self.matched,
                    criterion=self.request.criterion,
                ),
                count=self.deleted,
                matched=self.matched,
                criterion=self.request.criterion,
            )
        await self._edit_panel()
        await self._send_log()
        self.stop()

    #: panel timer -----------------------------------------------------------

    async def _panel_timer(self) -> None:
        """Edit the panel every ``PANEL_EDIT_INTERVAL`` while the run is active.

        ``progress()`` only fires at channel boundaries, so a single-channel
        run (the common case) would otherwise show zeros until the whole thing
        ends. The timer refreshes counters and elapsed time between boundary
        edits; it dies with the run and never outlives the view.
        """
        try:
            while self.state == "running" and self._task is not None and not self._task.done():
                await asyncio.sleep(PANEL_EDIT_INTERVAL)
                if self.state != "running":
                    break
                with contextlib.suppress(Exception):
                    await self._edit_panel(force=True)
        except asyncio.CancelledError:
            pass

    def _stop_timer(self) -> None:
        """Cancel the periodic edit task (run finished or crashed)."""
        if self._timer_task is not None:
            self._timer_task.cancel()
            self._timer_task = None

    async def _send_log(self) -> None:
        """One staff-log card for the whole run (attribution via the stamp)."""
        t = self.bot.translator.t
        gid = self.guild_id
        with contextlib.suppress(Exception):
            await send_channel_log(
                self.bot,
                gid,
                await t(gid, "cleaner.logs.purged.title"),
                await log_description(
                    self.bot,
                    gid,
                    "cleaner.logs.purged.description",
                    moderator=self.moderator.mention,
                    count=self.matched,
                    deleted=self.deleted,
                    criterion=self.request.criterion,
                ),
                color="warning",
                card_key="cleaner.logs.purged.description",
                mention_user_ids=[self.moderator.id],
            )

    async def _progress(
        self,
        *,
        current: str | None = None,
        done: int | None = None,
        total: int | None = None,
        scanned: int | None = None,
        deleted: int | None = None,
        matched: int | None = None,
    ) -> None:
        """Engine callback: store counters, edit the panel at most every 2.5s."""
        if current is not None:
            self.current_channel = current
        if done is not None:
            self.channels_done = done
        if total is not None:
            self.channels_total = total
        if scanned is not None:
            self.scanned = scanned
        if deleted is not None:
            self.deleted = deleted
        if matched is not None:
            self.matched = matched
        now = time.monotonic()
        if self._interaction is None or now - self._last_edit < PANEL_EDIT_INTERVAL:
            return
        self._last_edit = now
        await self._edit_panel(force=True)

    async def _edit_panel(self, *, force: bool = False) -> None:
        """Timer edit of the original response through the interaction webhook.

        The response is ephemeral: the channel endpoint cannot see it (404
        Unknown Message), only the interaction's webhook can. After the 15
        min token expires edits fail permanently (Unknown Webhook Token or
        404): freeze with a backoff instead of warning on every tick.
        """
        if self._interaction is None:
            return
        if force and self.state != "running":
            # A timer/boundary tick that woke up after the run finished must
            # never overwrite the final summary with stale counters.
            return
        await self.prepare()
        try:
            await self._interaction.edit_original_response(view=self)
        except discord.NotFound:
            self._interaction = None
        except discord.HTTPException as exc:
            if not force:
                log.warning("purge panel edit failed, freezing the panel: %s", exc)
                self._interaction = None
                return
            # Timer tick: back off instead of spamming warnings every 2.5s.
            self._edit_failures += 1
            if self._edit_failures >= PANEL_EDIT_MAX_FAILURES:
                log.warning(
                    "purge panel edit failed %d times, freezing: %s",
                    self._edit_failures,
                    exc,
                )
                self._interaction = None
            else:
                await asyncio.sleep(PANEL_EDIT_INTERVAL * self._edit_failures)


class CleanerCog(commands.Cog):
    """Keyword, regex or last-N message purge with a live panel."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @discord.slash_command(
        name="limpar",
        description="Delete messages by word, regex or amount",
        default_member_permissions=discord.Permissions(manage_messages=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def limpar(
        self,
        ctx: discord.ApplicationContext,
        palavra: discord.Option(
            str, description="Word or phrase to purge", required=False, default=None
        ),
        regex: discord.Option(
            str, description="Regex pattern (Python) to purge", required=False, default=None
        ),
        last: discord.Option(
            int,
            description="Limit to the N most recent matches",
            required=False,
            default=None,
            min_value=LAST_MIN,
            max_value=LAST_MAX,
        ),
        canal: discord.Option(
            discord.TextChannel, description="Restrict to a channel", required=False, default=None
        ),
        autor: discord.Option(
            discord.User, description="Only messages from this author", required=False, default=None
        ),
    ) -> None:
        """Validate the criteria, confirm, then purge with a live panel."""
        guild_id = ctx.guild_id
        if not await get_setting(self.bot.storage, guild_id, "cleaner.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "cleaner.error_disabled"),
                ephemeral=True,
            )
        word = (palavra or "").strip()
        regex_src = (regex or "").strip()
        if word and regex_src:
            return await self._error(ctx, guild_id, "cleaner.error_conflict")
        if not word and not regex_src and last is None:
            return await self._error(ctx, guild_id, "cleaner.error_no_mode")
        pattern = None
        if regex_src:
            try:
                pattern = re.compile(regex_src, re.IGNORECASE)
            except re.error as exc:
                return await self._error(
                    ctx, guild_id, "cleaner.error_bad_regex", reason=str(exc)
                )
        if last is not None and not LAST_MIN <= last <= LAST_MAX:
            return await self._error(ctx, guild_id, "cleaner.error_last_range")

        request = PurgeRequest(
            mode="word" if word else "regex" if pattern else "last",
            word=word.lower(),
            pattern=pattern,
            last=last,
            channel=canal,
            author_id=autor.id if autor is not None else None,
            criterion=await self._compose_criterion(guild_id, word, pattern, last, canal, autor),
        )
        view = CleanerPurgeView(self.bot, guild_id, owner_id=ctx.author.id, request=request)
        view.runner = self._purge
        view.purge_guild = ctx.guild
        view.moderator = ctx.author
        await view.prepare()
        from rosemary.core.card_service import trace_card_path

        # Heading and confirm body are theme-customizable: trace both paths.
        await trace_card_path(self.bot, guild_id, "cleaner.panel")
        await trace_card_path(self.bot, guild_id, "cleaner.confirm")
        await ctx.respond(view=view, ephemeral=True)

    async def _error(self, ctx, guild_id: int, key: str, **fmt) -> None:
        """One ephemeral validation error, translated."""
        await ctx.respond(
            await self.bot.translator.t(guild_id, key, **fmt),
            ephemeral=True,
        )

    async def _compose_criterion(
        self,
        guild_id: int,
        word: str,
        pattern: re.Pattern | None,
        last: int | None,
        canal,
        autor,
    ) -> str:
        """Localized criterion sentence: mode, then author, then scope."""
        t = self.bot.translator.t
        gid = guild_id
        if last is not None and word:
            base = await t(gid, "cleaner.criterion_last_word", count=last, word=word)
        elif last is not None and pattern is not None:
            base = await t(
                gid, "cleaner.criterion_last_regex", count=last, regex=pattern.pattern
            )
        elif last is not None:
            base = await t(gid, "cleaner.criterion_last", count=last)
        elif word:
            base = await t(gid, "cleaner.criterion_word", word=word)
        else:
            base = await t(gid, "cleaner.criterion_regex", regex=pattern.pattern)
        if autor is not None:
            base += " " + await t(gid, "cleaner.criterion_author", author=autor.mention)
        if canal is not None:
            base += " " + await t(gid, "cleaner.criterion_channel", channel=canal.mention)
        else:
            base += " " + await t(gid, "cleaner.criterion_all")
        return base

    async def _purge(
        self,
        request: PurgeRequest,
        guild: discord.Guild,
        moderator,
        *,
        progress,
        cancel_event: asyncio.Event,
    ) -> int:
        """Delete matching messages; returns confirmed vs matched counts.

        Bulk deletion (batches of 100) under 14 days, single delete above.
        The audit purge stamp is renewed per channel so long runs keep their
        attribution (the stamp otherwise expires after 10 minutes). The
        ``last`` cap counts enqueued matches: the guard must fire before the
        API call, and failures are rare after the permission filter. Deletions
        are counted only when the API call succeeds: ``deleted`` is what
        Discord confirmed, ``matched`` is what the criteria selected.
        """
        channels = (
            [request.channel] if request.channel is not None else list(guild.text_channels)
        )
        channels = [c for c in channels if self._can_purge(c)]
        total = len(channels)
        deleted = 0  # confirmed by the API
        matched = 0  # selected by the criteria
        scanned = 0
        done = 0
        cap = request.last
        cap_reached = False
        cutoff = datetime.now(UTC).timestamp() - 14 * 86400
        from rosemary.cogs.audit import AuditCog

        audit = self.bot.get_cog("AuditCog")
        for channel in channels:
            if cancel_event.is_set() or cap_reached:
                break
            await progress(
                current=channel.mention,
                done=done,
                total=total,
                scanned=scanned,
                deleted=deleted,
                matched=matched,
            )
            if isinstance(audit, AuditCog):
                audit.note_purge_context(guild, moderator)
            batch: list[discord.Message] = []
            try:
                async for message in channel.history(limit=None):
                    scanned += 1
                    if cancel_event.is_set():
                        break
                    if cap is not None and matched >= cap:
                        cap_reached = True
                        break
                    if (
                        request.author_id is not None
                        and message.author.id != request.author_id
                    ):
                        continue
                    if not self._matches(request, message):
                        continue
                    if message.created_at.timestamp() >= cutoff:
                        batch.append(message)
                        matched += 1
                        if len(batch) >= 100:
                            with _suppress():
                                await channel.delete_messages(batch)
                                deleted += len(batch)
                            batch = []
                    else:
                        matched += 1
                        with _suppress():
                            await message.delete()
                            deleted += 1
                if batch and not cancel_event.is_set():
                    with _suppress():
                        await channel.delete_messages(batch)
                        deleted += len(batch)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Purge failed in channel %s: %s", channel.id, exc)
            done += 1
            await progress(
                done=done, total=total, scanned=scanned, deleted=deleted, matched=matched
            )
            if not (cancel_event.is_set() or cap_reached):
                await asyncio.sleep(CHANNEL_PAUSE_SECONDS)
        return PurgeResult(deleted=deleted, matched=matched)

    @staticmethod
    def _matches(request: PurgeRequest, message: discord.Message) -> bool:
        """Content criterion: word substring, regex search, or anything."""
        content = message.content or ""
        if request.mode == "word":
            return request.word in content.lower()
        if request.mode == "regex":
            return request.pattern.search(content) is not None
        return True

    @staticmethod
    def _can_purge(channel) -> bool:
        """The bot needs read + manage messages to purge a channel."""
        permissions = channel.permissions_for(channel.guild.me)
        return bool(permissions.read_messages and permissions.manage_messages)


def _suppress():
    return contextlib.suppress(discord.Forbidden, discord.HTTPException)
