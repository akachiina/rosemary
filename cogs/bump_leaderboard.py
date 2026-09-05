"""Bump Leaderboard cog for Rosemary.

Handles the weekly leaderboard reset, posts the ranking, and assigns the Bump-MVP
role to the winner, restoring their previous role customizations.

Note: this file intentionally does NOT import ``from __future__ import
annotations``: py-cord inspects slash-command annotations with
``inspect.signature`` and stringified ``discord.Option(...)`` annotations would
break option parsing at runtime (same reason as ``cogs/moderation.py``).
"""

import contextlib
import logging
from datetime import UTC, datetime, timedelta, tzinfo

import discord
from discord.ext import commands, tasks

from rosemary.core.boost_roles import BoostRoleStore
from rosemary.core.bump import BumpStore
from rosemary.core.cards import log_description, maybe_view
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting
from rosemary.core.timezone import resolve_timezone
from rosemary.ui.boost_dm import send_preview

log = logging.getLogger(__name__)

#: Weekday names matching the ``bump.leaderboard.reset_day`` choices and
#: ``datetime.strftime("%A")``.
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

#: Fallback reset hour when a stored ``reset_hour`` is out of range.
_DEFAULT_RESET_HOUR = 20


def _next_reset(now: datetime, reset_day: str, reset_hour: int, tz: tzinfo = UTC) -> datetime:
    """Return the next scheduled weekly reset at or after ``now`` (UTC).

    ``reset_day``/``reset_hour`` are interpreted in the guild's timezone
    ``tz``; the returned datetime is normalized to UTC so ``.timestamp()``
    stays the absolute instant. Falls back to ``now + 7 days`` when
    ``reset_day`` is unknown.
    """
    local = now.astimezone(tz)
    try:
        target = WEEKDAYS.index(reset_day)
    except ValueError:
        return now + timedelta(days=7)
    hour = reset_hour if 0 <= reset_hour <= 23 else _DEFAULT_RESET_HOUR
    days_ahead = (target - local.weekday()) % 7
    candidate = (local + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


class BumpLeaderboardCog(commands.Cog):
    """Weekly bump leaderboard and MVP role management."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.bump_store = BumpStore(bot.storage.data_dir)
        self.boost_store = BoostRoleStore(bot.storage.data_dir)
        self._reset_task.start()

    def cog_unload(self) -> None:
        self._reset_task.cancel()

    # -- Scheduled Tasks -----------------------------------------------------

    @tasks.loop(minutes=1)
    async def _reset_task(self) -> None:
        await self.bot.wait_until_ready()
        now_utc = datetime.now(UTC)
        for guild in self.bot.guilds:
            try:
                if not await get_setting(self.bot.storage, guild.id, "bump.leaderboard.enabled"):
                    continue

                reset_day = await get_setting(
                    self.bot.storage, guild.id, "bump.leaderboard.reset_day",
                )
                reset_hour = await get_setting(
                    self.bot.storage, guild.id, "bump.leaderboard.reset_hour",
                )
                tz = resolve_timezone(
                    await get_setting(self.bot.storage, guild.id, "general.timezone")
                )
                now_local = datetime.now(tz)

                if (
                    now_local.strftime("%A") != reset_day
                    or now_local.hour != reset_hour
                    or now_local.minute != 0
                ):
                    continue

                week_start = await self.bump_store.get_week_start(guild.id)
                if week_start and (now_utc - week_start).total_seconds() < 86400:
                    # Already reset today
                    continue

                await self._process_weekly_reset(guild)
            except Exception as exc:
                log.error("Failed to process bump leaderboard reset for %s: %s", guild.id, exc)

    async def _process_weekly_reset(self, guild: discord.Guild) -> str:
        """Process the weekly reset and return a status string.

        Returns one of ``"no_bumps"``, ``"no_role"`` or ``"posted"`` so callers
        (e.g. the test command) can report what actually happened.
        """
        leaderboard = await self.bump_store.get_leaderboard(guild.id)
        channel_id = await get_setting(self.bot.storage, guild.id, "bump.channel")
        channel = guild.get_channel(channel_id) if channel_id else None

        if not leaderboard:
            if isinstance(channel, discord.TextChannel):
                await self._post_no_bumps(guild, channel)
            await self.bump_store.reset_week(guild.id)
            await send_channel_log(
                self.bot,
                guild.id,
                await self.bot.translator.t(guild.id, "bump.logs.no_winner.title"),
                await self.bot.translator.t(guild.id, "bump.logs.no_winner.description"),
                color="info",
                card_key="bump.logs.no_winner.description",
            )
            return "no_bumps"

        sorted_lb = sorted(
            [(int(uid), count) for uid, count in leaderboard.items()],
            key=lambda x: x[1],
            reverse=True,
        )
        new_winner_id = sorted_lb[0][0]
        winner_count = sorted_lb[0][1]

        role_id = await get_setting(self.bot.storage, guild.id, "bump.leaderboard.winner_role")
        role = guild.get_role(role_id) if role_id else None

        if not role:
            await send_channel_log(
                self.bot,
                guild.id,
                await self.bot.translator.t(guild.id, "bump.logs.no_winner.title"),
                await self.bot.translator.t(guild.id, "bump.error_role_not_found"),
                color="danger",
                card_key="bump.logs.no_winner.description",
            )
            await self.bump_store.reset_week(guild.id)
            return "no_role"

        old_winner_id = await self.bump_store.get_current_winner(guild.id)

        await self._transfer_mvp_role(guild, role, old_winner_id, new_winner_id)

        await self.bump_store.add_win(guild.id, new_winner_id)
        await self.bump_store.set_current_winner(guild.id, new_winner_id)

        if isinstance(channel, discord.TextChannel):
            await self._post_leaderboard(
                guild, channel, sorted_lb, new_winner_id, winner_count, role.name,
            )

        total_wins = await self.bump_store.get_total_wins(guild.id, new_winner_id)
        await self._dm_participants(
            guild, role, new_winner_id, old_winner_id, winner_count, total_wins,
        )

        await self.bump_store.reset_week(guild.id)
        await send_channel_log(
            self.bot,
            guild.id,
            await self.bot.translator.t(guild.id, "bump.logs.week_reset.title"),
            await log_description(
                self.bot,
                guild.id,
                "bump.logs.week_reset.description",
                winner=f"<@{new_winner_id}>",
                bumps=sum(c for _, c in sorted_lb),
            ),
            color="success",
            card_key="bump.logs.week_reset.description",
            mention_user_ids=[new_winner_id],
        )
        return "posted"

    # -- Helpers -------------------------------------------------------------

    async def _post_no_bumps(self, guild: discord.Guild, channel: discord.TextChannel) -> None:
        from rosemary.core.mentions import mentions_for

        override = await maybe_view(self.bot, guild.id, "bump.no_bumps")
        if override is not None:
            await channel.send(
                view=override,
                allowed_mentions=await mentions_for(self.bot, guild.id, "bump.no_bumps"),
            )
            return

        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        view = DesignerView(store=False)
        content = await self.bot.translator.t(guild.id, "bump.messages.no_bumps_description")
        no_bumps_title = await self.bot.translator.t(guild.id, "bump.messages.no_bumps_title")
        view.add_item(
            designer_container(
                self.bot.theme.color(self.bot.theme.style("bump_no_data").color),
                TextDisplay(self.bot.theme.md("title", title=no_bumps_title)),
                TextDisplay(content),
            )
        )
        await channel.send(
            view=view,
            allowed_mentions=await mentions_for(self.bot, guild.id, "bump.no_bumps"),
        )

    async def _post_leaderboard(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        sorted_lb: list[tuple[int, int]],
        winner_id: int,
        winner_count: int,
        role_name: str | None = None,
    ) -> None:
        from rosemary.core.mentions import mentions_for

        view = (
            await maybe_view(self.bot, guild.id, "bump.leaderboard")
            or await self._build_leaderboard_view(
                guild, sorted_lb, winner_id, winner_count, role_name,
            )
        )
        await channel.send(
            view=view,
            allowed_mentions=await mentions_for(
                self.bot, guild.id, "bump.leaderboard",
                source="auto", user_ids=[winner_id],
            ),
        )

    async def _build_leaderboard_view(
        self,
        guild: discord.Guild,
        sorted_lb: list[tuple[int, int]],
        winner_id: int,
        winner_count: int,
        role_name: str | None = None,
    ) -> discord.ui.DesignerView:
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        lines = []
        for i, (uid, count) in enumerate(sorted_lb[:10], start=1):
            medal = self.bot.theme.medals.get(str(i), self.bot.theme.medals.get("default", "📊"))
            line = await self.bot.translator.t(
                guild.id,
                "bump.messages.leaderboard_line",
                medal=medal,
                position=i,
                user_mention=f"<@{uid}>",
                bump_count=count,
            )
            lines.append(line)

        week_start = await self.bump_store.get_week_start(guild.id)
        start_ts = int(week_start.timestamp()) if week_start else int(datetime.now(UTC).timestamp())

        reset_day = await get_setting(
            self.bot.storage, guild.id, "bump.leaderboard.reset_day"
        )
        reset_hour = await get_setting(
            self.bot.storage, guild.id, "bump.leaderboard.reset_hour"
        )
        tz = resolve_timezone(
            await get_setting(self.bot.storage, guild.id, "general.timezone")
        )
        next_reset = _next_reset(datetime.now(UTC), reset_day, reset_hour, tz)
        end_ts = int(next_reset.timestamp())
        next_reset_ts = int(next_reset.timestamp())

        if not role_name:
            role_name = self.bot.theme.bump.get("mvp_default_name", "Bump-MVP")

        description = await self.bot.translator.t(
            guild.id,
            "bump.messages.leaderboard_description",
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            next_reset_timestamp=next_reset_ts,
            winner_mention=f"<@{winner_id}>",
            winner_count=winner_count,
            role_name=role_name,
            leaderboard_list="\n".join(lines),
        )

        view = DesignerView(store=False)
        lb_title = await self.bot.translator.t(guild.id, "bump.messages.leaderboard_title")

        winner_member = guild.get_member(winner_id)
        if winner_member and winner_member.display_avatar:
            title_section = discord.ui.Section(
                TextDisplay(self.bot.theme.md("title", title=lb_title)),
                accessory=discord.ui.Thumbnail(winner_member.display_avatar.url),
            )
            items = [title_section, TextDisplay(description)]
        else:
            items = [
                TextDisplay(self.bot.theme.md("title", title=lb_title)),
                TextDisplay(description),
            ]

        container_color = self.bot.theme.color(self.bot.theme.style("bump_leaderboard").color)
        view.add_item(designer_container(container_color, *items))
        return view

    async def _transfer_mvp_role(
        self,
        guild: discord.Guild,
        role: discord.Role,
        old_winner_id: int | None,
        new_winner_id: int,
    ) -> None:
        if not await self.boost_store.role_exists(guild.id, role.id):
            await self.boost_store.add_role(guild.id, role.id, new_winner_id, self.bot.user.id)
        else:
            await self.boost_store.transfer_ownership(guild.id, role.id, new_winner_id)

        if old_winner_id and old_winner_id != new_winner_id:
            old_member = guild.get_member(old_winner_id)
            if old_member:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await old_member.remove_roles(role, reason="Bump MVP week reset")

        new_member = guild.get_member(new_winner_id)
        if new_member:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await new_member.add_roles(role, reason="Bump MVP winner")

        customization = await self.bump_store.get_customization(guild.id, new_winner_id)
        if customization:
            default_name = self.bot.theme.bump.get("mvp_default_name", "Bump-MVP")
            name = customization.get("name", default_name)
            default_color = self.bot.theme.bump.get("mvp_default_color", "brand")
            if customization.get("color"):
                color = discord.Colour(int(customization["color"], 16))
            else:
                color = self.bot.theme.color(default_color)
            emoji = customization.get("emoji")
            # Note: Discord API requires bytes for icon; we can't restore a
            # role icon from a URL without downloading it. For now we only
            # restore name, color, and unicode emoji.
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await role.edit(
                    name=name,
                    colour=color,
                    unicode_emoji=emoji,
                    reason="Bump MVP restoration",
                )
        else:
            name = self.bot.theme.bump.get("mvp_default_name", "Bump-MVP")
            color = self.bot.theme.color(self.bot.theme.bump.get("mvp_default_color", "brand"))
            emoji = self.bot.theme.bump.get("mvp_default_emoji", "🚀")
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await role.edit(
                    name=name,
                    colour=color,
                    unicode_emoji=emoji,
                    reason="Bump MVP default",
                )

    async def _dm_participants(
        self,
        guild: discord.Guild,
        role: discord.Role,
        new_winner_id: int,
        old_winner_id: int | None,
        bump_count: int,
        total_wins: int,
    ) -> None:
        from rosemary.ui.boost_dm import send

        new_member = guild.get_member(new_winner_id)
        if new_member:
            is_repeat = old_winner_id == new_winner_id
            key = "bump.dm.winner_again" if is_repeat else "bump.dm.winner_new"
            max_members = await get_setting(self.bot.storage, guild.id, "boost.max_members")
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await send(
                    self.bot,
                    new_member,
                    guild.id,
                    key,
                    mention=new_member.mention,
                    bump_count=bump_count,
                    role_name=role.name,
                    max_members=max_members,
                    total_wins=total_wins,
                )
                data = await self.boost_store.get_role(guild.id, role.id) or {}
                await send_preview(self.bot, new_member, guild.id, role, data)

        if old_winner_id and old_winner_id != new_winner_id:
            old_member = guild.get_member(old_winner_id)
            if old_member:
                old_count = (await self.bump_store.get_leaderboard(guild.id)).get(
                    str(old_winner_id), 0,
                )
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await send(
                        self.bot,
                        old_member,
                        guild.id,
                        "bump.dm.winner_lost",
                        mention=old_member.mention,
                        bump_count=old_count,
                        winner_mention=f"<@{new_winner_id}>",
                    )

    # -- Commands ------------------------------------------------------------

    @discord.slash_command(
        name="bump_leaderboard",
        description="Show the weekly bump leaderboard",
        contexts={discord.InteractionContextType.guild},
    )
    async def bump_leaderboard(self, ctx: discord.ApplicationContext) -> None:
        """Post the weekly bump leaderboard in the current channel."""
        guild = ctx.guild
        if not await get_setting(self.bot.storage, guild.id, "bump.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "bump.error_disabled"), ephemeral=True
            )
        if not await get_setting(self.bot.storage, guild.id, "bump.leaderboard.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "bump.error_leaderboard_disabled"),
                ephemeral=True,
            )

        leaderboard = await self.bump_store.get_leaderboard(guild.id)
        if not leaderboard:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "bump.messages.no_bumps_description"),
                ephemeral=True,
            )

        sorted_lb = sorted(
            [(int(uid), count) for uid, count in leaderboard.items()],
            key=lambda x: x[1],
            reverse=True,
        )
        winner_id, winner_count = sorted_lb[0]

        role_id = await get_setting(self.bot.storage, guild.id, "bump.leaderboard.winner_role")
        role = guild.get_role(role_id) if role_id else None
        role_name = role.name if role else None

        view = await self._build_leaderboard_view(
            guild, sorted_lb, winner_id, winner_count, role_name,
        )
        from rosemary.core.mentions import mentions_for

        await ctx.respond(
            view=view,
            allowed_mentions=await mentions_for(
                self.bot, guild.id, "bump.leaderboard",
                source="command", user_ids=[winner_id],
            ),
        )

    @discord.slash_command(
        name="bump_stats",
        description="Show your bump statistics",
        contexts={discord.InteractionContextType.guild},
    )
    async def bump_stats(self, ctx: discord.ApplicationContext) -> None:
        """Show the caller's bump statistics for this week."""
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        guild = ctx.guild
        if not await get_setting(self.bot.storage, guild.id, "bump.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "bump.error_disabled"), ephemeral=True
            )
        if not await get_setting(self.bot.storage, guild.id, "bump.leaderboard.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "bump.error_leaderboard_disabled"),
                ephemeral=True,
            )

        leaderboard = await self.bump_store.get_leaderboard(guild.id)
        user_count = leaderboard.get(str(ctx.author.id), 0)
        if user_count == 0:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "bump.messages.no_bumps_you"),
                ephemeral=True,
            )

        sorted_lb = sorted(
            [(int(uid), count) for uid, count in leaderboard.items()],
            key=lambda x: x[1],
            reverse=True,
        )
        position = next(
            (i for i, (uid, _) in enumerate(sorted_lb, start=1) if uid == ctx.author.id),
            0,
        )
        total_bumps = sum(count for _, count in sorted_lb)
        total_wins = await self.bump_store.get_total_wins(guild.id, ctx.author.id)
        week_start = await self.bump_store.get_week_start(guild.id)

        stats_title = await self.bot.translator.t(guild.id, "bump.stats.title")
        lines = [
            await self.bot.translator.t(
                guild.id,
                "bump.stats.this_week",
                bumps=user_count,
                position=position,
                total_bumps=total_bumps,
            ),
        ]
        if total_wins > 0:
            lines.append(
                await self.bot.translator.t(guild.id, "bump.stats.history", wins=total_wins)
            )
        lines.append(
            await self.bot.translator.t(
                guild.id,
                "bump.stats.week_start",
                week_start_ts=int(week_start.timestamp()) if week_start else 0,
            )
        )
        reset_day = await get_setting(
            self.bot.storage, guild.id, "bump.leaderboard.reset_day"
        )
        reset_hour = await get_setting(
            self.bot.storage, guild.id, "bump.leaderboard.reset_hour"
        )
        tz = resolve_timezone(
            await get_setting(self.bot.storage, guild.id, "general.timezone")
        )
        next_reset_ts = int(
            _next_reset(datetime.now(UTC), reset_day, reset_hour, tz).timestamp()
        )
        lines.append(
            await self.bot.translator.t(
                guild.id,
                "bump.stats.week_end",
                next_reset_ts=next_reset_ts,
            )
        )

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color(self.bot.theme.style("bump_leaderboard").color),
                TextDisplay(self.bot.theme.md("title", title=stats_title)),
                TextDisplay("\n\n".join(lines)),
            )
        )
        await ctx.respond(view=view, ephemeral=True)

    @discord.slash_command(
        name="test_bump_leaderboard",
        description="[ADMIN] Force the weekly leaderboard calculation",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def test_bump_leaderboard(self, ctx: discord.ApplicationContext) -> None:
        """Force the weekly leaderboard processing and posting."""
        guild = ctx.guild
        if not await get_setting(self.bot.storage, guild.id, "bump.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "bump.error_disabled"), ephemeral=True
            )
        if not await get_setting(self.bot.storage, guild.id, "bump.leaderboard.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "bump.error_leaderboard_disabled"),
                ephemeral=True,
            )

        channel_id = await get_setting(self.bot.storage, guild.id, "bump.channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "bump.error_no_channel"), ephemeral=True
            )

        # _process_weekly_reset performs heavy IO (card send, role.edit, participant
        # DMs, DB writes, log channel). DEFER first so the interaction keeps its
        # token past Discord's 3s ACK window; ctx.respond routes to a followup.
        await ctx.response.defer(ephemeral=True)
        status = await self._process_weekly_reset(guild)
        if status == "no_bumps":
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "bump.test.no_bumps"), ephemeral=True
            )
        if status == "no_role":
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "bump.error_no_role"), ephemeral=True
            )
        await ctx.respond(
            await self.bot.translator.t(guild.id, "bump.success.leaderboard_posted"),
            ephemeral=True,
        )

    async def post_current_leaderboard(self, guild_id: int) -> bool:
        """Post the current week's leaderboard to the bump channel.

        Returns ``True`` if a card was sent, ``False`` otherwise (no data,
        disabled, or missing channel).
        """
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return False
        if not await get_setting(self.bot.storage, guild_id, "bump.leaderboard.enabled"):
            return False

        channel_id = await get_setting(self.bot.storage, guild_id, "bump.channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return False

        leaderboard = await self.bump_store.get_leaderboard(guild_id)
        if not leaderboard:
            # Mirror the weekly auto-post: emit the "no data" card instead of
            # staying silent, so the button always publishes something to the
            # bump channel. The ephemeral flash still reports "no_data".
            await self._post_no_bumps(guild, channel)
            await send_channel_log(
                self.bot,
                guild_id,
                await self.bot.translator.t(guild_id, "bump.logs.no_bumps_posted.title"),
                await log_description(
                    self.bot,
                    guild_id,
                    "bump.logs.no_bumps_posted.description",
                    channel=channel.mention,
                ),
                color="info",
                card_key="bump.logs.no_bumps_posted.description",
            )
            return False

        sorted_lb = sorted(
            [(int(uid), count) for uid, count in leaderboard.items()],
            key=lambda x: x[1],
            reverse=True,
        )
        winner_id, winner_count = sorted_lb[0]
        role_id = await get_setting(self.bot.storage, guild_id, "bump.leaderboard.winner_role")
        role = guild.get_role(role_id) if role_id else None
        role_name = role.name if role else None
        view = await self._build_leaderboard_view(
            guild, sorted_lb, winner_id, winner_count, role_name,
        )
        from rosemary.core.mentions import mentions_for

        await channel.send(
            view=view,
            allowed_mentions=await mentions_for(
                self.bot, guild_id, "bump.leaderboard",
                source="auto", user_ids=[winner_id],
            ),
        )
        await send_channel_log(
            self.bot,
            guild_id,
            await self.bot.translator.t(guild_id, "bump.logs.leaderboard_posted.title"),
            await log_description(
                self.bot,
                guild_id,
                "bump.logs.leaderboard_posted.description",
                channel=channel.mention,
            ),
            color="info",
            card_key="bump.logs.leaderboard_posted.description",
        )
        return True

    @discord.slash_command(
        name="reset_bump_week",
        description="[ADMIN] Manually reset the bump week",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def reset_bump_week(self, ctx: discord.ApplicationContext) -> None:
        """Reset the weekly leaderboard without posting."""
        await ctx.response.defer(ephemeral=True)
        await self.bump_store.reset_week(ctx.guild_id)
        await send_channel_log(
            self.bot,
            ctx.guild_id,
            await self.bot.translator.t(ctx.guild_id, "bump.logs.week_reset_manual.title"),
            await log_description(
                self.bot,
                ctx.guild_id,
                "bump.logs.week_reset_manual.description",
                author=ctx.author.mention,
            ),
            color="warning",
            card_key="bump.logs.week_reset_manual.description",
            mention_user_ids=[ctx.author.id],
        )
        await ctx.respond(
            await self.bot.translator.t(ctx.guild_id, "bump.success.week_reset"), ephemeral=True
        )

    @discord.slash_command(
        name="add_test_bumps",
        description="[ADMIN] Add test bumps for a user",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def add_test_bumps(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(discord.Member, description="Member to add bumps to"),
        count: discord.Option(
            int, description="Number of bumps to add", min_value=1, max_value=100,
        ),
    ) -> None:
        """Add ``count`` bumps for ``user`` in this week's leaderboard."""
        # Each add_bump is a read-modify-write of the guild JSON; with count up to
        # 100 this easily exceeds the 3s initial-response window, so DEFER first.
        await ctx.response.defer(ephemeral=True)
        for _ in range(count):
            await self.bump_store.add_bump(ctx.guild_id, user.id)
        total = (await self.bump_store.get_leaderboard(ctx.guild_id)).get(str(user.id), 0)
        await send_channel_log(
            self.bot,
            ctx.guild_id,
            await self.bot.translator.t(ctx.guild_id, "bump.logs.bumps_added.title"),
            await log_description(
                self.bot,
                ctx.guild_id,
                "bump.logs.bumps_added.description",
                author=ctx.author.mention,
                user=user.mention,
                count=count,
                total=total,
            ),
            color="warning",
            card_key="bump.logs.bumps_added.description",
            mention_user_ids=[ctx.author.id, user.id],
        )
        await ctx.respond(
            await self.bot.translator.t(
                ctx.guild_id,
                "bump.success.bumps_added",
                count=count,
                user_mention=user.mention,
                total=total,
            ),
            ephemeral=True,
        )

    # -- Listeners -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        guild_id = after.guild.id
        if not await get_setting(self.bot.storage, guild_id, "bump.leaderboard.enabled"):
            return

        role_id = await get_setting(self.bot.storage, guild_id, "bump.leaderboard.winner_role")
        if after.id != role_id:
            return

        current_winner = await self.bump_store.get_current_winner(guild_id)
        if not current_winner:
            return

        if before.name == after.name and before.color == after.color and (
            before.unicode_emoji == after.unicode_emoji
        ):
            return

        customization = {
            "name": after.name,
            "color": str(after.color).replace("#", ""),
            "emoji": after.unicode_emoji,
        }
        await self.bump_store.save_customization(guild_id, current_winner, customization)
        log.info("Auto-saved MVP role customization for %s in %s", current_winner, guild_id)


def setup(bot) -> None:
    bot.add_cog(BumpLeaderboardCog(bot))
