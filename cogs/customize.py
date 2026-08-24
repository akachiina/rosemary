"""The ``/customize`` command: edit every customizable bot message."""

import discord
from discord.ext import commands

from rosemary.ui.customize_menu import CustomizeMenuView


class CustomizeCog(commands.Cog):
    """Entry point for the message composer (localized as /personalizar)."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @discord.slash_command(
        name="customize",
        description="Customize the bot's messages for this server",
        default_member_permissions=discord.Permissions(manage_guild=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def customize(self, ctx: discord.ApplicationContext) -> None:
        """Open the card picker (ephemeral)."""
        view = CustomizeMenuView(self.bot, ctx.guild_id, owner_id=ctx.author.id)
        await view.prepare()
        await ctx.respond(view=view, ephemeral=True)


def setup(bot) -> None:
    bot.add_cog(CustomizeCog(bot))
