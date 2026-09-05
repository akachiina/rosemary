"""Canned reminders: save named messages, fan them out as DMs on demand.

Admin-only and disabled by default. Sending is paced (one DM at a time with
a configurable delay) and reports delivered/failed counts to the requester.
"""

import asyncio
import logging

import discord
from discord.ext import commands

from rosemary.core.cards import log_description
from rosemary.core.debug import send_channel_log
from rosemary.core.reminders import ReminderStore
from rosemary.core.settings import get_setting

log = logging.getLogger(__name__)


class RemindersCog(commands.Cog):
    """Named DM reminders."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = ReminderStore(bot.storage.data_dir)

    async def _guard(self, guild_id: int) -> bool:
        return bool(await get_setting(self.bot.storage, guild_id, "reminders.enabled"))

    @discord.slash_command(
        name="reminders_save",
        description="[ADMIN] Save a reminder message",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def reminders_save(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(str, description="Short name"),
        message: discord.Option(str, description="Message text"),
    ) -> None:
        """Save a named reminder message."""
        if not await self._guard(ctx.guild_id):
            return await ctx.respond(
                await self.bot.translator.t(ctx.guild_id, "reminders.error_disabled"),
                ephemeral=True,
            )
        await self.store.save(ctx.guild_id, name, message)
        await ctx.respond(
            await self.bot.translator.t(
                ctx.guild_id, "reminders.saved", name=name.strip().lower()
            ),
            ephemeral=True,
        )

    @discord.slash_command(
        name="reminders_delete",
        description="[ADMIN] Delete a reminder message",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def reminders_delete(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(str, description="Short name"),
    ) -> None:
        """Delete a named reminder message."""
        if not await self._guard(ctx.guild_id):
            return await ctx.respond(
                await self.bot.translator.t(ctx.guild_id, "reminders.error_disabled"),
                ephemeral=True,
            )
        if await self.store.delete(ctx.guild_id, name):
            key = "reminders.deleted"
        else:
            key = "reminders.not_found"
        await ctx.respond(
            await self.bot.translator.t(
                ctx.guild_id, key, name=name.strip().lower()
            ),
            ephemeral=True,
        )

    @discord.slash_command(
        name="reminders_list",
        description="[ADMIN] List reminder messages",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def reminders_list(self, ctx: discord.ApplicationContext) -> None:
        """List saved reminder names."""
        if not await self._guard(ctx.guild_id):
            return await ctx.respond(
                await self.bot.translator.t(ctx.guild_id, "reminders.error_disabled"),
                ephemeral=True,
            )
        names = sorted((await self.store.all(ctx.guild_id)).keys())
        key = "reminders.list_empty" if not names else "reminders.list_title"
        await ctx.respond(
            await self.bot.translator.t(
                ctx.guild_id, key, names="\n".join(f"- {name}" for name in names)
            ),
            ephemeral=True,
        )

    @discord.slash_command(
        name="reminders_send",
        description="[ADMIN] DM a reminder to every member",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def reminders_send(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(str, description="Short name"),
    ) -> None:
        """Fan out a saved reminder to every non-bot member."""
        guild_id = ctx.guild_id
        if not await self._guard(guild_id):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "reminders.error_disabled"),
                ephemeral=True,
            )
        message = await self.store.get(guild_id, name)
        if not message:
            return await ctx.respond(
                await self.bot.translator.t(
                    guild_id, "reminders.not_found", name=name.strip().lower()
                ),
                ephemeral=True,
            )
        await ctx.response.defer(ephemeral=True)
        delay = await get_setting(self.bot.storage, guild_id, "reminders.delay_seconds")
        sent, failed = 0, 0
        for member in list(ctx.guild.members):
            if member.bot:
                continue
            try:
                await member.send(message)
                sent += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
            await asyncio.sleep(max(delay, 0))
        await send_channel_log(
            self.bot,
            guild_id,
            await self.bot.translator.t(guild_id, "reminders.logs.sent.title"),
            await log_description(
                self.bot,
                guild_id,
                "reminders.logs.sent.description",
                moderator=ctx.author.mention,
                name=name.strip().lower(),
                sent=sent,
                failed=failed,
            ),
            color="info",
            card_key="reminders.logs.sent.description",
            mention_user_ids=[ctx.author.id],
        )
        await ctx.respond(
            await self.bot.translator.t(
                guild_id, "reminders.done", sent=sent, failed=failed
            ),
            ephemeral=True,
        )


def setup(bot) -> None:
    bot.add_cog(RemindersCog(bot))
