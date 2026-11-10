"""The ``/debug`` command: a card-based diagnostics panel with a log test."""

from __future__ import annotations

from datetime import UTC, datetime

import discord
from discord.ext import commands

from rosemary import __version__
from rosemary.core.card_service import (
    render_card_message,
    simple_payload,
)
from rosemary.core.debug import (
    bot_state_summary,
    format_uptime,
    send_log,
    trace_card_path,
)
from rosemary.core.settings import get_setting
from rosemary.core.themes import theme_for
from rosemary.ui.containers import ActionRow
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
        from rosemary.ui.runtime import close

        await close(self, interaction)

    async def _test_log(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
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
            # The flash is the feedback of the last action: an empty string
            # means "nothing to say", so the document drops the paragraph.
            "flash": self.flash or "",
            # The color token the state chooses (the theme owns its meaning).
            "flash_color": self.flash_color or "",
        }

    async def build_items(self) -> list[discord.ui.ViewItem]:
        variables = await self._variables()
        # The panel is a card: the document (the packaged one, or the theme's
        # own) owns the frame, and the code owns the buttons.
        payload, _allowed = await render_card_message(
            self.bot, self.guild_id, "debug.panel", variables
        )
        items: list[discord.ui.ViewItem] = []
        if payload is not None and payload.view is not None:
            items = list(payload.view.children)
        if not items:
            # No document at all (the catalog has no copy): the card fallback
            # answers in the card's own shape, never as a hand-built frame.
            t = self.bot.translator.t
            title = await t(self.guild_id, "debug.panel.title")
            body = "\n".join(
                [
                    await t(self.guild_id, key, **values)
                    for key, values in bot_state_summary(self.bot)
                ]
            )
            fallback = simple_payload(
                self.bot,
                self.guild_id,
                "debug.panel",
                title=title,
                body=body,
                color="info",
            )
            if fallback.view is not None:
                items = list(fallback.view.children)

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
