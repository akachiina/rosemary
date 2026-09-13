"""Card resolution and rendering: theme override -> catalog default.

This is the single pipeline every card send goes through:

1. the guild's active theme file may override the card (raw Discord Components
   V2 in the theme, converted at load — see :mod:`rosemary.core.v2_convert`);
2. otherwise the feature's own default applies: a registered builder, the seed
   map (:data:`SEED_PARTS_BY_KEY`) or plain ``card.<key>``/``<key>`` scalars.

Rendering validates first and raises :class:`CardsError` on invalid content —
send sites treat that as "use the default message", so a bad theme can never
break a send.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import discord

from rosemary.core.cards import (
    DOCUMENT_VERSION,
    ECHO_VARIABLES,
    CardsError,
    build_items,
    get_default_builder,
    safe_format,
)
from rosemary.core.themes import card_document, card_origin, theme_for

log = logging.getLogger(__name__)

#: Where each card's default copy lives outside ``card.<key>``. Most features
#: render catalog text from their own section (``about.title``,
#: ``bump.messages.thank_you_description``, ...), while ``card.<key>`` only
#: carries labels — so the map points at the keys the send site really
#: resolves. Entry: ``(container_color | None, heading_key | None, parts)``.
#: A part is a catalog key, a literal template, or a ``(label_key, value_key)``
#: tuple. Used when a theme overrides a card but the feature has no builder:
#: the themed document replaces the whole message, while this map only powers
#: test-sends and previews of *default* content (see :func:`default_document`).
SEED_PARTS_BY_KEY: dict[str, tuple[str | None, str | None, tuple[Any, ...]]] = {
    "about.card": ("brand", "about.title", ("about.text", "about.version")),
    "bump.reminder": (
        "brand",
        "bump.messages.reminder_title",
        ("bump.messages.reminder_description",),
    ),
    "bump.thank_you": (
        "brand",
        "bump.messages.thank_you_title",
        ("bump.messages.thank_you_description",),
    ),
    "bump.leaderboard": (
        "warning",
        "bump.messages.leaderboard_title",
        ("bump.messages.leaderboard_description",),
    ),
    "bump.no_bumps": (
        "info",
        "bump.messages.no_bumps_title",
        ("bump.messages.no_bumps_description",),
    ),
    "bump.anti_camping": (None, None, ("bump.messages.anti_camping",)),
    "bump.schedule.open": (None, None, ("bump.schedule.open_message_default",)),
    "bump.schedule.close": (None, None, ("bump.schedule.close_message_default",)),
    # List cards: the send site builds the body and passes it as {body}.
    "invites.leaderboard": ("info", "invites.leaderboard.title", ("{body}",)),
    "invites.stats": ("info", "invites.stats.title", ("{body}",)),
    "invites.personal": ("info", "invites.personal.title", ("{body}",)),
    "invites.invited_list": ("info", "invites.invited.title", ("{body}",)),
    "invites.server_stats": ("info", "invites.server.title", ("{body}",)),
    "invites.invited_by": ("info", "invites.inviter.title", ("invites.inviter.body",)),
    "tickets.panel": ("brand", "tickets.panel.title", ("tickets.panel.text",)),
    "tickets.created": ("brand", "tickets.created.title", ("tickets.created.text",)),
    "partnerships.invite": (None, None, ("partnerships.invite_text",)),
    "cleaner.confirm": (None, None, ("cleaner.confirm_text",)),
    "boost.preview": (
        "success",
        None,
        (
            "# @{role_name}",
            ("boost.emoji.owner", "{owner}"),
            ("boost.emoji.members", "boost.descriptions.members_count"),
        ),
    ),
}


async def _resolve_parts(raw, guild_id: int, parts: tuple[Any, ...]):
    """Yield each seed part resolved (see :func:`_resolve_seed_part`)."""
    for part in parts:
        yield await _resolve_seed_part(raw, guild_id, part)


async def _resolve_seed_part(raw, guild_id: int, part: Any) -> str:
    """One seed part: catalog key, ``(label_key, value)`` entry, or literal."""
    if isinstance(part, tuple):
        label_key, value_key = part
        label = await raw(guild_id, label_key)
        if not isinstance(label, str) or label == label_key or not label.strip():
            label = label_key.rsplit(".", 1)[-1].replace("_", " ")
        value = await raw(guild_id, value_key)
        if not isinstance(value, str) or value == value_key:
            value = value_key
        return f"**{label}:**\n{value}"
    found = await raw(guild_id, part)
    if isinstance(found, str) and found != part:
        return found
    return part


def _seed_document(bot, guild_id: int | None, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate-free seed doc: theme emojis resolve, placeholders stay literal."""
    from rosemary.core.themes import theme_for

    mapping = {
        **(getattr(theme_for(bot, guild_id), "emojis", {}) or {}),
        **ECHO_VARIABLES,
    }

    def walk(items: list[dict[str, Any]]) -> None:
        for block in items:
            if block.get("type") == "text":
                block["body"] = safe_format(str(block["body"]), mapping)
            walk(block.get("children") or [])

    walk(blocks)
    return {"v": DOCUMENT_VERSION, "blocks": blocks}


