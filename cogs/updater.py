"""Self-update: check, confirm, back up, reset and restart.

Admin-only and disabled by default. Requires a clean working tree (local
changes are never overwritten blindly). After restarting, guilds that had
logging enabled are notified through their log channels.
"""

import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands

from rosemary.core.cards import log_description, text_or
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting
from rosemary.core.updater import backup_data, compare, fetch, is_clean, prune_backups, reset_hard
from rosemary.ui.containers import TextDisplay
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)


class UpdaterCog(commands.Cog):
    """Git-based self update with backup and restart."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._started = False

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
        """Notify log channels once after an update restart (then clear)."""
        if self._started:
            return
        self._started = True
        flag = self._pending_flag
        if not flag.exists():
            return
        try:
            guild_ids = [
                int(line) for line in flag.read_text().splitlines() if line.strip().isdigit()
            ]
        except OSError:
            guild_ids = []
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
                    ),
                    color="success",
                    card_key="updater.logs.updated.description",
                )
            except Exception as exc:
                log.error("Post-update notify failed in %s: %s", guild_id, exc)

    @discord.slash_command(
        name="update",
        description="[ADMIN] Update the bot from git",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def update(self, ctx: discord.ApplicationContext) -> None:
        """Check for updates and, on confirmation, apply them and restart."""
        guild_id = ctx.guild_id
        t = self.bot.translator.t
        if not await get_setting(self.bot.storage, guild_id, "updater.enabled"):
            return await ctx.respond(
                await t(guild_id, "updater.error_disabled"), ephemeral=True
            )
        branch = await get_setting(self.bot.storage, guild_id, "updater.branch")
        await ctx.response.defer(ephemeral=True)
        if not await fetch(self._repo, branch):
            return await ctx.respond(
                await t(guild_id, "updater.error_fetch"), ephemeral=True
            )
        behind, ahead = await compare(self._repo, branch)
        if behind == 0:
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
            if not await reset_hard(cog._repo, branch):
                view.clear_items()
                view.add_item(TextDisplay(await t(guild_id, "updater.error_reset")))
                view.stop()
                return await interaction.edit(view=view)
            await cog._notify_all(
                moderator=ctx.author.mention,
                branch=branch,
            )
            try:
                cog._pending_flag.write_text(
                    "\n".join(str(g.id) for g in cog.bot.guilds)
                )
            except OSError as exc:
                log.warning("Could not write update flag: %s", exc)
            log.info("Restarting for update (backup at %s)", backup)
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
                    await t(guild_id, "updater.confirm", behind=behind, branch=branch),
                    behind=behind,
                    branch=branch,
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
