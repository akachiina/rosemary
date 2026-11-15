"""``/convert file``: images, video, audio, documents and sticker files.

The command takes one attachment and opens an ephemeral card with one select
per export category. The conversion runs in a worker thread; the result is an
ephemeral card with the converted file attached (the sticker target is a
plain 320x320 file under 512 KB the member can upload as a sticker).
"""

import discord
from discord.ext import commands

from rosemary.core.convert import kind_of
from rosemary.core.host import HostConfig, load_host_config
from rosemary.core.localized_group import LocalizedSlashCommandGroup
from rosemary.core.sizes import human_size
from rosemary.ui import runtime
from rosemary.ui.convert_panel import ConvertPanel


class ConvertCog(commands.Cog):
    """File conversion commands."""

    convert_group = LocalizedSlashCommandGroup(
        "convert",
        description="Convert an attached file",
        contexts={discord.InteractionContextType.guild},
    )

    def __init__(self, bot, host: HostConfig | None = None) -> None:
        self.bot = bot
        self.host = host or load_host_config()

    @convert_group.command(
        name="file",
        description="Convert an attached file",
    )
    async def convert_file(
        self,
        ctx: discord.ApplicationContext,
        attachment: discord.Option(
            discord.Attachment, description="File to convert"
        ),
    ) -> None:
        """Open the format picker for one attachment."""
        guild = ctx.guild
        if guild is None:
            return
        t = self.bot.translator.t
        size = int(getattr(attachment, "size", 0) or 0)
        if size > self.host.convert_max_mb * 1024 * 1024:
            return await ctx.respond(
                await t(
                    guild.id,
                    "convert.too_large",
                    size=human_size(size),
                    max_mb=self.host.convert_max_mb,
                ),
                ephemeral=True,
            )
        kind = kind_of(attachment.filename)
        if kind is None:
            return await ctx.respond(
                await t(guild.id, "convert.bad_type"), ephemeral=True
            )
        try:
            data = await attachment.read()
        except (discord.Forbidden, discord.HTTPException):
            return await ctx.respond(
                await t(guild.id, "convert.error"), ephemeral=True
            )
        await ctx.response.defer(ephemeral=True)
        panel = ConvertPanel(
            self.bot, guild.id, data, attachment.filename, kind, self.host
        )
        await panel.open_screen()
        await runtime.open(panel, ctx.followup.send, ephemeral=True)
