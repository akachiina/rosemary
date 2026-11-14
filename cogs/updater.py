"""Self-update: check, confirm, back up, apply and restart.

Two channels (host configuration only): ``git`` follows ``origin/<branch>``
commit by commit, ``stable`` follows the latest ``vX.Y.Z`` release. The
release watcher runs when ``update.auto`` is on, so the host does not need
the slash command; the command is a manual trigger for the trusted hosts.
After restarting, the guilds that opted in are notified (log channel and a
DM to the owner and admins).

Nothing here is a security boundary: the host edits the code. The command
also checks the host ids at runtime, because Discord's permission overrides
are a hint (a server admin can change them).
"""

import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands

from rosemary import __version__
from rosemary.core.cards import text_or
from rosemary.core.debug import send_log
from rosemary.core.host import HostConfig, load_host_config
from rosemary.core.settings import get_setting
from rosemary.core.updater import (
    apply_release,
    backup_data,
    compare,
    describe_target,
    fetch,
    is_clean,
    is_newer,
    latest_release_tag,
    origin_slug,
    prune_backups,
    reset_hard,
    should_notify_failure,
)
from rosemary.ui.containers import TextDisplay

log = logging.getLogger(__name__)

#: GitHub API root (public repositories need no token; 60 requests/hour).
_GITHUB_API = "https://api.github.com"


