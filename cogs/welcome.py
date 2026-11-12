"""Welcome, leave and ban announcements with composable cards.

Every message is a customizable card (``events.welcome`` / ``events.leave`` /
``events.ban``): guilds edit them through /customize; without an override the
default document of the package renders as a themed card. Channels and
switches live in the EVENTS category of /settings.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque

import discord
from discord.ext import commands

from rosemary.core.settings import get_setting

log = logging.getLogger(__name__)

RAID_WINDOW_SECONDS = 10.0
RAID_THRESHOLD = 5
RAID_WELCOME_DELAY = 2.0


class WelcomeCog(commands.Cog):
    """Member join / leave / ban announcements."""

    #: Card key -> the guild setting holding that event's alert role.
    PING_ROLE_KEYS = {
        "events.welcome": "events.welcome_ping_role",
        "events.leave": "events.leave_ping_role",
        "events.ban": "events.ban_ping_role",
    }

    def __init__(self, bot) -> None:
        self.bot = bot
        self._recent_joins: dict[int, deque[float]] = {}
        self._raid_active: set[int] = set()
        self._recently_banned: set[int] = set()

    #: helpers -------------------------------------------------------------

    async def _is_staff_log_channel(self, guild_id: int, channel_id: int) -> bool:
        """Whether the channel is the staff log channel.

        A bot action (our /ban, auto-ban, trap) already posts a moderation card
        there; an event card in the same channel would duplicate it. Different
        channels mean different audiences: the announcement still goes out.
        """
        staff = await get_setting(self.bot.storage, guild_id, "logging.channel")
        if staff is None:
            return False
        try:
            return int(staff) == int(channel_id)
        except (TypeError, ValueError):
            return False

    async def _t(self, guild_id: int, key: str, **variables) -> str:
        return await self.bot.translator.t(guild_id, key, **variables)

    def _raid_detected(self, guild_id: int) -> bool:
        now = time.monotonic()
        joins = self._recent_joins.setdefault(
            guild_id, deque(maxlen=RAID_THRESHOLD * 2)
        )
        joins.append(now)
        while joins and now - joins[0] > RAID_WINDOW_SECONDS:
            joins.popleft()
        active = len(joins) >= RAID_THRESHOLD
        if active and guild_id not in self._raid_active:
            log.warning("Raid detected in guild %s", guild_id)
        if not active:
            self._raid_active.discard(guild_id)
        else:
            self._raid_active.add(guild_id)
        return active

    async def _send_event_card(
        self,
        channel: discord.TextChannel,
        guild: discord.Guild,
        key: str,
        title_key: str,
        body_key: str,
        color: str,
        variables: dict,
        user_id: int | None = None,
    ) -> None:
        """Send the event card: themed/default document via the shared
        renderer, plain-text theme override as fallback. Pings follow the
        document content (``{user}`` in the text decides), per-card toggle
        permitting; ``user_id`` only matters for that text-only path. The
        event's alert role (``events.<event>_ping_role``) pings first in its
        own message, best-effort.
        """
        guild_id = guild.id
        from rosemary.core.card_service import render_card_message, simple_payload
        from rosemary.core.mentions import allowed_for_ids

        # The instant of the event: the card's footer shows the date (the
        # Components V2 form has no timestamp field of its own).
        variables = {
            **variables,
            "timestamp": variables.get("timestamp")
            or discord.utils.format_dt(discord.utils.utcnow(), style="R"),
        }
        payload, allowed = await render_card_message(
            self.bot, guild_id, key, variables
        )
        if payload is None:
            # The fallback follows the card's own form (an embed): the
            # announcement never changes its surface.
            payload = simple_payload(
                self.bot,
                guild_id,
                key,
                title=await self._t(guild_id, title_key, **variables),
                body=await self._t(guild_id, body_key, **variables),
                color=color,
                thumbnail=variables.get("user_avatar") or None,
            )
            allowed = await allowed_for_ids(
                self.bot, guild_id, key,
                user_ids=[user_id] if user_id else [],
            )
        await self._send_receptionist_ping(channel, guild, key)
        await channel.send(**payload.message_kwargs(), allowed_mentions=allowed)

    async def _send_receptionist_ping(
        self, channel: discord.TextChannel, guild: discord.Guild, card_key: str
    ) -> None:
        """Ping the event's alert role in its own message before the card.

        Off by default (``events.<event>_ping_role``). The card's theme ping
        toggle gates it too: a guild that muted the card mutes the role. A
        deleted role or a failed ping is logged and skipped, never blocking
        the card that follows.
        """
        setting_key = self.PING_ROLE_KEYS.get(card_key)
        if setting_key is None:
            return
        role_id = await get_setting(self.bot.storage, guild.id, setting_key)
        if not role_id:
            return
        if guild.get_role(int(role_id)) is None:
            log.warning(
                "Alert role %s for %s is gone in guild %s",
                role_id, card_key, guild.id,
            )
            return
        from rosemary.core.mentions import allowed_for_ids

        allowed = await allowed_for_ids(
            self.bot, guild.id, card_key, role_ids=[int(role_id)]
        )
        if not allowed.roles:
            return  # card pings muted in the guild's theme
        try:
            await channel.send(f"<@&{int(role_id)}>", allowed_mentions=allowed)
        except discord.HTTPException as exc:
            log.warning("Alert ping failed for %s: %s", card_key, exc)

    async def _inviter_mention(self, member: discord.Member) -> str:
        """Inviter mention for the ``{inviter}`` card placeholder.

        Always returns a string (possibly empty) so customized cards never
        render a literal ``{inviter}``. Empty when tracking is off, hidden,
        unattributed, or the record is not there yet (listener race).
        """
        from rosemary.core.invites import InviteStore

        try:
            if not await get_setting(
                self.bot.storage, member.guild.id, "invites.enabled"
            ):
                return ""
            if not await get_setting(
                self.bot.storage, member.guild.id, "invites.show_inviter"
            ):
                return ""
            record = await InviteStore(self.bot.storage.data_dir).previous_record(
                member.guild.id, member.id
            )
        except Exception:
            return ""
        if not record:
            return ""
        inviter_id = record.get("inviter_id")
        return f"<@{inviter_id}>" if inviter_id else ""

    async def _event_channel(
        self, guild: discord.Guild, setting_key: str
    ) -> discord.TextChannel | None:
        if not await get_setting(self.bot.storage, guild.id, "events.enabled"):
            return None
        channel_id = await get_setting(self.bot.storage, guild.id, setting_key)
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    #: listeners -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        try:
            channel = await self._event_channel(member.guild, "events.welcome_channel")
            if channel is None:
                return
            if await get_setting(
                self.bot.storage, member.guild.id, "events.raid_protection"
            ) and self._raid_detected(member.guild.id):
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(RAID_WELCOME_DELAY)
            variables = {
                "user": member.mention,
                "user_name": member.display_name,
                "user_id": str(member.id),
                "server": member.guild.name,
                "count": member.guild.member_count or 0,
                "user_avatar": member.display_avatar.url,
                "inviter": await self._inviter_mention(member),
            }
            await self._send_event_card(
                channel,
                member.guild,
                "events.welcome",
                "events.welcome.title",
                "events.welcome.body",
                "brand",
                variables,
                member.id,
            )
        except Exception as exc:
            log.error("Welcome message failed for %s: %s", member, exc)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            if member.id in self._recently_banned:
                self._recently_banned.discard(member.id)
                return
            channel = await self._event_channel(member.guild, "events.leave_channel")
            if channel is None:
                return
            if await self._is_staff_log_channel(member.guild.id, channel.id):
                # Our own /kick or trap kick already posted a moderation card
                # in this same channel: a leave card would only duplicate it.
                return
            variables = {
                "user": member.mention,
                "user_name": member.display_name,
                "user_id": str(member.id),
                "server": member.guild.name,
                "count": member.guild.member_count or 0,
                "inviter": "",
                "user_avatar": member.display_avatar.url,
            }
            await self._send_event_card(
                channel,
                member.guild,
                "events.leave",
                "events.leave.title",
                "events.leave.body",
                "warning",
                variables,
                member.id,
            )
        except Exception as exc:
            log.error("Leave message failed for %s: %s", member, exc)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        try:
            self._recently_banned.add(user.id)
            channel = await self._event_channel(guild, "events.ban_channel")
            if channel is None:
                return
            if await self._is_staff_log_channel(guild.id, channel.id):
                # Our own /ban, auto-ban or trap ban already posted a
                # moderation card in this same channel: the announcement
                # would only duplicate it.
                return
            variables = {
                "user": user.mention,
                "user_name": user.name,
                "user_id": str(user.id),
                "server": guild.name,
                "count": guild.member_count or 0,
                "inviter": "",
                "user_avatar": user.display_avatar.url,
            }
            await self._send_event_card(
                channel,
                guild,
                "events.ban",
                "events.ban.title",
                "events.ban.body",
                "danger",
                variables,
                user.id,
            )
        except Exception as exc:
            log.error("Ban message failed for %s: %s", user, exc)

