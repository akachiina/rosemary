"""Utility commands: ``/ping`` and ``/serverinfo`` (Components V2 cards).

Both are first-class CardSpecs (``utility.ping`` / ``utility.serverinfo``):
a theme may restyle them, and ``debug.card_paths`` traces the path like any
other send. The default layout of the server card lives in the default
package (``themes/default/cards/content.yaml``); the code below is only the
command. ``!ping`` is the prefix alias of ``/ping``.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from rosemary.core.card_service import render_card_message, simple_payload
from rosemary.ui.responses import PrefixContext


class UtilityCog(commands.Cog):
    """Latency and server diagnostics."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @discord.slash_command(
        name="ping",
        description="Show the bot latency",
        contexts={discord.InteractionContextType.guild},
    )
    async def ping(self, ctx: discord.ApplicationContext) -> None:
        """Show the gateway latency."""
        latency = getattr(self.bot, "latency", None)
        ms = f"{latency * 1000:.0f}" if latency is not None else "-"
        payload, _allowed = await render_card_message(
            self.bot, ctx.guild_id, "utility.ping", {"ms": ms}
        )
        if payload is None:
            payload = simple_payload(
                self.bot,
                ctx.guild_id,
                "utility.ping",
                title=await self.bot.translator.t(ctx.guild_id, "ping.title"),
                body=await self.bot.translator.t(ctx.guild_id, "ping.latency", ms=ms),
                color="info",
            )
        await ctx.respond(**payload.message_kwargs(), ephemeral=True)

    @commands.command(name="ping")
    async def ping_prefix(self, ctx: commands.Context) -> None:
        """Prefix alias: !ping"""
        await self.ping(PrefixContext(ctx))


