"""Invite tracking cog for Rosemary.

Attributes each join to the invite code that brought the member in (by
diffing invite ``uses``), keeps per-inviter counters (regular/bonus/fake/left)
and serves stats, ranking, personal links and admin tooling. Joins that happen
while the bot is away cannot be attributed — members who joined before
tracking started stay ``unknown``.

Note: this file intentionally does NOT import ``from __future__ import
annotations``: py-cord inspects slash-command annotations with
``inspect.signature`` and stringified ``discord.Option(...)`` annotations would
break option parsing at runtime (same reason as ``cogs/moderation.py``).
"""

import contextlib
import logging
import time
from datetime import UTC, datetime

import discord
from discord.ext import commands

from rosemary.core.cards import log_description
from rosemary.core.debug import send_channel_log
from rosemary.core.invites import (
    FAKE,
    FLAGGED,
    UNKNOWN,
    InviteSnapshot,
    InviteStore,
    find_used_invite,
    join_status,
)
from rosemary.core.settings import get_setting

log = logging.getLogger(__name__)

_CACHE_COOLDOWN = 2.0


def _snapshot(invite: discord.Invite) -> InviteSnapshot:
    inviter = getattr(invite, "inviter", None)
    return InviteSnapshot(
        code=invite.code,
        uses=int(getattr(invite, "uses", 0) or 0),
        inviter_id=inviter.id if inviter is not None else None,
    )