async def default_document(bot, guild_id: int, key: str) -> dict[str, Any] | None:
    """The feature's default document for ``key`` (no theme override).

    Resolution order:
    1. a feature-registered default builder (rich cards with custom layouts);
    2. the declared seed map (:data:`SEED_PARTS_BY_KEY`);
    3. plain ``card.<key>`` / ``<key>`` scalars (DM and log cards);
    4. ``None`` when the feature resolves no catalog copy at all.
    """
    builder = get_default_builder(key)
    if builder is not None:
        try:
            doc = await builder(bot, guild_id)
        except Exception as exc:
            log.warning("default builder failed for card %s: %s", key, exc)
        else:
            if isinstance(doc, dict) and isinstance(doc.get("blocks"), list):
                return copy.deepcopy(doc)
    raw = getattr(bot.translator, "raw", None)
    if not callable(raw):
        return None
    plan = SEED_PARTS_BY_KEY.get(key)
    if plan is not None:
        color, heading_key, parts = plan
        bodies = [body async for body in _resolve_parts(raw, guild_id, parts) if body.strip()]
        heading = None
        if heading_key is not None:
            found = await raw(guild_id, heading_key)
            if isinstance(found, str) and found.strip() and found != heading_key:
                heading = f"# {found}"
        if heading is None and not bodies:
            return None
        blocks: list[dict[str, Any]] = []
        if heading is not None:
            blocks.append({"type": "text", "body": heading})
        blocks.extend({"type": "text", "body": body} for body in bodies)
        if color is not None:
            blocks = [{"type": "container", "color": color, "children": blocks}]
        return _seed_document(bot, guild_id, blocks)
    lines: list[str] = []
    for candidate_key in (f"card.{key}", key):
        candidate = await raw(guild_id, candidate_key)
        if isinstance(candidate, str) and candidate.strip() and candidate != candidate_key:
            lines.append(candidate)
    if not lines:
        return None
    blocks = [{"type": "text", "body": line} for line in lines]
    return _seed_document(bot, guild_id, blocks)


async def get_effective_document(bot, guild_id: int, key: str) -> dict[str, Any] | None:
    """Themed override, else the feature's default document (or ``None``)."""
    themed = await card_document(bot, guild_id, key)
    if themed is not None:
        return copy.deepcopy(themed)
    return await default_document(bot, guild_id, key)


async def render_document(
    bot,
    doc: dict[str, Any],
    variables: dict[str, Any] | None = None,
    *,
    guild_id: int | None = None,
    card_key: str | None = None,
    draft: bool = False,
) -> discord.ui.DesignerView:
    """Render one validated document into a Components V2 view.

    ``guild_id`` selects the theme used for emoji tokens and color names —
    the guild's active theme when known, the built-in one otherwise.
    ``CardsError`` is intentionally allowed to propagate so callers can show
    the structured issue instead of silently substituting another message.
    """
    from rosemary.core.card_actions import bind_action_callbacks
    from rosemary.core.themes import theme_for

    items = build_items(
        theme_for(bot, guild_id),
        doc,
        variables or {},
        draft=draft,
        card_key=card_key,
    )
    bind_action_callbacks(items)
    view = discord.ui.DesignerView(store=False)
    for item in items:
        view.add_item(item)
    return view


async def render_card_message(
    bot,
    guild_id: int,
    key: str,
    variables: dict[str, Any] | None = None,
    *,
    silent: bool = False,
) -> tuple[discord.ui.DesignerView | None, discord.AllowedMentions]:
    """Render a card for a real send: ``(view, allowed_mentions)``.

    Resolution: themed override else the feature default — ``None`` only when
    the card resolves no document at all, in which case the caller falls back
    to its own default view and computes mentions for that text via
    ``mentions.allowed_for_text``. Otherwise the returned ``allowed_mentions``
    is derived from the document itself: only ``<@id>``/``<@&id>`` tokens the
    resolved text actually contains may ping, and only when the theme's pings
    toggle for the card is on (or the caller forces ``silent``). An invalid
    document renders ``None`` so the caller's default path takes over — sends
    never break on themed content.
    """
    from rosemary.core.mentions import allowed_for_document
    from rosemary.core.themes import theme_for

    doc = await get_effective_document(bot, guild_id, key)
    if doc is None:
        return None, discord.AllowedMentions.none()
    mapping = {
        **(getattr(theme_for(bot, guild_id), "emojis", {}) or {}),
        **(dict(ECHO_VARIABLES) if variables is None else variables),
    }
    try:
        view = await render_document(bot, doc, mapping, guild_id=guild_id, card_key=key)
    except CardsError as exc:
        log.warning("card %s for guild %s is invalid, using default: %s", key, guild_id, exc)
        return None, discord.AllowedMentions.none()
    allowed = await allowed_for_document(bot, guild_id, key, doc, mapping, silent=silent)
    await _trace_card_path(bot, guild_id, key)
    return view, allowed


async def _trace_card_path(bot, guild_id: int, key: str) -> None:
    """Post ``card.<key>`` + origin to the log channel when ``debug.card_paths``
    is on. Never pings (``card_key=None``) and never traces itself."""
    from rosemary.core.debug import send_channel_log
    from rosemary.core.settings import get_setting
    from rosemary.core.themes import card_origin

    try:
        enabled = await get_setting(bot.storage, guild_id, "debug.card_paths")
    except (KeyError, AttributeError):
        return
    if not enabled:
        return
    theme_name, _ = card_origin(bot, guild_id, key)
    origin = (
        await bot.translator.t(guild_id, "debug.trace.theme", theme=theme_name)
        if theme_name
        else await bot.translator.t(guild_id, "debug.trace.default")
    )
    description = await bot.translator.t(
        guild_id, "debug.trace.card_path", path=f"card.{key}", origin=origin
    )
    await send_channel_log(
        bot,
        guild_id,
        await bot.translator.t(guild_id, "debug.trace.title"),
        description,
        color="info",
    )


def log_default_failure(key: str, exc: Exception) -> None:
    """Warn that a card's default builder crashed (seed falls through)."""
    log.warning("default builder failed for card %s: %s", key, exc)


__all__ = [
    "SEED_PARTS_BY_KEY",
    "card_origin",
    "default_document",
    "get_effective_document",
    "log_default_failure",
    "render_card_message",
    "render_document",
    "theme_for",
]
