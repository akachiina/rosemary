"""Shared card document operations for the editor and utilities.

This module resolves effective documents, renders documents, and parses or
serializes imports/exports. Parsing an import never persists it; callers decide
explicitly when a validated draft should be saved.
"""

from __future__ import annotations

import copy
import json
import time
from typing import Any

import discord

from rosemary.core.cards import (
    DOCUMENT_VERSION,
    ECHO_VARIABLES,
    build_items,
    get_default_builder,
    safe_format,
)


async def get_effective_document(bot, guild_id: int, key: str) -> dict[str, Any] | None:
    """Saved override, else the feature's default document (or ``None``)."""
    from rosemary.core.cards import card_store

    saved = await card_store(bot).get_document(guild_id, key)
    if isinstance(saved, dict) and isinstance(saved.get("blocks"), list):
        return copy.deepcopy(saved)
    return await catalog_default_document(bot, guild_id, key)


#: Where each card's default copy lives outside ``card.<key>``. Most features
#: render catalog text from their own section (``about.title``,
#: ``bump.messages.thank_you_description``, ...), while ``card.<key>`` only
#: carries editor labels — so the map points at the keys the send site really
#: resolves. Entry: ``(container_color | None, heading_key | None, parts)``.
#: A part is a catalog key, a literal template (used as-is when ``raw()``
#: echoes it back) or a ``(label_key, value_key_or_literal)`` tuple rendered
#: like theme ``entry`` markdown. Missing heading keys are skipped — raw keys
#: never reach the UI. Colors mirror the default builders' theme styles.
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


async def catalog_default_document(bot, guild_id: int, key: str) -> dict[str, Any] | None:
    """What members receive today, as an editable document (no builder needed).

    Resolution order:
    1. a feature-registered default builder (rich cards with custom layouts);
    2. the declared seed map (:data:`SEED_PARTS_BY_KEY`) — the catalog keys the
       feature itself renders, wrapped in the same container color;
    3. plain ``card.<key>`` / ``<key>`` scalars (DM and log cards);
    4. ``None`` when the feature resolves no catalog copy at all.

    Placeholders stay literal (theme emojis resolve), matching what the send
    sites render without an override. This is what the editor seeds from, so
    /personalizar always starts from the real message instead of an empty
    skeleton.
    """
    builder = get_default_builder(key)
    if builder is not None:
        try:
            doc = await builder(bot, guild_id)
        except Exception as exc:
            log_default_failure(key, exc)
        else:
            if isinstance(doc, dict) and isinstance(doc.get("blocks"), list):
                return doc
    raw = getattr(bot.translator, "raw", None)
    if not callable(raw):
        return None
    plan = SEED_PARTS_BY_KEY.get(key)
    if plan is not None:
        color, heading_key, parts = plan
        bodies = [
            body
            async for body in _resolve_parts(raw, guild_id, parts)
            if body.strip()
        ]
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
        return _seed_document(bot, blocks)
    lines: list[str] = []
    for candidate_key in (f"card.{key}", key):
        candidate = await raw(guild_id, candidate_key)
        if isinstance(candidate, str) and candidate.strip() and candidate != candidate_key:
            lines.append(candidate)
    if not lines:
        return None
    blocks = [{"type": "text", "body": line} for line in lines]
    return _seed_document(bot, blocks)


def _seed_document(bot, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate-free seed doc: theme emojis resolve, placeholders stay literal."""
    mapping = {**(getattr(bot.theme, "emojis", {}) or {}), **ECHO_VARIABLES}

    def walk(items: list[dict[str, Any]]) -> None:
        for block in items:
            if block.get("type") == "text":
                block["body"] = safe_format(str(block["body"]), mapping)
            walk(block.get("children") or [])

    walk(blocks)
    return {"v": DOCUMENT_VERSION, "blocks": blocks}


async def render_document(
    bot,
    doc: dict[str, Any],
    variables: dict[str, Any] | None = None,
    *,
    card_key: str | None = None,
    draft: bool = False,
) -> discord.ui.DesignerView:
    """Render one validated document into a Components V2 view.

    ``CardsError`` is intentionally allowed to propagate so editors can show
    the structured translated issue instead of silently substituting another
    message.
    """
    from rosemary.core.card_actions import bind_action_callbacks

    items = build_items(
        bot.theme,
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


def export_payload(key: str, doc: dict[str, Any]) -> tuple[str, bytes]:
    """Serialize one document for download."""
    payload = {
        "v": 2,
        "key": key,
        "exported_at": int(time.time()),
        "blocks": copy.deepcopy(doc.get("blocks", [])),
    }
    data = json.dumps(payload, indent=2, ensure_ascii=False).encode()
    return f"{key.replace('.', '-')}.json", data


def parse_import_payload(
    payload: Any,
    theme: Any | None = None,
    *,
    expected_key: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Validate an exported card without persisting it.

    Returns ``(document, "")`` on success. A missing/mismatched key, bad shape
    or invalid document returns ``(None, "invalid")`` (``"shape"`` for a missing
    block list), letting the editor keep the current draft untouched.
    """
    from rosemary.core.cards import ensure_ids, validate_document

    if not isinstance(payload, dict):
        return None, "shape"
    version = payload.get("v")
    blocks = payload.get("blocks")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in (1, 2)
        or not isinstance(blocks, list)
        or not blocks
    ):
        return None, "shape"
    if expected_key is not None:
        exported_key = payload.get("key")
        if exported_key not in (None, expected_key):
            return None, "invalid"
    doc = ensure_ids({"v": DOCUMENT_VERSION, "blocks": copy.deepcopy(blocks)})
    if validate_document(doc, theme=theme, draft=True):
        return None, "invalid"
    return doc, ""


async def import_payload(bot, guild_id: int, key: str, payload: Any) -> tuple[bool, str]:
    """Validate and persist an imported document. Returns ``(ok, reason)``."""
    from rosemary.core.card_actions import sync_guild
    from rosemary.core.card_history import history_store
    from rosemary.core.cards import card_store

    doc, reason = parse_import_payload(payload, bot.theme, expected_key=key)
    if doc is None:
        return False, reason
    await card_store(bot).save_document(guild_id, key, doc)
    await history_store(bot).append(guild_id, key, doc, None)
    await sync_guild(bot, guild_id)
    return True, ""


def log_default_failure(key: str, exc: Exception) -> None:
    """Warn that a card's default builder crashed (seed falls through)."""
    import logging

    logging.getLogger(__name__).warning(
        "default builder failed for card %s: %s", key, exc
    )
