"""Self-update: check, confirm, back up, reset and restart.

Two channels: ``git`` follows ``origin/<branch>`` commit by commit, ``stable``
follows the latest ``vX.Y.Z`` release tag (default). Admin-only and disabled
by default. Requires a clean working tree (local changes are never
overwritten blindly). After restarting, guilds that had logging enabled are
notified through their log channels.
"""

import logging
import os
import sys
import time
from pathlib import Path

import discord
import discord.ext.tasks as tasks
from discord.ext import commands

from rosemary import __version__
from rosemary.core.cards import log_description, text_or
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting
from rosemary.core.updater import (
    backup_data,
    compare,
    describe_target,
    fetch,
    fetch_tags,
    is_clean,
    latest_stable,
    prune_backups,
    reset_hard,
    should_notify_failure,
)
from rosemary.ui.containers import TextDisplay
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)


class UpdaterCog(commands.Cog):
    """Git-based self update with backup and restart."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._started = False
        self._last_check: float | None = None
        self._last_failure: str | None = None

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
        """Notify log channels once after an update restart, then start the loop."""
        if self._started:
            return
        self._started = True
        await self._notify_pending()
        self._loop.start()

    def cog_unload(self) -> None:
        self._loop.cancel()

    @tasks.loop(hours=1)
    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self.check_auto_update()
        except Exception as exc:
            log.error("Auto-update check failed: %s", exc)

    @_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _notify_pending(self) -> None:
        flag = self._pending_flag
        if not flag.exists():
            return
        try:
            lines = [line.strip() for line in flag.read_text().splitlines() if line.strip()]
            previous = lines[0] if lines and lines[0].startswith("v") else None
            guild_ids = [int(line) for line in lines if line.isdigit()]
        except OSError:
            previous, guild_ids = None, []
        with _suppress_os():
            flag.unlink()
        for guild_id in guild_ids:
            try:
                await send_channel_log(
                    self.bot,
                    guild_id,
                    await self.bot.translator.t(guild_id, "updater.logs.updated.title"),
                    await log_description(
                        self.bot,
                        guild_id,
                        "updater.logs.updated.description",
                        previous=previous or "-",
                        current=f"v{__version__}",
                    ),
                    color="success",
                    card_key="updater.logs.updated.description",
                )
            except Exception as exc:
                log.error("Post-update notify failed in %s: %s", guild_id, exc)

    async def _enabled_guilds(self) -> list:
        """Guilds with auto-update switched on, in stable id order."""
        enabled = []
        for guild in list(self.bot.guilds):
            try:
                if await get_setting(self.bot.storage, guild.id, "updater.enabled"):
                    enabled.append(guild)
            except Exception as exc:
                log.error("Auto-update setting read failed in %s: %s", guild.id, exc)
        return sorted(enabled, key=lambda guild: guild.id)

    async def check_auto_update(self) -> None:
        """Hourly tick: apply the update when due in any enabled guild."""
        guilds = await self._enabled_guilds()
        if not guilds:
            return
        interval = min(
            [
                await get_setting(
                    self.bot.storage, guild.id, "updater.check_interval_hours"
                )
                for guild in guilds
            ]
        )
        now = time.monotonic()
        if self._last_check is not None and now - self._last_check < interval * 3600:
            return
        self._last_check = now
        channels = {
            await get_setting(self.bot.storage, guild.id, "updater.channel")
            for guild in guilds
        }
        channel = "stable" if "stable" in channels else "git"
        branch = await get_setting(self.bot.storage, guilds[0].id, "updater.branch")
        outcome = await self._evaluate(channel, branch)
        if outcome == "up_to_date":
            await self._record_failure(None, guilds)
            return
        if outcome != "ready":
            await self._record_failure(outcome, guilds)
            return
        await self._record_failure(None, guilds)
        await self._restart_for_update(guilds, channel)

    async def _evaluate(self, channel: str, branch: str) -> str:
        """Fetch and decide. Returns ``up_to_date``, ``ready`` or a failure key."""
        if channel == "stable":
            tags = await fetch_tags(self._repo)
            latest = latest_stable(tags)
            if latest is None:
                return "fetch"
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

    async def _record_failure(self, signature: str | None, guilds: list) -> None:
        """Log every failure; notify log channels only on state changes."""
        notify, self._last_failure = should_notify_failure(self._last_failure, signature)
        if signature is not None:
            log.warning("Auto-update check failed (%s)", signature)
        if notify and signature is not None:
            for guild in guilds:
                try:
                    await send_channel_log(
                        self.bot,
                        guild.id,
                        await self.bot.translator.t(
                            guild.id, "updater.logs.auto_failed.title"
                        ),
                        await log_description(
                            self.bot,
                            guild.id,
                            "updater.logs.auto_failed.description",
                            reason=signature,
                        ),
                        color="danger",
                        card_key="updater.logs.auto_failed.description",
                    )
                except Exception as exc:
                    log.error("Auto-update failure notify failed: %s", exc)

    async def _restart_for_update(self, guilds: list, channel: str) -> None:
        target = getattr(self, "_pending_target", None)
        if target is None:
            return
        try:
            backup = backup_data(self._data_dir, self._backup_root)
            prune_backups(self._backup_root)
        except OSError as exc:
            log.error("Auto-update backup failed: %s", exc)
            await self._record_failure("backup", guilds)
            return
        if not await reset_hard(self._repo, target):
            log.error("Auto-update reset failed")
            await self._record_failure("reset", guilds)
            return
        for guild in guilds:
            try:
                await send_channel_log(
                    self.bot,
                    guild.id,
                    await self.bot.translator.t(
                        guild.id, "updater.logs.updating.title"
                    ),
                    await log_description(
                        self.bot,
                        guild.id,
                        "updater.logs.updating.description",
                        moderator="-",
                        target=target,
                        previous=f"v{__version__}",
                    ),
                    color="warning",
                    card_key="updater.logs.updating.description",
                )
            except Exception as exc:
                log.error("Pre-update notify failed in %s: %s", guild.id, exc)
        try:
            self._pending_flag.write_text(
                f"v{__version__}\n" + "\n".join(str(g.id) for g in self.bot.guilds)
            )
        except OSError as exc:
            log.warning("Could not write update flag: %s", exc)
        log.info("Auto-updating to %s (channel %s, backup at %s)", target, channel, backup)
        os.execv(sys.executable, [sys.executable, str(self._repo / "bot.py")])

    @discord.slash_command(
        name="update",
        description="[ADMIN] Update the bot",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def update(self, ctx: discord.ApplicationContext) -> None:
        """Check for updates and, on confirmation, apply them and restart.

        Manual use is always allowed for admins; ``updater.enabled`` only
        gates the automatic loop.
        """
        guild_id = ctx.guild_id
        t = self.bot.translator.t
        channel = await get_setting(self.bot.storage, guild_id, "updater.channel")
        branch = await get_setting(self.bot.storage, guild_id, "updater.branch")
        await ctx.response.defer(ephemeral=True)

        if channel == "stable":
            tags = await fetch_tags(self._repo)
            latest = latest_stable(tags)
            target, target_label = describe_target(
                channel="stable",
                current_version=__version__,
                latest_tag=latest,
                behind=0,
                branch=branch,
            )
            ahead = 0
        else:
            if not await fetch(self._repo, branch):
                return await ctx.respond(
                    await t(guild_id, "updater.error_fetch"), ephemeral=True
                )
            behind, ahead = await compare(self._repo, branch)
            target, target_label = describe_target(
                channel="git",
                current_version=__version__,
                latest_tag=None,
                behind=behind,
                branch=branch,
            )

        if target is None:
            return await ctx.respond(
                await t(guild_id, "updater.up_to_date"), ephemeral=True
            )
        if not await is_clean(self._repo):
            return await ctx.respond(
                await t(guild_id, "updater.error_dirty", ahead=ahead), ephemeral=True
            )

        view = MenuView(author_id=ctx.author.id)
        cog = self

        async def confirm(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            try:
                backup = backup_data(cog._data_dir, cog._backup_root)
                prune_backups(cog._backup_root)
            except OSError as exc:
                log.error("Update backup failed: %s", exc)
                view.clear_items()
                view.add_item(TextDisplay(await t(guild_id, "updater.error_backup")))
                view.stop()
                return await interaction.edit(view=view)
            if target is None or not await reset_hard(cog._repo, target):
                view.clear_items()
                view.add_item(TextDisplay(await t(guild_id, "updater.error_reset")))
                view.stop()
                return await interaction.edit(view=view)
            await cog._notify_all(
                moderator=ctx.author.mention,
                target=target_label,
                previous=f"v{__version__}",
            )
            try:
                cog._pending_flag.write_text(
                    f"v{__version__}\n"
                    + "\n".join(str(g.id) for g in cog.bot.guilds)
                )
            except OSError as exc:
                log.warning("Could not write update flag: %s", exc)
            log.info("Restarting for update to %s (backup at %s)", target, backup)
            os.execv(
                sys.executable, [sys.executable, str(cog._repo / "bot.py")]
            )

        async def cancel(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            view.clear_items()
            view.add_item(TextDisplay(await t(guild_id, "updater.cancelled")))
            view.stop()
            await interaction.edit(view=view)

        view.register("updater_yes", confirm)
        view.register("updater_no", cancel)
        view.add_item(
            TextDisplay(
                await text_or(
                    self.bot,
                    guild_id,
                    "updater.confirm",
                    await t(guild_id, "updater.confirm", target=target_label),
                    target=target_label,
                )
            )
        )
        view.add_item(
            discord.ui.ActionRow(
                view.make_button(
                    custom_id="updater_yes",
                    label=await t(guild_id, "updater.confirm_button"),
                    style=discord.ButtonStyle.danger,
                ),
                view.make_button(
                    custom_id="updater_no",
                    label=await t(guild_id, "updater.cancel_button"),
                    style=discord.ButtonStyle.secondary,
                ),
            )
        )
        await ctx.respond(view=view, ephemeral=True)

    async def _notify_all(self, **variables) -> None:
        for guild in list(self.bot.guilds):
            try:
                await send_channel_log(
                    self.bot,
                    guild.id,
                    await self.bot.translator.t(
                        guild.id, "updater.logs.updating.title"
                    ),
                    await log_description(
                        self.bot,
                        guild.id,
                        "updater.logs.updating.description",
                        **variables,
                    ),
                    color="warning",
                    card_key="updater.logs.updating.description",
                )
            except Exception as exc:
                log.error("Pre-update notify failed in %s: %s", guild.id, exc)


def _suppress_os():
    import contextlib

    return contextlib.suppress(OSError)


def setup(bot) -> None:
    bot.add_cog(UpdaterCog(bot))
