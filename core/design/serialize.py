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

import logging
import re
from typing import Any

from rosemary.core.design.ir import (
    Action,
    Card,
    Container,
    Divider,
    File,
    Gallery,
    Row,
    Section,
    Text,
    Thumbnail,
    blocks_to_document,
)

log = logging.getLogger(__name__)

#: A mention or a Discord time token. An embed does not parse either one in
#: the author and footer lines: the member would read the raw token.
_MENTION_TOKEN_RE = re.compile(r"<@[!&]?[0-9]{1,20}>")
_TIME_TOKEN_RE = re.compile(r"<t:[0-9]{1,20}(?::[tTdDfFR])?>")


def _clean_embed_slot(text: str, key: str, slot: str) -> str:
    """Drop an author/footer line that carries a mention or time token.

    The frame of an embed is written by the document, but a guild theme may
    reintroduce a token the packaged copy no longer carries. An embed parses
    neither one in these lines, so the line would show raw: the whole slot is
    dropped (never half a line) and the card is logged. A visual regression
    must not come back through a theme.
    """
    if _MENTION_TOKEN_RE.search(text) or _TIME_TOKEN_RE.search(text):
        log.warning(
            "card %s: %s carried a mention or date token; the line was dropped",
            key or "?",
            slot,
        )
        return ""
    return text.strip()


