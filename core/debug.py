"""Shared helpers for the /debug panel and staff logging.

Diagnostics return ``(i18n key, variables)`` pairs so the cog can translate
them. Staff logs delegate title/body/layout rendering to the shared card
pipeline; call sites pass content variables only.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord

import rosemary.core.log_cards  # noqa: F401  (registers default log builders)
from rosemary.core.card_service import render_card_message, send_card
from rosemary.core.log_specs import LOG_SPECS
from rosemary.core.settings import get_setting

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


async def send_log(
    bot: RosemaryBot,
    guild_id: int,
    event: str,
    *,
    body_key: str | None = None,
    file: discord.File | None = None,
    **variables: Any,
) -> bool:
    """Send one registry log event through the shared card pipeline.

    ``event`` is a key in :data:`rosemary.core.log_specs.LOG_SPECS`
    (``moderation.logs.ban``). The call site passes content variables only:
    title, body, color, footer and the per-event theme override come from the
    registry and the guild's active theme. ``body_key`` selects a known body
    variant (e.g. the no-role bump log) without moving copy into the call site.

    Returns whether a message was actually sent. Logging disabled, no channel
    and HTTP failures are silent (warned in the bot log), exactly like the
    previous helper.
    """
    spec = LOG_SPECS.get(event)
    if spec is None:
        log.warning("Unknown log event %r; dropping send", event)
        return False
    if not await get_setting(bot.storage, guild_id, "logging.enabled"):
        return False
    channel_id = await get_setting(bot.storage, guild_id, "logging.channel")
    if channel_id is None:
        return False
    channel = bot.get_channel(channel_id)
    if channel is None or not isinstance(channel, discord.TextChannel):
        return False

    values = dict(variables)
    values.setdefault("timestamp", discord.utils.format_dt(datetime.now(UTC), "R"))
    if spec.actor_var:
        actor = values.get(spec.actor_var)
        if actor is not None and str(actor).strip() and str(actor).strip() != "-":
            values.setdefault("actor", actor)
    # Old body overrides still reference their literal labels. Inject them so
    # a guild theme written before the fields migration keeps rendering.
    for old_name, label_key in spec.legacy_labels:
        if old_name not in values:
            translate = getattr(bot.translator, "t", None)
            if callable(translate):
                values[old_name] = await translate(guild_id, label_key)
    if body_key:
        values["body_key"] = body_key

    payload, allowed = await render_card_message(bot, guild_id, event, values)
    if payload is None:
        log.warning("Log event %s resolved no card for guild %s", event, guild_id)
        return False
    kwargs: dict[str, Any] = {}
    if file is not None:
        kwargs["file"] = file
    try:
        await send_card(channel, payload, allowed, **kwargs)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        log.warning("Could not send log to channel %s: %s", channel_id, exc)
        return False
    return True
