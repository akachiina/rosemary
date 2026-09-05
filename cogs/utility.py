"""Utility commands: ``/ping`` and ``/serverinfo`` (Components V2 cards)."""

from __future__ import annotations

import discord
from discord.ext import commands

from rosemary.ui.containers import (
    DesignerView,
    TextDisplay,
    designer_container,
)


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
        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("info"),
                TextDisplay(
                    self.bot.theme.md(
                        "title",
                        title=await self.bot.translator.t(ctx.guild_id, "ping.title"),
                    )
                ),
                TextDisplay(
                    await self.bot.translator.t(ctx.guild_id, "ping.latency", ms=ms)
                ),
            )
        )
        await ctx.respond(view=view, ephemeral=True)

    @discord.slash_command(
        name="serverinfo",
        description="Show server information",
        contexts={discord.InteractionContextType.guild},
    )
    async def serverinfo(self, ctx: discord.ApplicationContext) -> None:
        """Show guild information."""
        t = self.bot.translator.t
        guild = ctx.guild
        created = f"<t:{int(guild.created_at.timestamp())}:D>" if guild.created_at else "-"
        rows = "\n".join(
            [
                await t(ctx.guild_id, "serverinfo.name", value=guild.name),
                await t(ctx.guild_id, "serverinfo.owner", value=str(guild.owner)),
                await t(
                    ctx.guild_id, "serverinfo.members", value=guild.member_count or 0
                ),
                await t(ctx.guild_id, "serverinfo.roles", value=len(guild.roles)),
                await t(
                    ctx.guild_id,
                    "serverinfo.channels",
                    value=len(guild.text_channels) + len(guild.voice_channels),
                ),
                await t(ctx.guild_id, "serverinfo.created", value=created),
            ]
        )
        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("brand"),
                TextDisplay(
                    self.bot.theme.md(
                        "title",
                        title=await t(ctx.guild_id, "serverinfo.title"),
                    )
                ),
                TextDisplay(rows),
            )
        )
        await ctx.respond(view=view, ephemeral=True)


def setup(bot) -> None:
    bot.add_cog(UtilityCog(bot))
