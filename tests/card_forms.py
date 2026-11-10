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


def payload_footer(payload: Any) -> str:
    """The footer text of a rendered payload (the embed footer)."""
    embed = getattr(payload, "embed", None)
    if embed is not None and embed.footer and embed.footer.text:
        return embed.footer.text
    return ""


def payload_thumbnail(payload: Any) -> str:
    """The thumbnail URL of a rendered payload (empty when there is none)."""
    embed = getattr(payload, "embed", None)
    if embed is not None and embed.thumbnail and embed.thumbnail.url:
        return embed.thumbnail.url
    return ""


def payload_image(payload: Any) -> str:
    """The image URL of a rendered payload (empty when there is none)."""
    embed = getattr(payload, "embed", None)
    if embed is not None and embed.image and embed.image.url:
        return embed.image.url
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
