"""Form-agnostic helpers for the card tests.

A card ships in one of three forms (Components V2, classic embed, plain
text). The tests assert on what the member actually receives, not on the
internal shape: these helpers read a rendered payload (or a raw document)
whatever form it took.
"""

from __future__ import annotations

from typing import Any


def view_texts(view: Any) -> list[str]:
    """Every text of a rendered view (nested-safe)."""
    texts: list[str] = []

    def walk(node: Any) -> None:
        content = getattr(node, "content", None)
        if isinstance(content, str) and content.strip():
            texts.append(content)
        children = (
            list(getattr(node, "children", []) or [])
            + list(getattr(node, "items", []) or [])
            + list(getattr(node, "walk_children", lambda: [])())
        )
        for child in children:
            walk(child)

    for item in getattr(view, "children", []) or []:
        walk(item)
    return texts


def embed_texts(embed: Any) -> list[str]:
    """Every text of a classic embed (title, description, fields, footer)."""
    if embed is None:
        return []
    texts = [embed.title or "", embed.description or ""]
    for field in embed.fields:
        texts.append(field.name or "")
        texts.append(field.value or "")
    if embed.footer and embed.footer.text:
        texts.append(embed.footer.text)
    if embed.author and embed.author.name:
        texts.append(embed.author.name)
    return [text for text in texts if text]


def payload_texts(payload: Any) -> list[str]:
    """Every text of a rendered card payload (view, embed or plain text)."""
    texts: list[str] = []
    if payload is None:
        return texts
    texts.extend(view_texts(getattr(payload, "view", None)))
    texts.extend(embed_texts(getattr(payload, "embed", None)))
    content = getattr(payload, "content", None)
    if isinstance(content, str) and content.strip():
        texts.append(content)
    return texts


class Sent:
    """A sent card, seen as a payload (the helpers read either shape)."""

    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.view = kwargs.get("view")
        embed = kwargs.get("embed")
        self.embed = embed
        self.embeds = tuple(kwargs.get("embeds") or ()) if embed is None else (embed,)
        self.files = list(kwargs.get("files") or [])
        self.content = kwargs.get("content")


def sent(call: Any) -> Sent:
    """The card of a ``send``/``edit`` call, as a payload."""
    return Sent(dict(getattr(call, "kwargs", {}) or {}))


def payload_footer(payload: Any) -> str:
    """The footer text of a rendered payload (the embed footer)."""
    embed = getattr(payload, "embed", None)
    if embed is not None and embed.footer and embed.footer.text:
        return embed.footer.text
    return "\n".join(payload_footer_lines(payload))


def payload_footer_lines(payload: Any) -> list[str]:
    """The footer lines of a rendered payload (the small print of the V2)."""
    embed = getattr(payload, "embed", None)
    if embed is not None and embed.footer and embed.footer.text:
        return [embed.footer.text]
    lines: list[str] = []

    def walk(node: Any) -> None:
        content = getattr(node, "content", None)
        if isinstance(content, str) and content.startswith("-# "):
            lines.append(content[3:].strip())
        for child in list(getattr(node, "children", []) or []) + list(
            getattr(node, "items", []) or []
        ):
            walk(child)

    view = getattr(payload, "view", None)
    for item in getattr(view, "children", []) or []:
        walk(item)
    return lines


def payload_thumbnail(payload: Any) -> str:
    """The thumbnail URL of a rendered payload (empty when there is none)."""
    embed = getattr(payload, "embed", None)
    if embed is not None and embed.thumbnail and embed.thumbnail.url:
        return embed.thumbnail.url
    return _first_accessory(payload)


def payload_image(payload: Any) -> str:
    """The image URL of a rendered payload (empty when there is none)."""
    embed = getattr(payload, "embed", None)
    if embed is not None and embed.image and embed.image.url:
        return embed.image.url
    return _first_gallery(payload)


def payload_author_icon(payload: Any) -> str:
    """The author icon URL of a rendered payload (empty when there is none).

    The V2 card draws the author line as a section of its own: the name in
    bold and the picture as the accessory (the native thumbnail). The embed
    draws the same picture in its own author frame.
    """
    embed = getattr(payload, "embed", None)
    if embed is not None and embed.author and embed.author.icon_url:
        return embed.author.icon_url
    return _first_accessory(payload)


