"""Serializers: one card model, three forms.

The card model (:mod:`rosemary.core.design.ir`) is semantic. This module
writes it in each form Discord understands:

* :func:`card_to_v2` builds the internal Components V2 document;
* :func:`card_to_embed` builds the internal embed document;
* :func:`card_to_text` reduces the card to one plain text.

The markdown dialect belongs here, not in the content: the Components V2
serializer writes the heading marker (``#``) and the small-text marker
(``-#``); the embed serializer writes neither, because an embed has a title
and a footer of its own.

The document-level converters (a Components V2 document into an embed or into
text) still live in :mod:`rosemary.core.forms`: they are the legacy path and
move here when the legacy path is deleted.
"""

from __future__ import annotations

from typing import Any

from rosemary.core.design.ir import (
    Action,
    Card,
    Container,
    Divider,
    Gallery,
    Row,
    Section,
    Text,
    Thumbnail,
    blocks_to_document,
)


def action_to_document(action: Action) -> dict[str, Any]:
    """One action as the document button shape."""
    return {
        key: value
        for key, value in (
            ("label", action.label),
            ("url", action.url),
            ("action", action.action),
            ("emoji", action.emoji),
            ("style", action.style),
            ("id", action.id),
        )
        if value
    }


def field_value(field: Any) -> str:
    """The value of a field, as inline code when it is a machine value."""
    value = str(field.value)
    if field.code and value.strip():
        return f"`{value}`"
    return value


def _title_markdown(card: Card, theme: Any = None) -> str:
    """The heading of the card, in the theme's title preset."""
    title = str(card.title)
    md = getattr(theme, "md", None)
    if callable(md):
        try:
            return str(md("title", title=title))
        except (KeyError, ValueError):
            pass
    return f"# {title}"


def card_to_v2(card: Card, theme: Any = None) -> dict[str, Any]:
    """The internal Components V2 document of a card.

    A blocks-native card keeps its body. A slots-native card gets the standard
    frame: the title and the description in a section (with the thumbnail as
    the accessory), the fields as labelled lines, the footer as a small
    (``-#``) line and the actions as a row.

    ``theme`` gives the title preset (the theme owns the markdown dialect);
    without it the heading is the plain ``# title``.
    """
    if card.blocks:
        return blocks_to_document(card.blocks)

    children: list[Any] = []
    body: list[Any] = []
    if card.title:
        body.append(Text(body=_title_markdown(card, theme)))
    for paragraph in card.paragraphs:
        body.append(Text(body=paragraph))
    if body:
        if card.thumbnail:
            children.append(
                Section(children=tuple(body), accessory=Thumbnail(url=card.thumbnail))
            )
        else:
            children.extend(body)
    if card.image:
        # The image rides a gallery (the V2 way of showing one picture).
        children.append(Gallery(urls=(card.image,)))
    if card.fields:
        lines = "\n".join(
            f"**{field.name}:** {field_value(field)}" for field in card.fields
        )
        children.append(Text(body=lines))
    if card.footer:
        children.append(Divider())
        # The footer is the small print: each line rides its own small
        # (``-#``) block, because the marker belongs to the line, not to the
        # whole text.
        for line in card.footer.splitlines():
            line = line.strip()
            if line:
                children.append(Text(body=f"-# {line}"))
    if card.actions:
        children.append(Row(buttons=tuple(card.actions)))
    if not children:
        return {"v": 1, "blocks": []}
    if card.color:
        children = [Container(children=tuple(children), color=card.color)]
    return blocks_to_document(tuple(children))


def card_to_embed(card: Card) -> dict[str, Any]:
    """The internal embed document of a card.

    A blocks-native card is converted (the legacy converter: the title, the
    description, the fields and the footer). A slots-native card writes its
    slots directly.
    """
    if card.blocks:
        from rosemary.core.forms import to_embed_document

        return to_embed_document(blocks_to_document(card.blocks))

    embed: dict[str, Any] = {}
    if card.title:
        embed["title"] = card.title
    if card.url:
        embed["url"] = card.url
    if card.paragraphs:
        # The body of a classic embed is one blob: the paragraphs meet on a
        # line break (the way they meet is the dialect of the form).
        embed["description"] = "\n".join(card.paragraphs)
    elif card.content:
        # A content-native card (a plain notice) has no slots: its copy is
        # the body of the embed.
        embed["description"] = card.content
    if card.color:
        embed["color"] = card.color
    if card.author is not None:
        author: dict[str, Any] = {"name": card.author.name}
        if card.author.icon_url:
            author["icon_url"] = card.author.icon_url
        embed["author"] = author
    if card.thumbnail:
        embed["thumbnail"] = {"url": card.thumbnail}
    if card.image:
        embed["image"] = {"url": card.image}
    if card.footer or card.footer_icon:
        footer: dict[str, Any] = {}
        if card.footer:
            footer["text"] = card.footer
        if card.footer_icon:
            footer["icon_url"] = card.footer_icon
        embed["footer"] = footer
    if card.timestamp:
        embed["timestamp"] = card.timestamp
    if card.fields:
        embed["fields"] = [
            {"name": field.name, "value": field_value(field), "inline": field.inline}
            for field in card.fields
        ]
    document: dict[str, Any] = {"kind": "embed", "embed": embed}
    if card.actions:
        document["buttons"] = [action_to_document(action) for action in card.actions]
    if card.ping_line:
        # The message content line of an embed card: an embed never notifies,
        # so the mention tokens travel here.
        document["content"] = card.ping_line
    return document


def card_to_text(card: Card) -> str:
    """The plain text of a card.

    A content-native card keeps its text. A blocks-native card is reduced by
    the legacy converter. A slots-native card joins its parts: one line when
    every part is a line, the original line breaks otherwise.
    """
    if card.content:
        return card.content
    if card.blocks:
        from rosemary.core.forms import to_text_content

        return to_text_content(blocks_to_document(card.blocks))

    parts: list[str] = []
    if card.title:
        parts.append(card.title)
    parts.extend(card.paragraphs)
    parts.extend(f"{field.name}: {field_value(field)}" for field in card.fields)
    if card.footer:
        parts.append(card.footer)
    if not parts:
        return ""
    if all("\n" not in part for part in parts):
        return " · ".join(parts).strip()
    return "\n".join(parts).strip()


__all__ = [
    "action_to_document",
    "card_to_embed",
    "card_to_text",
    "card_to_v2",
]
