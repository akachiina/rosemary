"""The single ``/personalizar`` entry point for the card editor."""

import discord
from discord.ext import commands

from rosemary.ui.customize_menu import CustomizeMenuView


class CustomizeCog(commands.Cog):
    """The single ``/personalizar`` entry point (decorator name ``customize``).

    The base name stays ``customize`` because per-guild localization reads the
    ``customize.command.name``/``.description`` catalog keys; no group, no
    subcommands — everything else lives in the picker and the shared editor.
    """

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
        if not ctx.response.is_done():
            await ctx.response.defer(ephemeral=True)
        view = CustomizeMenuView(self.bot, ctx.guild_id, owner_id=ctx.author.id)
        await view.prepare()
        await ctx.respond(view=view, ephemeral=True)
