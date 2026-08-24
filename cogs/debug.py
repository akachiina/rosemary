"""The ``/debug`` command — a diagnostics panel with a log-channel test button."""

import logging

import discord
from discord.ext import commands

from rosemary.core.debug import bot_state_summary, send_channel_log
from rosemary.ui.containers import (
    ActionRow,
    TextDisplay,
    designer_container,
)
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)


class DebugMenuView(MenuView):
    """Ephemeral diagnostics panel: bot state summary + log test button."""

    def __init__(self, bot, guild_id: int, *, owner_id: int | None = None) -> None:
        super().__init__(author_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.flash: str | None = None
        self.flash_color: str | None = None
        self.register("debug_close", self._close)
        self.register("debug_test_log", self._test_log)

    async def _close(self, interaction: discord.Interaction) -> None:
        self.disable_all_items()
        await interaction.edit(view=self)
        self.stop()

    async def _test_log(self, interaction: discord.Interaction) -> None:
        t = self.bot.translator.t
        sent = await send_channel_log(
            self.bot,
            self.guild_id,
            await t(self.guild_id, "debug.test_log_title"),
            await t(self.guild_id, "debug.test_log_description"),
        )
        self.flash = await t(
            self.guild_id,
            "debug.test_log_sent" if sent else "debug.test_log_not_sent",
        )
        self.flash_color = "success" if sent else "danger"
        await self.rerender(interaction)

    async def build_items(self) -> list[discord.ui.ViewItem]:
        t = self.bot.translator.t
        container_parts = [
            TextDisplay(
                self.bot.theme.md(
                    "title", title=await t(self.guild_id, "debug.title")
                )
            )
        ]
        for key, variables in bot_state_summary(self.bot):
            container_parts.append(TextDisplay(await t(self.guild_id, key, **variables)))
        flash_color = self.flash_color or "info"
        if self.flash:
            container_parts.append(TextDisplay(self.flash))
            self.flash = None
            self.flash_color = None
        container = designer_container(
            self.bot.theme.color(flash_color), *container_parts
        )

        close = self.make_button(
            custom_id="debug_close",
            label=await t(self.guild_id, "debug.close"),
            style=discord.ButtonStyle.danger,
        )
        test_log = self.make_button(
            custom_id="debug_test_log",
            label=await t(self.guild_id, "debug.test_log"),
        )
        return [container, ActionRow(close, test_log)]


class DebugCog(commands.Cog):
    """Bot diagnostics."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @discord.slash_command(
        name="debug",
        description="Bot diagnostics panel",
        default_member_permissions=discord.Permissions(manage_guild=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def debug(self, ctx: discord.ApplicationContext) -> None:
        """Open an ephemeral diagnostics panel."""
        view = DebugMenuView(self.bot, ctx.guild_id, owner_id=ctx.author.id)
        await view.prepare()
        await ctx.respond(view=view, ephemeral=True)
