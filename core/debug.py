"""Shared helpers for the /debug panel and staff logging.

Diagnostics return ``(i18n key, variables)`` pairs so the cog can translate
them. Staff logs delegate title/body/layout rendering to the shared card
pipeline; call sites pass content variables only.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import discord

from rosemary.core.card_service import render_card_message, send_card
from rosemary.core.identity import event_time, inject_identities
from rosemary.core.settings import get_setting
from rosemary.core.theme_package import default_package
from rosemary.core.themes import card_origin

if TYPE_CHECKING:
    from rosemary.bot import RosemaryBot

log = logging.getLogger(__name__)

#: Public event for one setting change. Exported so UI code can pass the event
#: without a literal that looks like a catalog setting key.
SETTINGS_LOG_EVENT = "settings.log"

#: How long a traced path stays quiet (the same card in a burst is one line).
_TRACE_TTL = 30.0
_TRACE_SEEN: dict[tuple[int, str], float] = {}


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


def _actor_variable(doc: dict[str, Any]) -> str:
    """The variable that names the actor of the event (``{moderator}``)."""
    slot = doc.get("actor")
    if not isinstance(slot, str):
        return ""
    name = slot.strip()
    if name.startswith("{") and name.endswith("}"):
        name = name[1:-1]
    return name if name and "." not in name else ""


def _legacy_label_keys(doc: dict[str, Any]) -> list[tuple[str, str]]:
    """The old ``<variable>_label`` names of the fields, with their key.

    A guild theme written before the fields migration customized the body of
    a log and referenced the labels by their old names (``{member_label}``).
    The document's fields carry both names, so the injection is generic.
    """
    pairs: list[tuple[str, str]] = []
    for field in doc.get("fields") or []:
        if not isinstance(field, dict):
            continue
        label = field.get("label")
        value = field.get("value")
        if not isinstance(label, str):
            # The short form (``- target: "{member}"``): the field name is
            # the label name and the template is the value.
            name = next(iter(field), "")
            label = "log.field." + str(name) + ".label"
            value = field.get(str(name))
        key = label.strip()
        if key.startswith("{") and key.endswith("}"):
            key = key[1:-1]
        if not key.startswith("log.field."):
            continue
        template = str(value or "").strip()
        if template.startswith("{") and template.endswith("}"):
            template = template[1:-1]
        if not template or "." in template:
            continue
        pairs.append((template + "_label", key))
    return pairs


def bot_state_summary(bot: RosemaryBot) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(translation key, format vars)`` pairs describing bot state.

    The variable names are the ones the ``debug.panel`` contract declares
    (``guilds``, ``members``, ``latency``, ``uptime``): the same names the
    panel document uses, so the card and this fallback say the same thing.
    """
    latency = getattr(bot, "latency", None)
    return [
        ("debug.guilds", {"guilds": len(bot.guilds)}),
        (
            "debug.members",
            {"members": sum(g.member_count or 0 for g in bot.guilds)},
        ),
        (
            "debug.latency",
            {"latency": f"{latency * 1000:.0f}" if latency is not None else "-"},
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

    ``event`` is a key of the default document package (``moderation.logs.ban``).
    The call site passes content variables only: title, body, color, footer and
    the per-event theme override come from the document and the guild's active
    theme. ``body_key`` selects a known body variant (e.g. the no-role bump
    log) without moving copy into the call site.

    Returns whether a message was actually sent. Logging disabled, no channel
    and HTTP failures are silent (warned in the bot log), exactly like the
    previous helper.
    """
    doc = default_package().document(event)
    if doc is None:
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
    actor_var = _actor_variable(doc)
    if actor_var:
        actor = values.get(actor_var)
        if actor is not None and str(actor).strip() and str(actor).strip() != "-":
            values.setdefault("actor", actor)
    guild = None
    get_guild = getattr(bot, "get_guild", None)
    if callable(get_guild):
        guild = get_guild(guild_id)
    inject_identities(bot, guild, values)
    # The event clock: the readable date in the guild's timezone and the
    # integer instant of the native embed timestamp. A call site that already
    # sent the instant (``epoch`` or a Discord time token) keeps it.
    moment = values.get("epoch")
    if moment is None:
        moment = values.get("timestamp")
    values.update(await event_time(bot, guild_id, moment))
    # Old body overrides still reference their literal labels. Inject them so
    # a guild theme written before the fields migration keeps rendering.
    for old_name, label_key in _legacy_label_keys(doc):
        if old_name in values:
            continue
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


async def trace_card_path(bot, guild_id: int, key: str) -> None:
    """Post ``card.<key>`` + origin to the log channel when ``debug.card_paths``
    is on. The default trace body carries no mention tokens and the trace card
    never traces itself.

    Repeats are deduplicated per ``(guild, key)`` for :data:`_TRACE_TTL`
    seconds: one menu repaint or one log burst should not flood the log
    channel with the same path. Public so call sites that build themed views
    outside the card pipeline (ephemeral confirm cards) can trace their own
    sends.
    """
    if key == "debug.trace":
        return
    try:
        enabled = await get_setting(bot.storage, guild_id, "debug.card_paths")
    except (KeyError, AttributeError):
        return
    if not enabled:
        return
    token = (guild_id, key)
    now = time.monotonic()
    if now - _TRACE_SEEN.get(token, 0.0) < _TRACE_TTL:
        return
    _TRACE_SEEN[token] = now
    theme_name, _ = card_origin(bot, guild_id, key)
    origin = (
        await bot.translator.t(guild_id, "debug.trace.theme", theme=theme_name)
        if theme_name
        else await bot.translator.t(guild_id, "debug.trace.default")
    )
    await send_log(
        bot,
        guild_id,
        "debug.trace",
        path=f"card.{key}",
        origin=origin,
    )