def payload_accent(payload: Any) -> int:
    """The accent colour of a rendered payload (the container of the V2)."""
    embed = getattr(payload, "embed", None)
    if embed is not None and embed.colour:
        return embed.colour.value
    view = getattr(payload, "view", None)
    for item in getattr(view, "children", []) or []:
        colour = getattr(item, "colour", None)
        if colour is not None:
            return getattr(colour, "value", colour)
    return 0


def payload_pictures(payload: Any) -> list[str]:
    """Every picture of a rendered payload (the accessories and the gallery).
    """
    embed = getattr(payload, "embed", None)
    if embed is not None:
        found: list[str] = []
        if embed.thumbnail and embed.thumbnail.url:
            found.append(embed.thumbnail.url)
        if embed.image and embed.image.url:
            found.append(embed.image.url)
        if embed.author and embed.author.icon_url:
            found.append(embed.author.icon_url)
        return found
    found = []
    for node in _walk(payload):
        accessory = getattr(node, "accessory", None)
        url = getattr(accessory, "url", "")
        if isinstance(url, str) and url:
            found.append(url)
            continue
        if type(node).__name__ != "MediaGallery":
            continue
        for item in getattr(node, "items", []) or []:
            item_url = getattr(item, "url", "")
            if isinstance(item_url, str) and item_url:
                found.append(item_url)
    return found


def _walk(payload: Any):
    """Every item of the Components V2 view of a payload."""
    view = getattr(payload, "view", None)

    def walk(node: Any):
        yield node
        for child in list(getattr(node, "children", []) or []) + list(
            getattr(node, "items", []) or []
        ):
            yield from walk(child)

    for item in getattr(view, "children", []) or []:
        yield from walk(item)


def _first_accessory(payload: Any) -> str:
    """The URL of the first picture accessory of the V2 (the thumbnail)."""
    for node in _walk(payload):
        accessory = getattr(node, "accessory", None)
        url = getattr(accessory, "url", "")
        if isinstance(url, str) and url:
            return url
    return ""


def _first_gallery(payload: Any) -> str:
    """The URL of the first picture of the V2 (the gallery of the image).

    The image of the card is the picture the member asked for: the gallery
    is the only place it rides.
    """
    for node in _walk(payload):
        if type(node).__name__ != "MediaGallery":
            continue
        for item in getattr(node, "items", []) or []:
            url = getattr(item, "url", "")
            if isinstance(url, str) and url:
                return url
    return ""


def doc_texts(doc: Any) -> list[str]:
    """Every text of a default document, whatever its form (V2 or embed)."""
    if not doc:
        return []
    if doc.get("kind") == "embed":
        embed = doc.get("embed") or {}
        texts = [str(embed.get("title") or ""), str(embed.get("description") or "")]
        for field in embed.get("fields") or []:
            texts.append(str(field.get("name") or ""))
            texts.append(str(field.get("value") or ""))
        footer = embed.get("footer") or {}
        texts.append(str(footer.get("text") or ""))
        author = embed.get("author") or {}
        texts.append(str(author.get("name") or ""))
        return [text for text in texts if text]
    texts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("body"), str):
                texts.append(node["body"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc.get("blocks") or [])
    return texts


def doc_footer_texts(doc: Any) -> list[str]:
    """The footer lines of a default document (the attribution line)."""
    if not doc:
        return []
    if doc.get("kind") == "embed":
        footer = (doc.get("embed") or {}).get("footer") or {}
        return [footer["text"]] if footer.get("text") else []
    return [
        block["body"]
        for block in doc["blocks"][0]["children"]
        if block.get("type") == "text" and block.get("body", "").startswith("-# ")
    ]


def doc_buttons(doc: Any) -> list[dict[str, Any]]:
    """The buttons of a default document (the classic form carries them)."""
    if not doc:
        return []
    return [button for button in doc.get("buttons") or [] if isinstance(button, dict)]