def _guard_embed_frame(embed: dict[str, Any], key: str) -> None:
    """Keep the frame of an embed readable and free of duplicated art.

    An author/footer line never carries a mention or a date token (an embed
    does not parse them there), and the thumbnail (the identity) never repeats
    the image (the content): the duplicate is the right-side icon, so it is
    the one dropped.
    """
    author = embed.get("author")
    if isinstance(author, dict):
        name = _clean_embed_slot(str(author.get("name") or ""), key, "author")
        if name:
            author["name"] = name
        else:
            embed.pop("author", None)
    footer = embed.get("footer")
    if isinstance(footer, dict):
        text = _clean_embed_slot(str(footer.get("text") or ""), key, "footer")
        if text:
            footer["text"] = text
        else:
            embed.pop("footer", None)
    thumbnail = embed.get("thumbnail")
    image = embed.get("image")
    if isinstance(thumbnail, dict) and isinstance(image, dict):
        thumbnail_url = str(thumbnail.get("url") or "")
        if thumbnail_url and thumbnail_url == str(image.get("url") or ""):
            embed.pop("thumbnail", None)
            log.warning(
                "card %s: the thumbnail repeated the image URL; the thumbnail was dropped",
                key or "?",
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


#: Discord's limit: a section carries three text blocks at most.
SECTION_TEXTS = 3


def _sections(body: list[Any]) -> list[list[Any]]:
    """The body of a card split into the groups a section can carry.

    A card that says more than three things (a title and three paragraphs)
    does not fit in one section: the body opens a second one instead of
    asking Discord for a 400.
    """
    return [
        body[index : index + SECTION_TEXTS]
        for index in range(0, len(body), SECTION_TEXTS)
    ]


def card_to_v2(card: Card, theme: Any = None) -> dict[str, Any]:
    """The internal Components V2 document of a card.

    A blocks-native card keeps its body. A slots-native card gets the standard
    frame: the opening line (the event sentence or the notification phrase)
    above the frame, the title and the description in a section (the
    thumbnail as its accessory), the fields as labelled lines, the footer as
    a small (``-#``) line and the actions as a row. The author line (the name
    in bold above the title) is drawn only when the document declares it: the
    content documents show the subject's picture as the thumbnail instead,
    and the author belongs to the classic embed.

    ``theme`` gives the title preset (the theme owns the markdown dialect);
    without it the heading is the plain ``# title``.
    """
    if card.blocks:
        return blocks_to_document(card.blocks)

    children: list[Any] = []
    body: list[Any] = []
    outside: list[Any] = []
    opening = card.content_line or card.ping_line
    if opening:
        # The opening line of the card: the event sentence (the content
        # line) or the notification phrase (the ping line). The embed
        # carries it in the message content, above the box; the V2 keeps
        # it above the frame too (a block of its own, outside the
        # container), so the sentence is not swallowed by the card.
        outside.append(Text(body=opening))
    if card.author is not None and card.author.name:
        # The author line: the member the card is about. The embed draws it
        # in its own frame (the name and the picture side by side); the V2
        # draws it in a section of its own, with the picture as its
        # accessory (the native thumbnail of the Components V2).
        children.append(
            Section(
                children=(Text(body=f"**{card.author.name}**"),),
                accessory=(
                    Thumbnail(url=card.author.icon_url) if card.author.icon_url else None
                ),
            )
        )
    if card.title:
        body.append(Text(body=_title_markdown(card, theme)))
    for paragraph in card.paragraphs:
        body.append(Text(body=paragraph))
    if body:
        if card.thumbnail:
            # The picture rides the first section (a section carries three
            # text blocks at most). A longer body opens plain text blocks
            # below it: a section without an accessory is not valid, and
            # py-cord would fill the empty accessory with a blank thumbnail.
            groups = _sections(body)
            children.append(
                Section(
                    children=tuple(groups[0]),
                    accessory=Thumbnail(url=card.thumbnail),
                )
            )
            for group in groups[1:]:
                children.extend(group)
        else:
            children.extend(body)
    if card.image:
        # The image rides a gallery (the V2 way of showing one picture).
        children.append(Gallery(urls=(card.image,)))
    if card.file:
        # A file (musi c/PDF) rides a File component: a V2 message hides
        # plain attachments, so the component is what exposes the upload.
        children.append(File(url=card.file))
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
    if not children and not outside:
        return {"v": 1, "blocks": []}
    if card.color and children:
        children = [Container(children=tuple(children), color=card.color)]
    return blocks_to_document(tuple(outside + children))


def card_to_embeds(card: Card, boxes: tuple[Card, ...] | None = None) -> list[dict[str, Any]]:
    """The embed documents of a message: one, or the list the card declares.

    A card that declares no ``embeds`` keeps its own slots as its single
    embed (the shape every card had). A card that declares the list stacks
    its boxes: the buttons of the message ride the first one, because the
    buttons belong to the message, not to a box.

    ``boxes`` names the boxes that actually ship (the caller drops the ones
    that say nothing): it defaults to the card's own list.
    """
    stack = card.embeds if boxes is None else boxes
    if not card.embeds:
        return [card_to_embed(card)]
    documents = [card_to_embed(sub) for sub in stack]
    if documents and card.actions and not documents[0].get("buttons"):
        documents[0]["buttons"] = [action_to_document(action) for action in card.actions]
    return documents


def card_to_embed(card: Card) -> dict[str, Any]:
    """The internal embed document of a card.

    A blocks-native card is converted (the legacy converter: the title, the
    description, the fields and the footer). A slots-native card writes its
    slots directly.
    """
    if card.blocks:
        from rosemary.core.forms import to_embed_document

        document = to_embed_document(blocks_to_document(card.blocks))
        _guard_embed_frame(document.get("embed") or {}, card.key)
        return document

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
    _guard_embed_frame(embed, card.key)
    document: dict[str, Any] = {"kind": "embed", "embed": embed}
    if card.actions:
        document["buttons"] = [action_to_document(action) for action in card.actions]
    line = card.content_line or card.ping_line
    if line:
        # The message content line of an embed card: an embed never notifies,
        # so the mention tokens travel here, inside a phrase the document
        # declares (never a bare token extracted from the embed body). The
        # content line wins over the ping line: it is the line that always
        # ships.
        document["content"] = line
    return document


def card_to_text(card: Card) -> str:
    """The plain text of a card: one thing per line.

    A content-native card keeps its text. A blocks-native card is reduced by
    the legacy converter. A slots-native card writes its slots: the title is
    the bold first line (the copy's own icon opens it), each paragraph rides
    its own lines, a field is a bullet (``• **Label:** value``) and the
    footer closes the text on its own line.
    """
    if card.content:
        return card.content
    if card.blocks:
        from rosemary.core.forms import to_text_content

        return to_text_content(blocks_to_document(card.blocks))

    from rosemary.core.forms import bold_title, plain_text

    lines: list[str] = []
    if card.title:
        title = bold_title(card.title)
        if title:
            lines.append(title)
    if card.content_line:
        # The event line opens the card: it is the sentence the member reads
        # first, so it keeps its own emphasis (the copy owns it).
        lines.append(str(card.content_line).strip())
    for paragraph in card.paragraphs:
        text = str(paragraph).strip()
        if text:
            lines.append(text)
    for field in card.fields:
        lines.append(f"• **{field.name}:** {field_value(field)}")
    if card.footer:
        footer = str(card.footer).strip()
        if footer:
            lines.append(footer)
    return plain_text(lines)


__all__ = [
    "action_to_document",
    "card_to_embed",
    "card_to_embeds",
    "card_to_text",
    "card_to_v2",
]
