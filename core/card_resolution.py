"""Which document answers for a card.

The resolution order is the one the whole system agrees on:

1. the guild's theme writes a **semantic delta** over the packaged document
   (the fields it does not declare are inherited);
2. the guild's theme writes a **legacy full card** (the raw Components V2 of
   the old themes): it replaces the card whole;
3. the **packaged document** answers (the default package, the source of
   truth).

The theme always wins, content *and* shape. Without a themed document the
packaged document decides the shape, unless the theme overrides the form
(``cards.<key>.form: embed``) or the caller forces one (a repaint keeps the
shape of the message it edits).

A broken document never breaks a send: the resolution falls back to the
packaged document (or to nothing, and the call site keeps its own default).
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from rosemary.core.design.ir import Card

log = logging.getLogger(__name__)


async def get_effective_card(
    bot,
    guild_id: int,
    key: str,
    variables: dict[str, Any] | None = None,
    form: str | None = None,
) -> Card | None:
    """The card to render: the theme's override, else the packaged document.

    ``form`` forces the shape (the repaint path) and the guild's form
    override picks the shape of the packaged content.
    """
    from rosemary.core.themes import card_delta, card_document

    delta = await card_delta(bot, guild_id, key)
    if delta is not None:
        card = await _delta_card(bot, guild_id, key, delta, variables or {}, form)
        if card is not None:
            return card
    themed = await card_document(bot, guild_id, key)
    if themed is not None:
        card = _themed_card(bot, guild_id, key, themed, form)
        if card.copy_only:
            # The theme wrote copy, not a layout: the card keeps its own
            # surface (the tone of the notice and the contract).
            style, contract = _packaged_surface(key)
            card = dataclasses.replace(card, style=style, variables=contract)
        return card
    return await _packaged_card(bot, guild_id, key, variables or {}, form)


def _packaged_surface(key: str) -> tuple[str, tuple[str, ...]]:
    """The surface of the packaged card: the tone and the contract.

    A themed document that carries only copy keeps the card's own surface:
    the tone of the notice prefix and the contract (the names the send site
    delivers) belong to the card, not to the copy a theme writes.
    """
    from rosemary.core.theme_package import default_package

    raw = default_package().document(key) or {}
    style = str(raw.get("style") or "info")
    contract = tuple(str(name) for name in (raw.get("variables") or ()))
    return style, contract


async def _packaged_card(
    bot,
    guild_id: int,
    key: str,
    variables: dict[str, Any],
    form: str | None = None,
) -> Card | None:
    """The packaged document of the card (the source of truth).

    ``None`` when the package carries no document for the key: the call site
    keeps its own default.
    """
    from rosemary.core.documents.semantic import build_card
    from rosemary.core.documents.templates import Resolver
    from rosemary.core.theme_package import default_package
    from rosemary.core.themes import card_form, theme_for

    raw = default_package().document(key)
    if raw is None:
        return None
    theme = theme_for(bot, guild_id)
    resolver = Resolver(
        bot,
        guild_id,
        variables or {},
        emojis=getattr(theme, "emojis", {}) or {},
    )
    card = await build_card(raw, key, resolver)
    legacy = await _legacy_body(bot, guild_id, key, raw, variables)
    if legacy:
        # The old body card of a guild theme: it replaces the body (and
        # the fields) of the packaged document, keeping its frame.
        card = dataclasses.replace(card, paragraphs=(legacy,), fields=())
    shape = form or card_form(bot, guild_id, key)
    if shape and shape != card.form:
        card = dataclasses.replace(card, form=shape)
    return card


async def _delta_card(
    bot,
    guild_id: int,
    key: str,
    delta: dict[str, Any],
    variables: dict[str, Any],
    form: str | None,
) -> Card | None:
    """A themed delta: the packaged document with the theme's own fields.

    The delta names the fields it changes; the fields it does not declare
    are inherited from the packaged document (the frame stays the packaged
    one). A broken delta never breaks a send: the card falls back to the
    packaged document.
    """
    from rosemary.core.documents.merge import DocumentError, merge, resolve_layers
    from rosemary.core.documents.semantic import build_card
    from rosemary.core.documents.templates import Resolver
    from rosemary.core.theme_package import default_package
    from rosemary.core.themes import card_form, theme_for

    base = default_package().document(key)
    if base is None:
        # No packaged document to inherit from: the delta has nothing to
        # customize.
        return None
    theme = theme_for(bot, guild_id)
    try:
        resolved = resolve_layers(
            delta,
            getattr(theme, "archetypes", {}) or {},
            getattr(theme, "fragments", {}) or {},
        )
        merged = merge(base, resolved)
    except DocumentError as exc:
        log.warning("themed delta for %s is invalid: %s", key, exc)
        return None
    resolver = Resolver(
        bot, guild_id, variables, emojis=getattr(theme, "emojis", {}) or {}
    )
    try:
        card = await build_card(merged, key, resolver)
    except DocumentError as exc:
        log.warning("themed delta for %s is invalid: %s", key, exc)
        return None
    if not resolved.get("description") and not resolved.get("paragraphs"):
        legacy = await _legacy_body(bot, guild_id, key, base, variables)
        if legacy:
            # The old body card of a guild theme: it replaces the body (and
            # the fields) of the packaged document, keeping its frame.
            card = dataclasses.replace(card, paragraphs=(legacy,), fields=())
    shape = form or resolved.get("form") or card_form(bot, guild_id, key)
    if shape and shape != card.form:
        card = dataclasses.replace(card, form=shape)
    return card


def _themed_card(
    bot,
    guild_id: int,
    key: str,
    themed: dict[str, Any],
    form: str | None,
) -> Card:
    """The themed document, as a card of the same model.

    A themed document always wins over the packaged one. A document that
    carries only copy (no layout) keeps the card's own surface: a text card
    stays one line, an embed card stays an embed. A structural document
    asked for a rich layout, so it ships as Components V2.
    """
    from rosemary.core.cards import default_form
    from rosemary.core.documents.legacy import document_to_card
    from rosemary.core.themes import card_form

    card = document_to_card(themed, key=key)
    shape = form or card_form(bot, guild_id, key)
    if shape is None and card.copy_only:
        shape = default_form(key)
    if shape and shape != card.form:
        card = dataclasses.replace(card, form=shape)
    return card


async def _legacy_body(
    bot,
    guild_id: int,
    key: str,
    raw: dict[str, Any],
    variables: dict[str, Any] | None,
) -> str | None:
    """The old body-card override of a guild theme (compatibility).

    Before the documents, a guild could customize the body of a log under the
    key of its catalog copy (``cards.<event>.description``). That override
    still wins over the packaged body; the frame (title, color, footer)
    stays the document's.
    """
    from rosemary.core.cards import maybe_flat_text
    from rosemary.core.theme_package import legacy_body_key

    legacy_key = legacy_body_key(raw, key)
    if not legacy_key:
        return None
    return await maybe_flat_text(bot, guild_id, legacy_key, **(variables or {}))


def reserve_contract(
    card: Card,
    mapping: dict[str, Any],
    variables: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep the card's contract names out of the theme emoji fallback.

    The theme's emoji tokens and the send-site variables share one namespace.
    When a name belongs to the card's contract but the send site did not
    provide it, the placeholder stays literal: without this, a missing
    ``{user}`` would resolve to the theme's "user" emoji and the card would
    show a person icon where the member should be.
    """
    provided = variables or {}
    out = dict(mapping)
    for name in getattr(card, "variables", ()) or ():
        if name not in provided:
            out.pop(name, None)
    return out


__all__ = ["get_effective_card", "reserve_contract"]
