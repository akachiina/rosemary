"""Birthday tracking with a daily announcement card.

Members register their date via /birthday (localized as /aniversario); a
per-guild minute loop compares local time — resolved from the guild timezone
setting — against ``birthdays.announce_time`` and posts one card per
celebrant, granting the configured role for the day. The announcement is a
customizable card (``birthdays.announce``) edited through /customize.
"""

from __future__ import annotations

import logging
from datetime import datetime

import discord
import discord.ext.tasks as tasks
from discord.ext import commands

from rosemary.core.birthdays import BirthdaysStore, validate_date
from rosemary.core.cards import ECHO_VARIABLES, maybe_text, maybe_view, set_default_builder
from rosemary.core.settings import get_setting
from rosemary.core.timezone import resolve_timezone

log = logging.getLogger(__name__)


class BirthdayCog(commands.Cog):
    """/birthday commands plus the daily announcement loop."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = BirthdaysStore(bot.storage.data_dir)

    async def start(self) -> None:
        self._loop.start()

    def cog_unload(self) -> None:
        self._loop.cancel()

    @tasks.loop(minutes=1)
    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        for guild in list(self.bot.guilds):
            try:
                await self.check_guild(guild)
            except Exception as exc:  # keep the loop alive per guild
                log.error("Birthday check failed in %s: %s", guild.id, exc)

    @_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    # -- helpers -------------------------------------------------------------

    async def check_guild(self, guild: discord.Guild, *, now: datetime | None = None) -> None:
        if not await get_setting(self.bot.storage, guild.id, "birthdays.enabled"):
            return
        announce_time = await get_setting(
            self.bot.storage, guild.id, "birthdays.announce_time"
        )
        try:
            hour_raw, minute_raw = str(announce_time).split(":")
            target_hour, target_minute = int(hour_raw), int(minute_raw)
        except ValueError:
            target_hour, target_minute = 12, 0

        now = now or datetime.now()
        tz = resolve_timezone(await get_setting(self.bot.storage, guild.id, "general.timezone"))
        local = now.astimezone(tz)
        if (local.hour, local.minute) != (target_hour, target_minute):
            return

        channel_id = await get_setting(self.bot.storage, guild.id, "birthdays.channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return

        await self.announce_today(guild, channel, local.date())

    async def announce_today(self, guild: discord.Guild, channel, today) -> None:
        role = None
        role_id = await get_setting(self.bot.storage, guild.id, "birthdays.role")
        if role_id:
            role = guild.get_role(role_id)

        if role is not None:
            for member in list(role.members):
                entry = await self.store.get(guild.id, member.id)
                is_today = bool(
                    entry and entry["day"] == today.day and entry["month"] == today.month
                )
                if not is_today:
                    with asyncio_exception_guard("role remove"):
                        await member.remove_roles(role, reason="Birthday over")

        year = today.year
        celebrants = [
            user_id
            for user_id, entry in (await self.store.all(guild.id)).items()
            if entry["day"] == today.day
            and entry["month"] == today.month
            and entry.get("last_announced_year") != year
        ]
        if not celebrants:
            return

        for user_id in celebrants:
            member = guild.get_member(user_id)
            variables = {
                "user": f"<@{user_id}>",
                "server": guild.name,
                "count": len(celebrants),
            }
            view = (
                await maybe_view(self.bot, guild.id, "birthdays.announce", variables)
                or None
            )
            if view is None:
                text = await maybe_text(self.bot, guild.id, "birthdays.announce", **variables)
                view = await self._default_card(guild.id, text, variables)
            from rosemary.core.mentions import mentions_for

            await channel.send(
                view=view,
                allowed_mentions=await mentions_for(
                    self.bot, guild.id, "birthdays.announce", user_ids=[user_id],
                ),
            )

            if role is not None and member is not None:
                with asyncio_exception_guard("role add"):
                    await member.add_roles(role, reason="Happy birthday!")
            await self.store.mark_announced(guild.id, user_id, year)

    async def _default_card(self, guild_id: int, override_text: str | None, variables: dict):
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        theme = self.bot.theme
        view = DesignerView(store=False)
        body = (
            override_text
            if override_text is not None
            else await self.bot.translator.t(
                guild_id, "birthdays.card.body", **variables
            )
        )
        title = await self.bot.translator.t(guild_id, "birthdays.card.title")
        view.add_item(
            designer_container(
                theme.color("warning"),
                TextDisplay(theme.md("title", title=title)),
                TextDisplay(body),
            )
        )
        return view

    # -- slash commands ------------------------------------------------------

    birthday_group = discord.SlashCommandGroup(
        "birthday",
        description="Register and list birthdays",
        contexts={discord.InteractionContextType.guild},
    )

    @staticmethod
    def _is_admin(interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(permissions and permissions.administrator)

    @birthday_group.command(name="set")
    async def set_birthday(
        self,
        ctx: discord.ApplicationContext,
        day: discord.Option(int, "Day of the month", min_value=1, max_value=31),
        month: discord.Option(int, "Month number", min_value=1, max_value=12),
        member: discord.Option(discord.Member, "Another member (admin only)", required=False),
    ) -> None:
        """Register a birthday; admins may set other members."""
        t = self.bot.translator.t
        target = member or ctx.author
        is_admin = self._is_admin(ctx.interaction)

        if member is not None and member.id != ctx.author.id and not is_admin:
            return await ctx.respond(
                await t(ctx.guild_id, "birthdays.errors.admin_only"), ephemeral=True
            )

        error_code = validate_date(day, month)
        if error_code == "bad_month":
            return await ctx.respond(
                await t(ctx.guild_id, "birthdays.errors.bad_month"), ephemeral=True
            )
        if error_code == "bad_day":
            return await ctx.respond(
                await t(ctx.guild_id, "birthdays.errors.bad_day"), ephemeral=True
            )

        cooldown_days = await get_setting(
            self.bot.storage, ctx.guild_id, "birthdays.change_cooldown_days"
        )
        existing = await self.store.get(ctx.guild_id, target.id)
        if not is_admin and existing and cooldown_days > 0:
            elapsed_days = (datetime.now().timestamp() - existing["last_changed"]) / 86400
            remaining = int(cooldown_days - elapsed_days) + 1
            if remaining > 0:
                return await ctx.respond(
                    await t(
                        ctx.guild_id,
                        "birthdays.errors.cooldown",
                        days=remaining,
                    ),
                    ephemeral=True,
                )

        await self.store.set(ctx.guild_id, target.id, day=day, month=month)
        await ctx.respond(
            await t(
                ctx.guild_id,
                "birthdays.set",
                user=target.mention,
                date=f"{day:02d}/{month:02d}",
            ),
            ephemeral=True,
        )

    @birthday_group.command(name="list")
    async def list_birthdays(self, ctx: discord.ApplicationContext) -> None:
        """List all registered birthdays grouped by month."""
        t = self.bot.translator.t
        entries = sorted(
            (await self.store.all(ctx.guild_id)).items(),
            key=lambda item: (item[1]["month"], item[1]["day"]),
        )
        months: list[str] = (await t(ctx.guild_id, "birthdays.months")).split(",")
        blocks: list[str] = []
        current_month = None
        lines: list[str] = []
        for user_id, entry in entries:
            if entry["month"] != current_month:
                if lines:
                    blocks.append("\n".join(lines))
                current_month = entry["month"]
                label = months[current_month - 1] if current_month <= len(months) else "?"
                lines = [f"**{label}**"]
            lines.append(f"- {entry['day']:02d}: <@{user_id}>")
        if lines:
            blocks.append("\n".join(lines))

        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        theme = self.bot.theme
        view = DesignerView(store=False)
        container_items: list[discord.ui.ViewItem] = [
            TextDisplay(theme.md("title", title=await t(ctx.guild_id, "birthdays.list_title")))
        ]
        if blocks:
            container_items.extend(TextDisplay(block) for block in blocks)
        else:
            container_items.append(TextDisplay(await t(ctx.guild_id, "birthdays.list_empty")))
        view.add_item(designer_container(theme.color("info"), *container_items))
        await ctx.respond(view=view, ephemeral=True)

    @birthday_group.command(name="clear")
    async def clear_birthday(self, ctx: discord.ApplicationContext) -> None:
        """Remove your own birthday."""
        removed = await self.store.clear(ctx.guild_id, ctx.author.id)
        key = "birthdays.cleared" if removed else "birthdays.not_found"
        await ctx.respond(
            await self.bot.translator.t(ctx.guild_id, key), ephemeral=True
        )


def asyncio_exception_guard(what: str):
    """Suppress Forbidden/HTTPException from Discord role operations."""
    import contextlib

    return contextlib.suppress(discord.Forbidden, discord.HTTPException)



async def default_announce_document(bot, guild_id: int) -> dict:
    """Catalog-default birthday announcement as an editable block document."""
    from rosemary.core.cards import safe_format

    mapping = {**bot.theme.emojis, **ECHO_VARIABLES}
    title = safe_format(
        await bot.translator.raw(guild_id, "birthdays.card.title"), mapping
    )
    body = safe_format(
        await bot.translator.raw(guild_id, "birthdays.card.body"), mapping
    )
    return {
        "v": 1,
        "blocks": [
            {"type": "container", "color": "warning", "children": [
                {"type": "text", "body": f"# {title}"},
                {"type": "text", "body": body},
            ]}
        ],
    }


set_default_builder("birthdays.announce", default_announce_document)