class InvitesCog(commands.Cog):
    """Invite attribution, stats, ranking and admin tools."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = InviteStore(bot.storage.data_dir)
        self._cache: dict[int, dict[str, int]] = {}
        self._cache_ts: dict[int, float] = {}
        self._started = False

    async def start(self) -> None:
        """Prime the invite cache once per process (called from ``_setup``)."""
        if self._started:
            return
        self._started = True
        for guild in self.bot.guilds:
            await self._prime_cache(guild.id)

    # -- cache ---------------------------------------------------------------

    async def _fetch_snapshots(self, guild: discord.Guild) -> list[InviteSnapshot] | None:
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not fetch invites for %s: %s", guild.id, exc)
            return None
        return [_snapshot(invite) for invite in invites]

    async def _prime_cache(self, guild_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        snapshots = await self._fetch_snapshots(guild)
        if snapshots is not None:
            self._cache[guild_id] = {s.code: s.uses for s in snapshots}
            self._cache_ts[guild_id] = time.monotonic()

    # -- attribution -----------------------------------------------------------

    @staticmethod
    def _account_age_days(member: discord.Member) -> float:
        created = getattr(member, "created_at", None)
        if created is None:
            return 36500.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return (datetime.now(UTC) - created).total_seconds() / 86400.0

    async def _attribute(
        self, member: discord.Member, snapshots: list[InviteSnapshot] | None
    ) -> tuple[int | None, str | None, str]:
        """Attribute a join; returns ``(inviter_id, code, status)``."""
        guild_id = member.guild.id
        used = (
            find_used_invite(self._cache.get(guild_id, {}), snapshots or [])
            if snapshots is not None
            else None
        )
        inviter_id = used.inviter_id if used is not None else None
        code = used.code if used is not None else None
        if used is None:
            return None, None, UNKNOWN

        prev = await self.store.previous_record(guild_id, member.id)
        is_rejoin = prev is not None
        changed = is_rejoin and prev.get("inviter_id") != inviter_id

        inviter_role_ids: list[int] = []
        if inviter_id is not None:
            inviter = member.guild.get_member(inviter_id)
            if inviter is not None:
                inviter_role_ids = [role.id for role in getattr(inviter, "roles", [])]
        excluded = (
            await self.store.is_excluded(guild_id, inviter_id, inviter_role_ids)
            if inviter_id is not None
            else False
        )
        status = join_status(
            account_age_days=self._account_age_days(member),
            fake_delay_days=await get_setting(
                self.bot.storage, guild_id, "invites.fake_delay_days"
            ),
            is_rejoin=is_rejoin,
            rejoin_changed_inviter=changed,
            anti_cheat_days=await get_setting(
                self.bot.storage, guild_id, "invites.anti_cheat_days"
            ),
            count_rejoins=await get_setting(
                self.bot.storage, guild_id, "invites.count_rejoins"
            ),
            excluded=excluded,
        )
        return inviter_id, code, status

    # -- listeners ---------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._prime_cache(guild.id)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if guild is None:
            return
        self._cache.setdefault(guild.id, {})[invite.code] = 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if guild is None:
            return
        self._cache.get(guild.id, {}).pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        try:
            snapshots = await self._fetch_snapshots(guild)
            if snapshots is not None:
                self._cache[guild.id] = {s.code: s.uses for s in snapshots}
                self._cache_ts[guild.id] = time.monotonic()
            if not await get_setting(self.bot.storage, guild.id, "invites.enabled"):
                return
            inviter_id, code, status = await self._attribute(member, snapshots)
            await self.store.record_join(guild.id, member.id, inviter_id, code, status)
            await self._log_join(guild, member, inviter_id, code, status)
        except Exception as exc:
            log.error("Invite attribution failed for %s: %s", member, exc)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        try:
            if not await get_setting(self.bot.storage, guild.id, "invites.enabled"):
                return
            if await get_setting(self.bot.storage, guild.id, "invites.track_leaves"):
                inviter_id = await self.store.record_leave(guild.id, member.id)
            else:
                record = await self.store.previous_record(guild.id, member.id)
                inviter_id = record.get("inviter_id") if record else None
            if inviter_id is None and await self.store.previous_record(guild.id, member.id) is None:
                return
            await send_channel_log(
                self.bot,
                guild.id,
                await self.bot.translator.t(guild.id, "invites.logs.leave.title"),
                await log_description(
                    self.bot,
                    guild.id,
                    "invites.logs.leave.description",
                    user=member.mention,
                    inviter=f"<@{inviter_id}>" if inviter_id else "-",
                ),
                color="info",
                card_key="invites.logs.leave.description",
                mention_user_ids=[member.id],
            )
        except Exception as exc:
            log.error("Invite leave tracking failed for %s: %s", member, exc)

    async def _log_join(
        self,
        guild: discord.Guild,
        member: discord.Member,
        inviter_id: int | None,
        code: str | None,
        status: str,
    ) -> None:
        label = await self.store.get_label(guild.id, code)
        flags = ""
        if status == FAKE:
            flags = await self.bot.translator.t(guild.id, "invites.flags.fake")
        elif status == FLAGGED:
            flags = await self.bot.translator.t(guild.id, "invites.flags.flagged")
        await send_channel_log(
            self.bot,
            guild.id,
            await self.bot.translator.t(guild.id, "invites.logs.join.title"),
            await log_description(
                self.bot,
                guild.id,
                "invites.logs.join.description",
                user=member.mention,
                code=code or "-",
                inviter=f"<@{inviter_id}>" if inviter_id else "-",
                label=f" ({label})" if label else "",
                flags=flags,
            ),
            color="info",
            card_key="invites.logs.join.description",
            mention_user_ids=[member.id, *( [inviter_id] if inviter_id else [])],
        )

    # -- shared card builder -----------------------------------------------------

    async def _rank_of(self, guild_id: int, user_id: int) -> int:
        for position, (uid, _) in enumerate(await self.store.leaderboard(guild_id), start=1):
            if uid == user_id:
                return position
        return 0

    # -- public commands -----------------------------------------------------------

    @discord.slash_command(
        name="invites",
        description="Show invite stats",
        contexts={discord.InteractionContextType.guild},
    )
    async def invites(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Member to inspect", required=False),
    ) -> None:
        """Show invite stats for yourself or another member."""
        guild_id = ctx.guild_id
        if not await get_setting(self.bot.storage, guild_id, "invites.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "invites.error_disabled"),
                ephemeral=True,
            )
        target = member or ctx.author
        stats = await self.store.get_stats(guild_id, target.id)
        if stats["total"] == 0 and stats["regular"] == 0 and stats["bonus"] == 0:
            return await ctx.respond(
                await self.bot.translator.t(
                    guild_id, "invites.stats.unknown", user=target.mention
                ),
                ephemeral=True,
            )
        latest = await self.store.invited_list(guild_id, target.id, limit=1)
        lines = [
            await self.bot.translator.t(
                guild_id,
                "invites.stats.breakdown",
                regular=stats["regular"],
                bonus=stats["bonus"],
                fake=stats["fake"],
                left=stats["left"],
            ),
            await self.bot.translator.t(
                guild_id,
                "invites.stats.total",
                total=stats["total"],
                rank=await self._rank_of(guild_id, target.id),
            ),
        ]
        real = stats["regular"] - stats["fake"]
        if real > 0:
            members = await self.store.invited_list(guild_id, target.id, limit=1000)
            stayed = sum(
                1 for uid, _entry in members if ctx.guild.get_member(uid) is not None
            )
            percent = round(stayed / real * 100) if real else 0
            lines.append(
                await self.bot.translator.t(
                    guild_id,
                    "invites.stats.retention",
                    percent=percent,
                    stayed=stayed,
                    total_real=real,
                )
            )
        if latest:
            uid, entry = latest[0]
            joined = entry.get("joined_at") or ""
            lines.append(
                await self.bot.translator.t(
                    guild_id,
                    "invites.stats.latest",
                    user=f"<@{uid}>",
                    when=f"<t:{int(datetime.fromisoformat(joined).timestamp())}:R>"
                    if joined
                    else "-",
                )
            )
        # Title carries the user; build the card with the variable filled in.
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        title = await self.bot.translator.t(
            guild_id, "invites.stats.title", user=target.mention
        )
        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("info"),
                TextDisplay(self.bot.theme.md("title", title=title)),
                TextDisplay("\n\n".join(lines)),
            )
        )
        await ctx.respond(view=view, ephemeral=True)

    @discord.slash_command(
        name="invites_leaderboard",
        description="Show the top inviters ranking",
        contexts={discord.InteractionContextType.guild},
    )
    async def invites_leaderboard(self, ctx: discord.ApplicationContext) -> None:
        """Post the top inviters ranking in the current channel."""
        guild_id = ctx.guild_id
        if not await get_setting(self.bot.storage, guild_id, "invites.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "invites.error_disabled"),
                ephemeral=True,
            )
        if not await get_setting(self.bot.storage, guild_id, "invites.leaderboard_enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "invites.error_leaderboard_disabled"),
                ephemeral=True,
            )
        size = await get_setting(self.bot.storage, guild_id, "invites.leaderboard_size")
        rows = (await self.store.leaderboard(guild_id))[:size]
        if not rows:
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "invites.leaderboard.empty"),
                ephemeral=True,
            )
        lines = []
        for position, (uid, stats) in enumerate(rows, start=1):
            medal = self.bot.theme.medals.get(
                str(position), self.bot.theme.medals.get("default", "📊")
            )
            lines.append(
                await self.bot.translator.t(
                    guild_id,
                    "invites.leaderboard.line",
                    medal=medal,
                    position=position,
                    user=f"<@{uid}>",
                    total=stats["total"],
                )
            )
        view = await self._public_card(
            guild_id, "invites.leaderboard.title", "\n".join(lines)
        )
        from rosemary.core.mentions import mentions_for

        await ctx.respond(
            view=view,
            allowed_mentions=await mentions_for(self.bot, guild_id, "invites.leaderboard"),
        )

    async def _public_card(self, guild_id: int, title_key: str, body: str):
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        title = await self.bot.translator.t(guild_id, title_key)
        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("info"),
                TextDisplay(self.bot.theme.md("title", title=title)),
                TextDisplay(body),
            )
        )
        return view

    @discord.slash_command(
        name="who_invited",
        description="Show who invited a member",
        contexts={discord.InteractionContextType.guild},
    )
    async def who_invited(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Member to inspect"),
    ) -> None:
        """Show who invited a member."""
        guild_id = ctx.guild_id
        if not await get_setting(self.bot.storage, guild_id, "invites.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "invites.error_disabled"),
                ephemeral=True,
            )
        record = await self.store.invited_by(guild_id, member.id)
        if not record:
            return await ctx.respond(
                await self.bot.translator.t(
                    guild_id, "invites.inviter.unknown", user=member.mention
                ),
                ephemeral=True,
            )
        inviter_id = record.get("inviter_id")
        joined = record.get("joined_at") or ""
        body = await self.bot.translator.t(
            guild_id,
            "invites.inviter.body",
            user=member.mention,
            inviter=f"<@{inviter_id}>" if inviter_id else "-",
            code=record.get("code") or "-",
            when=f"<t:{int(datetime.fromisoformat(joined).timestamp())}:R>"
            if joined
            else "-",
            status=await self.bot.translator.t(
                guild_id, f"invites.status.{record.get('status') or 'unknown'}"
            ),
        )
        title = await self.bot.translator.t(guild_id, "invites.inviter.title")
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("info"),
                TextDisplay(self.bot.theme.md("title", title=title)),
                TextDisplay(body),
            )
        )
        await ctx.respond(view=view, ephemeral=True)

    @discord.slash_command(
        name="invited",
        description="List members invited by someone",
        contexts={discord.InteractionContextType.guild},
    )
    async def invited(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Inviter to inspect", required=False),
    ) -> None:
        """List the latest members someone invited."""
        guild_id = ctx.guild_id
        if not await get_setting(self.bot.storage, guild_id, "invites.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "invites.error_disabled"),
                ephemeral=True,
            )
        target = member or ctx.author
        rows = await self.store.invited_list(guild_id, target.id)
        if not rows:
            return await ctx.respond(
                await self.bot.translator.t(
                    guild_id, "invites.invited.empty", user=target.mention
                ),
                ephemeral=True,
            )
        lines = []
        for uid, entry in rows:
            joined = entry.get("joined_at") or ""
            lines.append(
                await self.bot.translator.t(
                    guild_id,
                    "invites.invited.line",
                    user=f"<@{uid}>",
                    when=f"<t:{int(datetime.fromisoformat(joined).timestamp())}:R>"
                    if joined
                    else "-",
                    status=await self.bot.translator.t(
                        guild_id, f"invites.status.{entry.get('status') or 'unknown'}"
                    ),
                )
            )
        title = await self.bot.translator.t(
            guild_id, "invites.invited.title", user=target.mention
        )
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("info"),
                TextDisplay(self.bot.theme.md("title", title=title)),
                TextDisplay("\n".join(lines)),
            )
        )
        await ctx.respond(view=view, ephemeral=True)

    @discord.slash_command(
        name="my_invite",
        description="Get your personal invite link",
        contexts={discord.InteractionContextType.guild},
    )
    async def my_invite(self, ctx: discord.ApplicationContext) -> None:
        """Create (or reuse) a permanent invite and show personal stats."""
        guild_id = ctx.guild_id
        if not await get_setting(self.bot.storage, guild_id, "invites.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "invites.error_disabled"),
                ephemeral=True,
            )
        channel = None
        if isinstance(ctx.channel, discord.TextChannel):
            channel = ctx.channel
        elif ctx.guild.system_channel is not None:
            channel = ctx.guild.system_channel
        else:
            for candidate in ctx.guild.text_channels:
                channel = candidate
                break
        link = None
        if channel is not None:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                for invite in await channel.invites():
                    if (
                        invite.inviter is not None
                        and invite.inviter.id == self.bot.user.id
                        and not invite.temporary
                        and (invite.max_age or 0) == 0
                    ):
                        link = invite.url
                        break
                if link is None:
                    created = await channel.create_invite(
                        max_age=0, max_uses=0, reason="Personal invite link"
                    )
                    link = created.url
        if link is None:
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "invites.personal.no_channel"),
                ephemeral=True,
            )
        stats = await self.store.get_stats(guild_id, ctx.author.id)
        body = await self.bot.translator.t(
            guild_id,
            "invites.personal.body",
            link=link,
            total=stats["total"],
            regular=stats["regular"],
            bonus=stats["bonus"],
        )
        title = await self.bot.translator.t(guild_id, "invites.personal.title")
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("info"),
                TextDisplay(self.bot.theme.md("title", title=title)),
                TextDisplay(body),
            )
        )
        await ctx.respond(view=view, ephemeral=True)

    @discord.slash_command(
        name="server_invites",
        description="Show server-wide invite statistics",
        contexts={discord.InteractionContextType.guild},
    )
    async def server_invites(self, ctx: discord.ApplicationContext) -> None:
        """Show server-wide invite statistics."""
        guild_id = ctx.guild_id
        if not await get_setting(self.bot.storage, guild_id, "invites.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "invites.error_disabled"),
                ephemeral=True,
            )
        totals = await self.store.server_totals(guild_id)
        rows = await self.store.leaderboard(guild_id)
        lines = [
            await self.bot.translator.t(
                guild_id, "invites.server.joins", count=totals["joins"]
            ),
            await self.bot.translator.t(
                guild_id,
                "invites.stats.breakdown",
                regular=totals["regular"],
                bonus=totals["bonus"],
                fake=totals["fake"],
                left=totals["left"],
            ),
        ]
        if rows:
            uid, stats = rows[0]
            lines.append(
                await self.bot.translator.t(
                    guild_id,
                    "invites.server.top",
                    user=f"<@{uid}>",
                    total=stats["total"],
                )
            )
        title = await self.bot.translator.t(guild_id, "invites.server.title")
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("info"),
                TextDisplay(self.bot.theme.md("title", title=title)),
                TextDisplay("\n\n".join(lines)),
            )
        )
        await ctx.respond(view=view, ephemeral=True)

    # -- admin commands ----------------------------------------------------------

    @discord.slash_command(
        name="invites_bonus",
        description="[ADMIN] Add or remove bonus invites",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def invites_bonus(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Member to adjust"),
        amount: discord.Option(int, description="Between -100 and 100, excluding 0"),
    ) -> None:
        """Add (positive) or remove (negative) bonus invites."""
        guild_id = ctx.guild_id
        if amount == 0 or not -100 <= amount <= 100:
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "invites.bonus.invalid"),
                ephemeral=True,
            )
        stats = await self.store.add_bonus(guild_id, member.id, amount)
        key = "invites.bonus.added" if amount > 0 else "invites.bonus.removed"
        await send_channel_log(
            self.bot,
            guild_id,
            await self.bot.translator.t(guild_id, "invites.logs.bonus.title"),
            await log_description(
                self.bot,
                guild_id,
                "invites.logs.bonus.description",
                moderator=ctx.author.mention,
                user=member.mention,
                amount=amount,
                total=stats["total"],
            ),
            color="warning",
            card_key="invites.logs.bonus.description",
            mention_user_ids=[member.id, ctx.author.id],
        )
        await ctx.respond(
            await self.bot.translator.t(
                guild_id, key, user=member.mention, amount=abs(amount), total=stats["total"]
            ),
            ephemeral=True,
        )

    @discord.slash_command(
        name="invites_sync",
        description="[ADMIN] Recount invite uses from Discord",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def invites_sync(self, ctx: discord.ApplicationContext) -> None:
        """Rebuild the attribution baseline from live invite uses."""
        await ctx.response.defer(ephemeral=True)
        guild = ctx.guild
        snapshots = await self._fetch_snapshots(guild)
        if snapshots is None:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "invites.personal.no_channel"),
                ephemeral=True,
            )
        baseline = await self.store.reconcile_uses(guild.id, snapshots)
        self._cache[guild.id] = baseline
        self._cache_ts[guild.id] = time.monotonic()
        await send_channel_log(
            self.bot,
            guild.id,
            await self.bot.translator.t(guild.id, "invites.logs.sync.title"),
            await log_description(
                self.bot,
                guild.id,
                "invites.logs.sync.description",
                codes=len(baseline),
                moderator=ctx.author.mention,
            ),
            color="info",
            card_key="invites.logs.sync.description",
            mention_user_ids=[ctx.author.id],
        )
        await ctx.respond(
            await self.bot.translator.t(
                guild.id, "invites.sync.success", codes=len(baseline)
            ),
            ephemeral=True,
        )

    @discord.slash_command(
        name="invites_codes",
        description="[ADMIN] List invite codes and uses",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def invites_codes(self, ctx: discord.ApplicationContext) -> None:
        """List live invite codes with uses and owners."""
        await ctx.response.defer(ephemeral=True)
        snapshots = await self._fetch_snapshots(ctx.guild)
        if not snapshots:
            return await ctx.respond(
                await self.bot.translator.t(ctx.guild_id, "invites.codes.empty"),
                ephemeral=True,
            )
        lines = []
        for snapshot in sorted(snapshots, key=lambda s: s.uses, reverse=True)[:25]:
            label = await self.store.get_label(ctx.guild_id, snapshot.code)
            lines.append(
                await self.bot.translator.t(
                    ctx.guild_id,
                    "invites.codes.line",
                    code=snapshot.code,
                    uses=snapshot.uses,
                    inviter=f"<@{snapshot.inviter_id}>" if snapshot.inviter_id else "-",
                    label=f" ({label})" if label else "",
                )
            )
        title = await self.bot.translator.t(ctx.guild_id, "invites.codes.title")
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("info"),
                TextDisplay(self.bot.theme.md("title", title=title)),
                TextDisplay("\n".join(lines)),
            )
        )
        await ctx.respond(view=view, ephemeral=True)

    @staticmethod
    def _format_id_list(name: str, ids: list[int]) -> str:
        if name == "roles":
            return ", ".join(f"<@&{uid}>" for uid in ids)
        return ", ".join(f"<@{uid}>" for uid in ids)

    @discord.slash_command(
        name="invites_blacklist",
        description="[ADMIN] Manage the invite blacklist",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def invites_blacklist(
        self,
        ctx: discord.ApplicationContext,
        action: discord.Option(
            str, description="add, remove or list", choices=["add", "remove", "list"]
        ),
        kind: discord.Option(
            str, description="users, roles or hidden", choices=["users", "roles", "hidden"]
        ),
        member: discord.Option(discord.Member, description="User", required=False),
        role: discord.Option(discord.Role, description="Role (for roles)", required=False),
    ) -> None:
        """Manage users/roles excluded from credit and users hidden from ranking."""
        guild_id = ctx.guild_id
        if action == "list":
            doc_users = await self.store._id_list(guild_id, "users")
            doc_roles = await self.store._id_list(guild_id, "roles")
            doc_hidden = await self.store._id_list(guild_id, "hidden")
            if not doc_users and not doc_roles and not doc_hidden:
                return await ctx.respond(
                    await self.bot.translator.t(guild_id, "invites.blacklist.empty"),
                    ephemeral=True,
                )
            body = "\n".join(
                f"**{name}:** {self._format_id_list(name, ids) or '-'}"
                for name, ids in (
                    ("users", doc_users),
                    ("roles", doc_roles),
                    ("hidden", doc_hidden),
                )
            )
            title = await self.bot.translator.t(guild_id, "invites.blacklist.title")
            from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

            view = DesignerView(store=False)
            view.add_item(
                designer_container(
                    self.bot.theme.color("info"),
                    TextDisplay(self.bot.theme.md("title", title=title)),
                    TextDisplay(body),
                )
            )
            return await ctx.respond(view=view, ephemeral=True)

        target_id = None
        target_label = None
        if kind == "roles":
            if role is None:
                return await ctx.respond(
                    await self.bot.translator.t(guild_id, "invites.blacklist.missing_role"),
                    ephemeral=True,
                )
            target_id, target_label = role.id, role.mention
        else:
            if member is None:
                return await ctx.respond(
                    await self.bot.translator.t(guild_id, "invites.blacklist.missing_member"),
                    ephemeral=True,
                )
            target_id, target_label = member.id, member.mention

        if action == "add":
            ok = await self.store.blacklist_add(guild_id, kind, target_id)
            key = "invites.blacklist.added" if ok else "invites.blacklist.already"
        else:
            ok = await self.store.blacklist_remove(guild_id, kind, target_id)
            key = "invites.blacklist.removed" if ok else "invites.blacklist.missing"
        await ctx.respond(
            await self.bot.translator.t(
                guild_id, key, target=target_label, kind=kind
            ),
            ephemeral=True,
        )

    @discord.slash_command(
        name="invites_reset",
        description="[ADMIN] Reset all invite stats",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def invites_reset(self, ctx: discord.ApplicationContext) -> None:
        """Reset every tracked join and counter (with confirmation)."""
        from rosemary.ui.containers import TextDisplay, designer_container
        from rosemary.ui.menu import MenuView

        view = MenuView(author_id=ctx.author.id)
        t = self.bot.translator.t
        guild_id = ctx.guild_id
        store = self.store
        bot = self.bot

        async def confirm(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            await store.reset(guild_id)
            await send_channel_log(
                bot,
                guild_id,
                await t(guild_id, "invites.logs.reset.title"),
                await log_description(
                    bot,
                    guild_id,
                    "invites.logs.reset.description",
                    moderator=interaction.user.mention,
                ),
                color="warning",
                card_key="invites.logs.reset.description",
                mention_user_ids=[interaction.user.id],
            )
            view.clear_items()
            view.add_item(TextDisplay(await t(guild_id, "invites.reset.success")))
            view.stop()
            await interaction.edit(view=view)

        async def cancel(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            view.clear_items()
            view.add_item(TextDisplay(await t(guild_id, "invites.reset.cancelled")))
            view.stop()
            await interaction.edit(view=view)

        view.register("invites_reset_yes", confirm)
        view.register("invites_reset_no", cancel)
        container = designer_container(
            self.bot.theme.color("warning"),
            TextDisplay(
                self.bot.theme.md(
                    "title",
                    title=await t(guild_id, "invites.reset.confirm_title"),
                )
            ),
            TextDisplay(await t(guild_id, "invites.reset.confirm_text")),
        )
        view.add_item(container)
        view.add_item(
            discord.ui.ActionRow(
                view.make_button(
                    custom_id="invites_reset_yes",
                    label=await t(guild_id, "invites.reset.confirm"),
                    style=discord.ButtonStyle.danger,
                ),
                view.make_button(
                    custom_id="invites_reset_no",
                    label=await t(guild_id, "invites.reset.cancel"),
                    style=discord.ButtonStyle.secondary,
                ),
            )
        )
        await view.prepare()
        await ctx.respond(view=view, ephemeral=True)

    @discord.slash_command(
        name="invites_label",
        description="[ADMIN] Label an invite code",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def invites_label(
        self,
        ctx: discord.ApplicationContext,
        code: discord.Option(str, description="Invite code"),
        label: discord.Option(str, description="Label (omit to clear)", required=False),
    ) -> None:
        """Label a code with its source (omit the label to clear it)."""
        guild_id = ctx.guild_id
        await self.store.set_label(guild_id, code, label)
        if label:
            message = await self.bot.translator.t(
                guild_id, "invites.label.set", code=code, label=label
            )
        else:
            message = await self.bot.translator.t(
                guild_id, "invites.label.cleared", code=code
            )
        await ctx.respond(message, ephemeral=True)


def setup(bot) -> None:
    bot.add_cog(InvitesCog(bot))
