"""The ``/settings`` command: per-guild configuration through an interactive menu."""

import discord
from discord.ext import commands

from rosemary.ui.settings_menu import SettingsMenuView


class SettingsCog(commands.Cog):
    """Server configuration."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @discord.slash_command(
        name="settings",
        description="Configure server settings",
        default_member_permissions=discord.Permissions(manage_guild=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def settings(self, ctx: discord.ApplicationContext) -> None:
        """Open the interactive settings menu (ephemeral)."""
        view = SettingsMenuView(self.bot, ctx.guild_id, owner_id=ctx.author.id)
        await view.prepare()
        await ctx.respond(view=view, ephemeral=True)
