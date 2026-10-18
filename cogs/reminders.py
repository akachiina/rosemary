"""Canned reminders: save named messages, fan them out as DMs on demand.

Admin-only and disabled by default. Sending is paced (one DM at a time with
a configurable delay) and reports delivered/failed counts to the requester.
"""

import logging

import discord
from discord.ext import commands

from rosemary.core.cards import log_description
from rosemary.core.command_guards import require_admin
from rosemary.core.debug import send_channel_log
from rosemary.core.reminders import ReminderStore
from rosemary.core.settings import get_setting

log = logging.getLogger(__name__)


class RemindersCog(commands.Cog):
    """Named DM reminders."""


    reminders_group = discord.SlashCommandGroup(
        "reminders",
        description="Manage reminders",
        contexts={discord.InteractionContextType.guild},
        # Every subcommand is admin-only. The API reads the restriction only
        # on the top-level command, so declare it here for real client-side
        # hiding and pair it with require_admin() in each handler.
        default_member_permissions=discord.Permissions(administrator=True),
    )
    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = ReminderStore(bot.storage.data_dir)

    async def _guard(self, guild_id: int) -> bool:
        return bool(await get_setting(self.bot.storage, guild_id, "reminders.enabled"))

    @reminders_group.command(
        name="save",
        description="[ADMIN] Save a reminder message",
        default_member_permissions=discord.Permissions(administrator=True),
    )
    async def reminders_save(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(str, description="Short name"),
        message: discord.Option(str, description="Message text"),
    ) -> None:
        """Save a named reminder message."""
        if not await require_admin(ctx):
            return
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

    @reminders_group.command(
        name="delete",
        description="[ADMIN] Delete a reminder message",
        default_member_permissions=discord.Permissions(administrator=True),
    )
    async def reminders_delete(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(str, description="Short name"),
    ) -> None:
        """Delete a named reminder message."""
        if not await require_admin(ctx):
            return
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

    @reminders_group.command(
        name="list",
        description="[ADMIN] List reminder messages",
        default_member_permissions=discord.Permissions(administrator=True),
    )
    async def reminders_list(self, ctx: discord.ApplicationContext) -> None:
        """List saved reminder names as a themed card."""
        if not await require_admin(ctx):
            return
        if not await self._guard(ctx.guild_id):
            return await ctx.respond(
                await self.bot.translator.t(ctx.guild_id, "reminders.error_disabled"),
                ephemeral=True,
            )
        names = sorted((await self.store.all(ctx.guild_id)).keys())
        if not names:
            return await ctx.respond(
                await self.bot.translator.t(ctx.guild_id, "reminders.list_empty"),
                ephemeral=True,
            )
        from rosemary.core.card_service import CardPayload, render_card_message
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        t = self.bot.translator.t
        title = await t(ctx.guild_id, "reminders.list_title")
        body = "\n".join(f"- {name}" for name in names)
        payload, _allowed = await render_card_message(
            self.bot, ctx.guild_id, "reminders.list", {"title": title, "body": body}
        )
        if payload is None:
            view = DesignerView(store=False)
            view.add_item(
                designer_container(
                    self.bot.theme.color("info"),
                    TextDisplay(self.bot.theme.md("title", title=title)),
                    TextDisplay(body),
                )
            )
            payload = CardPayload(view=view)
        await ctx.respond(**payload.message_kwargs(), ephemeral=True)

    @reminders_group.command(
        name="send",
        description="[ADMIN] DM a reminder to every member",
        default_member_permissions=discord.Permissions(administrator=True),
    )
    async def reminders_send(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(str, description="Short name"),
    ) -> None:
        """Fan out a saved reminder to every non-bot member."""
        if not await require_admin(ctx):
            return
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
        from rosemary.core.mass_dm import fan_out, with_role

        delay = await get_setting(self.bot.storage, guild_id, "reminders.delay_seconds")
        target = await get_setting(self.bot.storage, guild_id, "reminders.target_role")
        members = with_role(ctx.guild.members, target)
        sent, failed = await fan_out(members, message, delay)
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

