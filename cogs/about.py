"""The ``/about`` command: bot information rendered with Components V2."""

from __future__ import annotations

import discord
from discord.ext import commands

from rosemary import __version__
from rosemary.ui.containers import (
    DesignerView,
    TextDisplay,
    designer_container,
    divider,
)


class AboutCog(commands.Cog):
    """Bot information."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @discord.slash_command(
        name="about",
        description="About Rosemary",
    )
    async def about(self, ctx: discord.ApplicationContext) -> None:
        """Show information about Rosemary."""
        t = self.bot.translator.t
        # Sections require an accessory (Thumbnail/Button); plain text must use
        # TextDisplay directly to keep the message valid.
        container = designer_container(
            self.bot.theme.color("brand"),
            TextDisplay(
                self.bot.theme.md(
                    "title", title=await t(ctx.guild_id, "about.title")
                )
            ),
            TextDisplay(await t(ctx.guild_id, "about.text")),
            divider(),
            TextDisplay(await t(ctx.guild_id, "about.version", version=__version__)),
        )
        view = DesignerView(store=False)
        view.add_item(container)
        await ctx.respond(view=view)