class UpdaterCog(commands.Cog):
    """Host-gated self update with backup, release watcher and restart."""

    def __init__(self, bot, host: HostConfig | None = None) -> None:
        self.bot = bot
        self.host = host or load_host_config()
        self._started = False
        self._last_failure: str | None = None
        self._watch_task: asyncio.Task | None = None
        self._release_cache: dict[str, dict] = {}
        # The commands are registered only when the host allows them; the
        # watcher and the post-update notification run regardless.
        allowed = {"update": self.host.update_command, "restart": self.host.restart_command}
        self.__cog_commands__ = [
            cmd for cmd in self.__cog_commands__ if allowed.get(cmd.name, True)
        ]

    @property
    def _repo(self) -> Path:
        return Path(__file__).resolve().parents[1]

    @property
    def _data_dir(self) -> Path:
        return self._repo / "data"

    @property
    def _backup_root(self) -> Path:
        return self._repo / "data_backups"

    @property
    def _pending_flag(self) -> Path:
        return self._data_dir / ".update_pending"

    async def start(self) -> None:
        """Notify once after an update, then start the release watcher."""
        if self._started:
            return
        self._started = True
        await self._notify_pending()
        if self.host.update_auto:
            self._watch_task = asyncio.create_task(self._watch_loop())

    def cog_unload(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()

    async def _watch_loop(self) -> None:
        """Check for a newer release every ``update.check_hours``."""
        await self.bot.wait_until_ready()
        interval = max(1, int(self.host.update_check_hours)) * 3600
        while not self.bot.is_closed():
            await asyncio.sleep(interval)
            try:
                await self._check_release()
            except Exception as exc:
                log.error("Release watcher failed: %s", exc)

    async def _check_release(self) -> None:
        """Apply a newer stable release when the watcher is on."""
        if self.host.update_channel != "stable":
            return
        tag = await latest_release_tag(self._repo)
        if tag is None or not is_newer(__version__, tag):
            return
        self._pending_target = tag
        await self._apply_update(list(self.bot.guilds), "stable", tag)

    async def _fetch_release(self, tag: str) -> dict | None:
        """The release metadata (notes, url, date), cached per process."""
        if tag in self._release_cache:
            return self._release_cache[tag]
        slug = await origin_slug(self._repo)
        if not slug:
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
                f"{_GITHUB_API}/repos/{slug}/releases/tags/{tag}",
                headers={"User-Agent": "Rosemary"},
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            log.warning("GitHub release fetch failed: %s", exc)
            return None
        release = {
            "name": str(data.get("name") or tag),
            "body": str(data.get("body") or "").strip(),
            "url": str(data.get("html_url") or ""),
            "date": str(data.get("published_at") or ""),
        }
        self._release_cache[tag] = release
        return release

    def _is_host(self, ctx: discord.ApplicationContext) -> bool:
        """Whether the invoker is a trusted host (or the guild owner)."""
        if self.host.owner_ids:
            return self.host.is_owner(ctx.author.id)
        guild = ctx.guild
        return bool(guild is not None and ctx.author.id == guild.owner_id)

    async def _notify_pending(self) -> None:
        """After an update: log embed everywhere, DM the owner and admins."""
        flag = self._pending_flag
        if not flag.exists():
            return
        previous = ""
        tag = ""
        guild_ids: list[int] = []
        try:
            raw = json.loads(flag.read_text(encoding="utf-8"))
            previous = str(raw.get("previous") or "")
            tag = str(raw.get("tag") or "")
            guild_ids = [int(value) for value in raw.get("guilds") or []]
        except (OSError, ValueError, TypeError):
            try:
                lines = [
                    line.strip()
                    for line in flag.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                previous = lines[0] if lines and lines[0].startswith("v") else ""
                guild_ids = [int(line) for line in lines if line.isdigit()]
            except (OSError, ValueError):
                previous, guild_ids = "", []
        with contextlib.suppress(OSError):
            flag.unlink()
        release = await self._fetch_release(tag or f"v{__version__}") or {}
        for guild_id in guild_ids:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            try:
                await send_log(
                    self.bot,
                    guild_id,
                    "updater.logs.updated",
                    previous=previous or "-",
                    current=f"v{__version__}",
                    url=release.get("url") or "",
                    notes=(release.get("body") or "")[:1000],
                )
            except Exception as exc:
                log.error("Post-update log failed in %s: %s", guild_id, exc)
            if not await get_setting(self.bot.storage, guild_id, "updater.notify_admins"):
                continue
            await self._dm_admins(guild, previous, release)

    async def _dm_admins(self, guild: discord.Guild, previous: str, release: dict) -> None:
        """DM the owner and the admins with the release card."""
        from rosemary.core.card_service import render_card_message

        variables = {
            "version": __version__,
            "previous": previous or "-",
            "release_name": release.get("name") or f"v{__version__}",
            "notes": (release.get("body") or "")[:3800],
            "url": release.get("url") or "",
        }
        payload, _allowed = await render_card_message(
            self.bot, guild.id, "updater.dm.release", variables
        )
        if payload is None:
            return
        recipients: list[discord.Member] = []
        owner = guild.get_member(guild.owner_id) if guild.owner_id else None
        if owner is not None and not owner.bot:
            recipients.append(owner)
        for member in guild.members:
            if member.bot or member.id == guild.owner_id:
                continue
            if getattr(member.guild_permissions, "administrator", False):
                recipients.append(member)
        for member in recipients:
            try:
                await member.send(**payload.message_kwargs())
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.debug("Release DM to %s failed: %s", member.id, exc)

    async def _record_failure(self, signature: str | None, guilds: list) -> None:
        notify, self._last_failure = should_notify_failure(self._last_failure, signature)
        if signature is not None:
            log.warning("Update check failed (%s)", signature)
        if notify and signature is not None:
            for guild in guilds:
                try:
                    await send_log(
                        self.bot,
                        guild.id,
                        "updater.logs.auto_failed",
                        reason=signature,
                    )
                except Exception as exc:
                    log.error("Update failure notify failed: %s", exc)

    async def _evaluate(self, channel: str, branch: str) -> str:
        """Fetch and decide. Returns ``up_to_date``, ``ready`` or a failure key."""
        if channel == "stable":
            latest = await self._latest_release_tag()
            target, _ = describe_target(
                channel="stable",
                current_version=__version__,
                latest_tag=latest,
                behind=0,
                branch=branch,
            )
            if target is None:
                return "up_to_date"
        else:
            if not await fetch(self._repo, branch):
                return "fetch"
            behind, _ = await compare(self._repo, branch)
            target, _ = describe_target(
                channel="git",
                current_version=__version__,
                latest_tag=None,
                behind=behind,
                branch=branch,
            )
            if target is None:
                return "up_to_date"
        if not await is_clean(self._repo):
            return "dirty"
        self._pending_target = target
        return "ready"

    async def _apply_release(self, tag: str) -> bool:
        """Download the release tarball and copy it over the repo."""
        return await apply_release(self._repo, tag)

    async def _apply_update(
        self, guilds: list, channel: str, target: str | None = None
    ) -> None:
        """Back up, apply and restart (or only stage when restart is off)."""
        target = target or getattr(self, "_pending_target", None)
        if target is None:
            return
        if self.host.update_backup:
            try:
                backup_data(self._data_dir, self._backup_root)
                prune_backups(self._backup_root)
            except OSError as exc:
                log.error("Update backup failed: %s", exc)
                await self._record_failure("backup", guilds)
                return
        if channel == "stable":
            ok = await self._apply_release(target)
            if not ok:
                await self._record_failure("reset", guilds)
                return
        elif not await reset_hard(self._repo, target):
            log.error("Update reset failed")
            await self._record_failure("reset", guilds)
            return
        for guild in guilds:
            try:
                await send_log(
                    self.bot,
                    guild.id,
                    "updater.logs.updating",
                    moderator="-",
                    target=target,
                    previous=f"v{__version__}",
                )
            except Exception as exc:
                log.error("Pre-update notify failed in %s: %s", guild.id, exc)
        try:
            self._pending_flag.write_text(
                json.dumps(
                    {
                        "previous": f"v{__version__}",
                        "tag": target,
                        "guilds": [g.id for g in self.bot.guilds],
                    }
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("Could not write update flag: %s", exc)
        if not self.host.update_restart_after:
            log.info("Update staged; restart is disabled by the host")
            return
        log.info("Restarting for update to %s (channel %s)", target, channel)
        os.execv(sys.executable, [sys.executable, str(self._repo / "bot.py")])

    @discord.slash_command(
        name="update",
        description="[HOST] Update the bot",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def update(self, ctx: discord.ApplicationContext) -> None:
        """Check for updates and, on confirmation, apply them and restart."""
        guild_id = ctx.guild_id
        t = self.bot.translator.t
        if not self._is_host(ctx):
            return await ctx.respond(
                await t(guild_id, "updater.error_host"), ephemeral=True
            )
        channel = self.host.update_channel
        branch = self.host.update_branch
        await ctx.response.defer(ephemeral=True)
        outcome = await self._evaluate(channel, branch)
        if outcome == "up_to_date":
            return await ctx.respond(
                await t(guild_id, "updater.up_to_date"), ephemeral=True
            )
        if outcome != "ready":
            return await ctx.respond(
                await t(guild_id, f"updater.error_{outcome}"), ephemeral=True
            )
        target = self._pending_target
        target_label = target or ""
        await self._confirm_update(ctx, target, target_label, channel)

    @discord.slash_command(
        name="restart",
        description="[HOST] Restart the bot",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def restart(self, ctx: discord.ApplicationContext) -> None:
        """Restart the process (the host only)."""
        guild_id = ctx.guild_id
        t = self.bot.translator.t
        if not self._is_host(ctx):
            return await ctx.respond(
                await t(guild_id, "updater.error_host"), ephemeral=True
            )
        await ctx.response.defer(ephemeral=True)
        from rosemary.ui.confirm import ConfirmResult, ConfirmView

        cog = self

        class _RestartConfirmView(ConfirmView):
            async def question_items(self) -> list[discord.ui.ViewItem]:
                themed_text = await text_or(
                    cog.bot,
                    guild_id,
                    "updater.restart_confirm",
                    await t(guild_id, "updater.restart_confirm"),
                )
                return [
                    TextDisplay(
                        cog.bot.theme.md(
                            "title", title=await t(guild_id, "updater.restart_title")
                        )
                    ),
                    TextDisplay(themed_text),
                ]

            async def action_rows(self) -> list[discord.ui.ViewItem]:
                return [
                    discord.ui.ActionRow(
                        self.make_button(
                            custom_id="confirm_yes",
                            label=await t(guild_id, "updater.restart_button"),
                            style=discord.ButtonStyle.danger,
                        ),
                        self.make_button(
                            custom_id="confirm_no",
                            label=await t(guild_id, "updater.cancel_button"),
                            style=discord.ButtonStyle.secondary,
                        ),
                    )
                ]

        async def apply_restart():
            async def _restart_later() -> None:
                # Let the confirm view edit its message before the process
                # is replaced, or the click would look unanswered.
                await asyncio.sleep(2)
                os.execv(sys.executable, [sys.executable, str(cog._repo / "bot.py")])

            asyncio.create_task(_restart_later())
            return ConfirmResult(await t(guild_id, "updater.restarting"), "success")

        view = _RestartConfirmView(
            self.bot,
            guild_id=guild_id,
            owner_id=ctx.author.id,
            on_confirm=apply_restart,
        )
        view.question_color = "warning"
        await view.prepare()
        await ctx.respond(**await view.message_kwargs(), ephemeral=True)

    async def _confirm_update(
        self,
        ctx: discord.ApplicationContext,
        target: str | None,
        target_label: str,
        channel: str,
    ) -> None:
        """The standard confirm dialog before applying an update."""
        guild_id = ctx.guild_id
        t = self.bot.translator.t
        from rosemary.core.debug import trace_card_path
        from rosemary.ui.confirm import ConfirmResult, ConfirmView

        cog = self

        class _UpdaterConfirmView(ConfirmView):
            async def question_items(self) -> list[discord.ui.ViewItem]:
                themed_text = await text_or(
                    cog.bot,
                    guild_id,
                    "updater.confirm",
                    await t(guild_id, "updater.confirm", target=target_label),
                    target=target_label,
                )
                return [
                    TextDisplay(
                        cog.bot.theme.md(
                            "title",
                            title=await t(guild_id, "updater.confirm_title"),
                        )
                    ),
                    TextDisplay(themed_text),
                ]

            async def action_rows(self) -> list[discord.ui.ViewItem]:
                return [
                    discord.ui.ActionRow(
                        self.make_button(
                            custom_id="confirm_yes",
                            label=await t(guild_id, "updater.confirm_button"),
                            style=discord.ButtonStyle.danger,
                        ),
                        self.make_button(
                            custom_id="confirm_no",
                            label=await t(guild_id, "updater.cancel_button"),
                            style=discord.ButtonStyle.secondary,
                        ),
                    )
                ]

        async def apply_update():
            await cog._apply_update(list(cog.bot.guilds), channel, target)
            return ConfirmResult(await t(guild_id, "updater.updating_now"), "success")

        view = _UpdaterConfirmView(
            self.bot,
            guild_id=guild_id,
            owner_id=ctx.author.id,
            on_confirm=apply_update,
        )
        view.question_color = "warning"
        await view.prepare()
        await trace_card_path(self.bot, guild_id, "updater.confirm")
        await ctx.respond(**await view.message_kwargs(), ephemeral=True)
