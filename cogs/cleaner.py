"""Message cleaner: keyword purge across readable text channels.

Scans every text channel the bot can read, deletes messages containing the
keyword (bulk delete under 14 days, single delete above), and reports progress
through ephemeral followups. Destructive, so it always asks for confirmation.
"""

import asyncio
import logging
from datetime import UTC, datetime

import discord
from discord.ext import commands

from rosemary.core.cards import log_description, text_or
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting
from rosemary.ui.containers import TextDisplay, designer_container
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)


class CleanerCog(commands.Cog):
    """Keyword-based message purge."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @discord.slash_command(
        name="limpar",
        description="Delete messages containing a word",
        default_member_permissions=discord.Permissions(manage_messages=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def limpar(
        self,
        ctx: discord.ApplicationContext,
        palavra: discord.Option(str, description="Word to purge"),
    ) -> None:
        """Delete every message containing ``palavra`` (with confirmation)."""
        guild_id = ctx.guild_id
        if not await get_setting(self.bot.storage, guild_id, "cleaner.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "cleaner.error_disabled"),
                ephemeral=True,
            )
        if not palavra.strip():
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "cleaner.error_empty"),
                ephemeral=True,
            )

        t = self.bot.translator.t
        view = MenuView(author_id=ctx.author.id)
        cog = self

        async def confirm(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            view.clear_items()
            view.add_item(
                TextDisplay(await t(guild_id, "cleaner.progress", word=palavra))
            )
            await interaction.edit(view=view)
            deleted = await cog._purge(guild_id, palavra.lower())
            view.clear_items()
            view.add_item(
                TextDisplay(
                    await text_or(
                        cog.bot,
                        guild_id,
                        "cleaner.result",
                        await t(guild_id, "cleaner.result", count=deleted, word=palavra),
                        count=deleted,
                        word=palavra,
                    )
                )
            )
            view.stop()
            await interaction.edit(view=view)
            await send_channel_log(
                cog.bot,
                guild_id,
                await t(guild_id, "cleaner.logs.purged.title"),
                await log_description(
                    cog.bot,
                    guild_id,
                    "cleaner.logs.purged.description",
                    moderator=ctx.author.mention,
                    word=palavra,
                    count=deleted,
                ),
                color="warning",
                card_key="cleaner.logs.purged.description",
                mention_user_ids=[ctx.author.id],
            )

        async def cancel(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            view.clear_items()
            view.add_item(TextDisplay(await t(guild_id, "cleaner.cancelled")))
            view.stop()
            await interaction.edit(view=view)

        view.register("cleaner_yes", confirm)
        view.register("cleaner_no", cancel)
        view.add_item(
            designer_container(
                self.bot.theme.color("warning"),
                TextDisplay(
                    self.bot.theme.md(
                        "title",
                        title=await t(guild_id, "cleaner.confirm_title", word=palavra),
                    )
                ),
                TextDisplay(
                    await text_or(
                        self.bot,
                        guild_id,
                        "cleaner.confirm",
                        await t(guild_id, "cleaner.confirm_text"),
                    )
                ),
            )
        )
        view.add_item(
            discord.ui.ActionRow(
                view.make_button(
                    custom_id="cleaner_yes",
                    label=await t(guild_id, "cleaner.confirm"),
                    style=discord.ButtonStyle.danger,
                ),
                view.make_button(
                    custom_id="cleaner_no",
                    label=await t(guild_id, "cleaner.cancel"),
                    style=discord.ButtonStyle.secondary,
                ),
            )
        )
        await ctx.respond(view=view, ephemeral=True)

    async def _purge(self, guild_id: int, word: str) -> int:
        """Delete matching messages; returns the deleted count."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return 0
        cutoff = datetime.now(UTC).timestamp() - 14 * 86400
        deleted = 0
        for channel in guild.text_channels:
            permissions = channel.permissions_for(guild.me)
            if not (permissions.read_messages and permissions.manage_messages):
                continue
            try:
                batch: list[discord.Message] = []
                async for message in channel.history(limit=None):
                    if word not in (message.content or "").lower():
                        continue
                    if message.created_at.timestamp() >= cutoff:
                        batch.append(message)
                        if len(batch) >= 100:
                            with _suppress():
                                await channel.delete_messages(batch)
                                deleted += len(batch)
                            batch = []
                    else:
                        with _suppress():
                            await message.delete()
                            deleted += 1
                if batch:
                    with _suppress():
                        await channel.delete_messages(batch)
                        deleted += len(batch)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Purge failed in channel %s: %s", channel.id, exc)
            await asyncio.sleep(0)
        return deleted


def _suppress():
    import contextlib

    return contextlib.suppress(discord.Forbidden, discord.HTTPException)


def setup(bot) -> None:
    bot.add_cog(CleanerCog(bot))
