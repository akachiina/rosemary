"""Form conversions: one document, three shapes (Components V2, embed, text).

A card's default content is always a Components V2 document (the format the
card editor speaks). The *form* decides how it ships:

* ``card``: the V2 document as-is;
* ``embed``: the classic embed (visual info, staff logs);
* ``text``: the plain text (``{emoji} message``, one thing per line).

The generic converters here do the translation so no card needs a hand-made
builder for another form. The semantic model (the Card IR) writes every form
from the same slots, so a card owns its layout in the document, not in code.

The embed conversion is best effort by nature: an embed has one title, one
description, one image and one thumbnail, while V2 nests containers, sections
and galleries. What cannot be represented is dropped with a warning, never
raised: a send never breaks because of a shape.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

#: Discord's embed limits (mirrors core.embed_convert).
TITLE_MAX = 256
DESCRIPTION_MAX = 4096
FIELD_COUNT_MAX = 25
FIELD_NAME_MAX = 256
FIELD_VALUE_MAX = 1024
FOOTER_TEXT_MAX = 2048
TOTAL_MAX = 6000

#: A line shaped like "**Label:** value" becomes a real embed field.
_FIELD_LINE_RE = re.compile(r"^\*\*([^*\n]{1,256}):\*\*\s*(.*)$", re.DOTALL)

#: The small-text footer line the theme writes ("-# *text*").
_FOOTER_RE = re.compile(r"^-#\s*(.*)$", re.DOTALL)


def _flatten(blocks: list[Any]) -> list[dict[str, Any]]:
    """Every leaf block of the document, in order (containers unwrapped)."""
    flat: list[dict[str, Any]] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "container":
            flat.extend(_flatten(block.get("children") or []))
        elif kind == "section":
            accessory = block.get("accessory")
            if isinstance(accessory, dict) and accessory.get("type") == "thumbnail":
                flat.append({"type": "thumbnail", "url": accessory.get("url")})
            flat.extend(_flatten(block.get("children") or []))
        else:
            flat.append(block)
    return flat


def _container_color(blocks: list[Any]) -> str | None:
    """The color token of the first container (the card's accent)."""
    for block in blocks or []:
        if isinstance(block, dict) and block.get("type") == "container":
            color = block.get("color")
            if isinstance(color, str) and color.strip():
                return color.strip()
    return None


#: A heading is a line starting with "#" (the Components V2 title marker).
#: The match stops at the first line break: a body may carry more text
#: after the title (a catalog copy that opens with a heading).
_HEADING_RE = re.compile(r"^#[ \t]*([^\n]*)")


def _heading_parts(body: str) -> tuple[str, str] | None:
    """Split a heading body into ``(title, rest)`` (``None`` when not one).

    A heading may carry more than its first line: the title takes the first
    line and the rest belongs to the description. An empty heading (a bare
    "#") is not a title at all: the body stays prose.
    """
    text = body.strip()
    match = _HEADING_RE.match(text)
    if match is None:
        return None
    title = match.group(1).strip()
    if not title:
        return None
    rest = text[match.end() :].strip()
    return title, rest


def _split_fields(body: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Split a text body into ``(prose lines, fields)``.

    A body made *only* of "**Label:** value" lines becomes fields; a body
    with prose keeps its prose (a bold phrase inside a sentence is not a
    field).
    """
    lines = [line for line in body.split("\n") if line.strip()]
    if not lines:
        return [], []
    parsed = [_FIELD_LINE_RE.match(line) for line in lines]
    if all(parsed):
        fields = [(match.group(1).strip(), match.group(2).strip()) for match in parsed]
        return [], fields
    return lines, []


def _append_fields(
    fields: list[dict[str, Any]], entries: list[tuple[str, str]], *, inline: bool
) -> None:
    """Append the converted field entries (dropping the overflow)."""
    for name, value in entries:
        if len(fields) >= FIELD_COUNT_MAX:
            log.warning("embed conversion dropped a field: the limit is %d", FIELD_COUNT_MAX)
            break
        fields.append(
            {
                "name": name[:FIELD_NAME_MAX] or "\u200b",
                "value": (value or "\u200b")[:FIELD_VALUE_MAX],
                "inline": inline,
            }
        )


def to_embed_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert a V2 document into the embed form.

    The result is the internal embed document (``{"kind": "embed", ...}``), so
    it flows through the same renderer as a theme-written embed card.
    """
    blocks = list(doc.get("blocks") or [])
    embed: dict[str, Any] = {}
    description: list[str] = []
    fields: list[dict[str, Any]] = []
    buttons: list[dict[str, Any]] = []
    image_url: str | None = None
    thumbnail_url: str | None = None
    footer_text: str | None = None

    color = _container_color(blocks)
    if color:
        embed["color"] = color

    for block in _flatten(blocks):
        kind = block.get("type")
        if kind == "text":
            body = str(block.get("body") or "")
            stripped = body.strip()
            if not stripped:
                continue
            footer_match = _FOOTER_RE.match(stripped)
            if footer_match:
                footer_text = footer_match.group(1).strip().strip("*")
                continue
            if "title" not in embed:
                heading = _heading_parts(body)
                if heading is not None:
                    title, rest = heading
                    embed["title"] = title[:TITLE_MAX]
                    if rest:
                        prose, block_fields = _split_fields(rest)
                        _append_fields(fields, block_fields, inline=len(block_fields) > 1)
                        description.extend(prose)
                    continue
            prose, block_fields = _split_fields(body)
            _append_fields(fields, block_fields, inline=len(block_fields) > 1)
            description.extend(prose)
        elif kind == "gallery":
            for url in block.get("urls") or []:
                if image_url is None:
                    image_url = str(url)
                else:
                    log.warning("embed conversion dropped a gallery item: one image fits")
        elif kind == "thumbnail":
            if thumbnail_url is None:
                thumbnail_url = str(block.get("url") or "") or None
        elif kind == "row":
            for button in block.get("buttons") or []:
                if not isinstance(button, dict):
                    continue
                converted = {
                    key: button[key]
                    for key in ("label", "url", "action", "id", "emoji")
                    if button.get(key) is not None
                }
                if converted:
                    buttons.append(converted)

    if description:
        embed["description"] = "\n".join(description)[:DESCRIPTION_MAX]
    if fields:
        embed["fields"] = fields
    if image_url:
        embed["image"] = {"url": image_url}
    if thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}
    if footer_text:
        embed["footer"] = {"text": footer_text[:FOOTER_TEXT_MAX]}

    document: dict[str, Any] = {"kind": "embed", "embed": embed}
    if buttons:
        document["buttons"] = buttons
    return document


def to_text_content(doc: dict[str, Any]) -> str:
    """Reduce a V2 document to plain text (the ``text`` form).

    One thing per line: a heading becomes the bold title (without the ``#``),
    a field-shaped line becomes a bullet (``• **Label:** value``) and the
    small-text footer is frame, so it does not ship. The style prefix (the
    ``{emoji}``) is added by the caller.
    """
    lines: list[str] = []
    for block in _flatten(doc.get("blocks") or []):
        if block.get("type") != "text":
            continue
        lines.extend(_text_block_lines(str(block.get("body") or "")))
    return plain_text(lines)


def _text_block_lines(body: str) -> list[str]:
    """The plain-text lines of one V2 text block."""
    stripped = body.strip()
    if not stripped or _FOOTER_RE.match(stripped):
        return []
    heading = _heading_parts(body)
    if heading is not None:
        title, rest = heading
        return [f"**{title}**", *_text_block_lines(rest)]
    lines: list[str] = []
    for line in stripped.splitlines():
        if not line.strip():
            lines.append("")
            continue
        match = _FIELD_LINE_RE.match(line)
        if match is not None:
            lines.append(f"• **{match.group(1).strip()}:** {match.group(2).strip()}")
        else:
            lines.append(line.rstrip())
    return lines


#: The first line of a text already structured (a bold label, a bullet, a
#: quote or a code fence) is not a bare title: the serializer never wraps it
#: again.
_STRUCTURED_HEAD = ("**", "•", "- ", "* ", ">", "```")


def plain_text(lines: list[str]) -> str:
    """Join text lines in the plain-text grammar.

    One thing per line. The first line of a multi-line text is its title, in
    bold, so the notice prefix opens a titled line as ``{emoji} **Title**``;
    a line that already carries a structure (a bold label, a bullet, a quote)
    is kept as it is.
    """
    text = "\n".join(str(line) for line in lines).strip()
    if not text or "\n" not in text:
        return text
    first, rest = text.split("\n", 1)
    first = first.strip()
    if not first or first.startswith(_STRUCTURED_HEAD):
        return text
    # The whole line becomes the title: the emphasis it carried inside is
    # dropped (a bold marker inside a bold line breaks the markdown).
    first = first.replace("**", "")
    return f"**{first}**\n{rest}"


__all__ = [
    "DESCRIPTION_MAX",
    "FIELD_COUNT_MAX",
    "FIELD_NAME_MAX",
    "FIELD_VALUE_MAX",
    "FOOTER_TEXT_MAX",
    "TITLE_MAX",
    "TOTAL_MAX",
    "plain_text",
    "to_embed_document",
    "to_text_content",
]
