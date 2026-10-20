"""The ``/debug`` command: a card-based diagnostics panel with a log test."""

from __future__ import annotations

from datetime import UTC, datetime

import discord
from discord.ext import commands

import rosemary.core.debug_cards  # noqa: F401  (registers the panel builder)
from rosemary import __version__
from rosemary.core.card_service import (
    default_document,
    get_effective_document,
    trace_card_path,
)
from rosemary.core.cards import (
    CardsError,
    is_embed_document,
)
from rosemary.core.cards import (
    build_items as build_card_items,
)
from rosemary.core.debug import bot_state_summary, format_uptime, send_log
from rosemary.core.settings import get_setting
from rosemary.core.themes import theme_for
from rosemary.ui.containers import (
    ActionRow,
    TextDisplay,
    designer_container,
)
from rosemary.ui.menu import MenuView


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
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        sent = await send_log(self.bot, self.guild_id, "debug.logs.test")
        t = self.bot.translator.t
        self.flash = await t(
            self.guild_id,
            "debug.test_log_sent" if sent else "debug.test_log_not_sent",
        )
        self.flash_color = "success" if sent else "danger"
        await self.rerender(interaction)

    async def _on_off(self, key: str) -> str:
        enabled = await get_setting(self.bot.storage, self.guild_id, key)
        return await self.bot.translator.t(
            self.guild_id, "settings.bool.true" if enabled else "settings.bool.false"
        )

    async def _variables(self) -> dict[str, object]:
        latency = getattr(self.bot, "latency", None)
        theme = theme_for(self.bot, self.guild_id)
        return {
            "guilds": len(self.bot.guilds),
            "members": sum(g.member_count or 0 for g in self.bot.guilds),
            "latency": f"{latency * 1000:.0f}" if latency is not None else "-",
            "uptime": format_uptime(getattr(self.bot, "start_time", None)),
            "version": __version__,
            "theme": getattr(theme, "name", "default"),
            "logging": await self._on_off("logging.enabled"),
            "audit": await self._on_off("audit.enabled"),
            "timestamp": discord.utils.format_dt(datetime.now(UTC), "R"),
            "flash": self.flash,
            "flash_color": self.flash_color,
        }

    async def build_items(self) -> list[discord.ui.ViewItem]:
        variables = await self._variables()
        doc = await get_effective_document(
            self.bot, self.guild_id, "debug.panel", variables
        )
        if doc is None or is_embed_document(doc):
            # Interactive menus need V2 components; an embed override cannot
            # carry the buttons, so keep the built-in card.
            doc = await default_document(
                self.bot, self.guild_id, "debug.panel", variables
            )
        try:
            items = build_card_items(
                theme_for(self.bot, self.guild_id),
                doc or {"v": 1, "blocks": []},
                variables,
                card_key="debug.panel",
            )
        except CardsError:
            items = []
        if not items:
            t = self.bot.translator.t
            parts = [TextDisplay(await t(self.guild_id, "debug.panel.title"))]
            for key, values in bot_state_summary(self.bot):
                parts.append(TextDisplay(await t(self.guild_id, key, **values)))
            items = [designer_container(self.bot.theme.color("info"), *parts)]

        t = self.bot.translator.t
        close = self.make_button(
            custom_id="debug_close",
            label=await t(self.guild_id, "debug.close"),
            style=discord.ButtonStyle.danger,
        )
        test_log = self.make_button(
            custom_id="debug_test_log",
            label=await t(self.guild_id, "debug.test_log"),
        )
        # The flash is part of the rendered document; clear it for the next
        # repaint after it has been shown once.
        self.flash = None
        self.flash_color = None
        return [*items, ActionRow(close, test_log)]


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
        # Trace after the ACK: the log send must never delay the panel response.
        await trace_card_path(self.bot, ctx.guild_id, "debug.panel")
