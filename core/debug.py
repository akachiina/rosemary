"""Shared helpers for the /debug panel and moderation logging.

Nothing here renders user-facing text: translated strings are passed in by
callers, and diagnostics return ``(i18n key, variables)`` pairs so the cog can
translate them.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import discord

from rosemary.core.settings import get_setting
from rosemary.ui.containers import TextDisplay, designer_container, divider

if TYPE_CHECKING:
    from rosemary.bot import RosemaryBot

log = logging.getLogger(__name__)


def format_uptime(start_time: float | None) -> str:
    """Render monotonic uptime as a compact, language-neutral duration."""
    if start_time is None:
        return "-"
    seconds = int(time.monotonic() - start_time)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def bot_state_summary(bot: RosemaryBot) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(translation key, format vars)`` pairs describing bot state."""
    latency = getattr(bot, "latency", None)
    return [
        ("debug.guilds", {"count": len(bot.guilds)}),
        (
            "debug.members",
            {"count": sum(g.member_count or 0 for g in bot.guilds)},
        ),
        (
            "debug.latency",
            {"ms": f"{latency * 1000:.0f}" if latency is not None else "-"},
        ),
        (
            "debug.uptime",
            {"uptime": format_uptime(getattr(bot, "start_time", None))},
        ),
    ]


async def send_channel_log(
    bot: RosemaryBot,
    guild_id: int,
    title: str,
    description: str,
    *,
    color: str = "info",
) -> bool:
    """Send a Components V2 log message to the guild's configured log channel.

    No-ops when logging is disabled or no channel is configured. ``title`` and
    ``description`` must already be translated. Returns whether a message was
    actually sent.
    """
    if not await get_setting(bot.storage, guild_id, "logging.enabled"):
        return False
    channel_id = await get_setting(bot.storage, guild_id, "logging.channel")
    if channel_id is None:
        return False
    channel = bot.get_channel(channel_id)
    if channel is None or not isinstance(channel, discord.TextChannel):
        return False

    style = bot.theme.style("log")
    accent = bot.theme.color(color) if color else bot.theme.color(style.color)
    view = discord.ui.DesignerView(store=False)
    view.add_item(
        designer_container(
            accent,
            TextDisplay(bot.theme.md(style.template, title=title)),
            divider(),
            TextDisplay(description),
        )
    )
    try:
        await channel.send(view=view)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        log.warning("Could not send log to channel %s: %s", channel_id, exc)
        return False
    return True
