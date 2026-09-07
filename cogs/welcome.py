"""Welcome, leave and ban announcements with composable cards.

Every message is a customizable card (``events.welcome`` / ``events.leave`` /
``events.ban``): guilds edit them through /customize; without an override the
default catalog templates render as a themed V2 container. Channels and
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

from rosemary.core.cards import ECHO_VARIABLES, maybe_text, maybe_view, set_default_builder
from rosemary.core.settings import get_setting

log = logging.getLogger(__name__)

RAID_WINDOW_SECONDS = 10.0
RAID_THRESHOLD = 5
RAID_WELCOME_DELAY = 2.0


async def default_event_document(
    bot,
    guild_id: int,
    *,
    title_key: str,
    body_key: str,
    color: str,
) -> dict:
    """Catalog-default event message as an editable block document.

    Placeholders ({user}, {server}...) stay literal — this is a template.
    Theme emoji tokens resolve; unknown placeholders are preserved by
    :func:`rosemary.core.cards.safe_format`.
    """
    from rosemary.core.cards import safe_format

    mapping = {**bot.theme.emojis, **ECHO_VARIABLES}
    title = safe_format(await bot.translator.raw(guild_id, title_key), mapping)
    body = safe_format(await bot.translator.raw(guild_id, body_key), mapping)
    return {
        "v": 1,
        "blocks": [
            {"type": "container", "color": color, "children": [
                {"type": "text", "body": f"# {title}"},
                {"type": "text", "body": body},
            ]}
        ],
    }


def _register_default_builders() -> None:
    for name, title, body, color in (
        ("welcome", "events.welcome.title", "events.welcome.body", "brand"),
        ("leave", "events.leave.title", "events.leave.body", "warning"),
        ("ban", "events.ban.title", "events.ban.body", "danger"),
    ):
        async def _builder(bot, guild_id, *, t=title, b=body, c=color):
            return await default_event_document(
                bot, guild_id, title_key=t, body_key=b, color=c
            )

        set_default_builder(f"events.{name}", _builder)


class WelcomeCog(commands.Cog):
    """Member join / leave / ban announcements."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._recent_joins: dict[int, deque[float]] = {}
        self._raid_active: set[int] = set()
        self._recently_banned: set[int] = set()

    # -- helpers -------------------------------------------------------------

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
        guild_id: int,
        key: str,
        title_key: str,
        body_key: str,
        color: str,
        variables: dict,
        user_id: int | None = None,
    ) -> None:
        """Send a customizable card: override > plain text > default layout."""
        from rosemary.core.mentions import mentions_for

        view = await maybe_view(self.bot, guild_id, key, variables)
        if view is None:
            text = await maybe_text(self.bot, guild_id, key, **variables)
            if text is None:
                from rosemary.ui.containers import (
                    DesignerView,
                    TextDisplay,
                    designer_container,
                )

                theme = self.bot.theme
                view = DesignerView(store=False)
                view.add_item(
                    designer_container(
                        theme.color(color),
                        TextDisplay(
                            theme.md(
                                "title",
                                title=await self._t(guild_id, title_key, **variables),
                            )
                        ),
                        TextDisplay(await self._t(guild_id, body_key, **variables)),
                    )
                )
            else:
                from rosemary.ui.containers import DesignerView

                view = DesignerView(store=False)
                view.add_item(TextDisplay(text))
            await channel.send(
                view=view,
                allowed_mentions=await mentions_for(
                    self.bot, guild_id, key,
                    user_ids=[user_id] if user_id else [],
                ),
            )
            return
        await channel.send(
            view=view,
            allowed_mentions=await mentions_for(
                self.bot, guild_id, key,
                user_ids=[user_id] if user_id else [],
            ),
        )

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

    # -- listeners -----------------------------------------------------------

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
                "server": member.guild.name,
                "count": member.guild.member_count or 0,
                "user_avatar": member.display_avatar.url,
                "inviter": await self._inviter_mention(member),
            }
            await self._send_event_card(
                channel,
                member.guild.id,
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
            variables = {
                "user": member.mention,
                "user_name": member.display_name,
                "server": member.guild.name,
                "count": member.guild.member_count or 0,
                "inviter": "",
            }
            await self._send_event_card(
                channel,
                member.guild.id,
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
            variables = {
                "user": user.mention,
                "user_name": user.name,
                "server": guild.name,
                "count": guild.member_count or 0,
                "inviter": "",
            }
            await self._send_event_card(
                channel,
                guild.id,
                "events.ban",
                "events.ban.title",
                "events.ban.body",
                "danger",
                variables,
                user.id,
            )
        except Exception as exc:
            log.error("Ban message failed for %s: %s", user, exc)

