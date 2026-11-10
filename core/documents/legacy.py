"""Legacy documents → the Card IR.

The bot still speaks the legacy document language everywhere: a Components V2
document (``{"v": 1, "blocks": [...]}``), an embed document
(``{"kind": "embed", "embed": {...}}``) or a text document
(``{"kind": "text", "content": "..."}``).

This module is the *only* place that reads that language: everything
downstream works with the semantic model (:mod:`rosemary.core.design.ir`),
so the legacy shapes can be retired one family at a time without touching the
renderers.

The reading is lossless: a document that goes through the IR and back
(:func:`rosemary.core.design.serialize.card_to_v2`,
:func:`~rosemary.core.design.serialize.card_to_embed`) keeps every field the
renderers read, including the editor's block ids.
"""

from __future__ import annotations

from typing import Any

from rosemary.core.cards import (
    FORM_CARD,
    FORM_EMBED,
    FORM_TEXT,
    CardIssue,
    CardsError,
    is_embed_document,
    is_text_document,
)
from rosemary.core.design.ir import (
    Action,
    Author,
    Card,
    Field,
    document_to_block,
)


def document_to_card(doc: Any, *, key: str = "") -> Card:
    """The IR card of a legacy document.

    The card is native in the shape the document speaks: a V2 document is a
    blocks card, an embed document is a slots card and a text document is a
    content card. A document that breaks the schema raises
    :class:`~rosemary.core.cards.CardsError`, so an invalid document falls
    back to the default message exactly like it does today.
    """
    if not isinstance(doc, dict):
        raise CardsError([CardIssue("document_shape")])
    if is_text_document(doc):
        return Card(key=key, form=FORM_TEXT, content=str(doc.get("content") or ""))
    if is_embed_document(doc):
        return _embed_card(doc, key=key)
    return _blocks_card(doc, key=key)


def _blocks_card(doc: dict[str, Any], *, key: str) -> Card:
    """A Components V2 document as a blocks-native card."""
    blocks = []
    for node in doc.get("blocks") or []:
        block = document_to_block(node)
        if block is None:
            # An unknown block type: the legacy validator rejects it, so the
            # card falls back instead of rendering a broken payload.
            raise CardsError([CardIssue("unknown_block")])
        blocks.append(block)
    return Card(key=key, form=FORM_CARD, blocks=tuple(blocks))


def _embed_card(doc: dict[str, Any], *, key: str) -> Card:
    """An embed document as a slots-native card."""
    raw = doc.get("embed")
    raw = raw if isinstance(raw, dict) else {}
    author = None
    raw_author = raw.get("author")
    if isinstance(raw_author, dict) and (
        raw_author.get("name") or raw_author.get("icon_url")
    ):
        author = Author(
            name=str(raw_author.get("name") or ""),
            icon_url=str(raw_author.get("icon_url") or ""),
        )
    footer = raw.get("footer")
    footer = footer if isinstance(footer, dict) else {}
    fields = []
    for field in raw.get("fields") or []:
        if not isinstance(field, dict):
            continue
        fields.append(
            Field(
                name=str(field.get("name") or ""),
                value=str(field.get("value") or ""),
                inline=bool(field.get("inline")),
            )
        )
    actions = []
    for button in doc.get("buttons") or []:
        if not isinstance(button, dict):
            continue
        actions.append(
            Action(
                label=str(button.get("label") or ""),
                url=str(button.get("url") or ""),
                action=str(button.get("action") or ""),
                emoji=str(button.get("emoji") or ""),
                style=str(button.get("style") or ""),
                id=str(button.get("id") or ""),
            )
        )
    timestamp = raw.get("timestamp")
    description = str(raw.get("description") or "")
    return Card(
        key=key,
        form=FORM_EMBED,
        color=str(raw.get("color") or ""),
        title=str(raw.get("title") or ""),
        url=str(raw.get("url") or ""),
        paragraphs=(description,) if description else (),
        author=author,
        thumbnail=str((raw.get("thumbnail") or {}).get("url") or ""),
        image=str((raw.get("image") or {}).get("url") or ""),
        footer=str(footer.get("text") or ""),
        footer_icon=str(footer.get("icon_url") or ""),
        timestamp=int(timestamp) if isinstance(timestamp, (int, float)) else None,
        fields=tuple(fields),
        actions=tuple(actions),
        ping_line=str(doc.get("content") or ""),
    )


__all__ = ["document_to_card"]
