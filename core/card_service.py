"""Shared card operations for the /personalizar menu, editor and subcommands.

One place for: resolving the effective document (saved override else the
feature default), rendering it, and exporting/importing JSON. Views and
slash commands both call here so behavior never diverges.
"""

from __future__ import annotations

import copy
import json
import time
from typing import Any

import discord

from rosemary.core.cards import (
    build_items,
    get_card,
    get_default_builder,
)


async def get_effective_document(bot, guild_id: int, key: str) -> dict[str, Any] | None:
    """Saved override, else the feature's default document (or ``None``)."""
    from rosemary.core.cards import card_store

    saved = await card_store(bot).get_document(guild_id, key)
    if isinstance(saved, dict) and isinstance(saved.get("blocks"), list):
        return copy.deepcopy(saved)
    builder = get_default_builder(key)
    if builder is None:
        return None
    try:
        doc = await builder(bot, guild_id)
    except Exception:
        return None
    if isinstance(doc, dict) and isinstance(doc.get("blocks"), list):
        return doc
    return None


async def preview_mapping(bot, guild_id: int, key: str) -> dict[str, Any]:
    """Sample variables for a card: contract samples plus guild extras."""
    from rosemary.core.variables import samples_for

    spec = get_card(key)
    mapping = samples_for(spec.variables) if spec is not None else {}
    guild = bot.get_guild(guild_id) if hasattr(bot, "get_guild") else None
    mapping.update(
        server=getattr(guild, "name", "…"),
        user_name=getattr(getattr(guild, "me", None), "display_name", "@voce"),
    )
    return mapping


async def render_card(
    bot, guild_id: int, key: str, variables: dict[str, Any] | None = None
) -> discord.ui.DesignerView | None:
    """Render the effective document (``None`` when neither saved nor default)."""
    from rosemary.core.cards import CardsError

    doc = await get_effective_document(bot, guild_id, key)
    if doc is None:
        return None
    if variables is None:
        variables = await preview_mapping(bot, guild_id, key)
    try:
        items = build_items(bot.theme, doc, variables, card_key=key)
    except CardsError:
        return None
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


async def import_payload(bot, guild_id: int, key: str, payload: Any) -> tuple[bool, str]:
    """Validate and store an imported document. Returns ``(ok, reason)``.

    ``reason`` is ``""`` on success, else a short machine tag for the caller
    to translate (``"shape"`` or ``"invalid"``).
    """
    from rosemary.core.card_actions import sync_guild
    from rosemary.core.card_history import history_store
    from rosemary.core.cards import (
        card_store,
        ensure_ids,
        validate_document,
    )

    blocks = payload.get("blocks") if isinstance(payload, dict) else None
    if not isinstance(blocks, list) or not blocks:
        return False, "shape"
    doc = ensure_ids({"v": 2, "blocks": copy.deepcopy(blocks)})
    if validate_document(doc, theme=bot.theme, draft=True):
        return False, "invalid"
    await card_store(bot).save_document(guild_id, key, doc)
    await history_store(bot).append(guild_id, key, doc, None)
    await sync_guild(bot, guild_id)
    return True, ""
