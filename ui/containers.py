"""Reusable Components V2 container builders for Rosemary.

Everything here renders through ``DesignerView`` + ``Container`` items: no
embeds, no ``content=`` messages. The only exception is
:func:`items_text`, which flattens V2 items into the plain-text layer (the
confirmation dialogs).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import discord

# Re-export the V2 layout primitives so cogs import from a single place.
Container = discord.ui.Container
Section = discord.ui.Section
Separator = discord.ui.Separator
TextDisplay = discord.ui.TextDisplay
ActionRow = discord.ui.ActionRow
DesignerView = discord.ui.DesignerView

__all__ = [
    "ActionRow",
    "Container",
    "DesignerView",
    "Section",
    "Separator",
    "TextDisplay",
    "header_display",
    "divider",
    "designer_container",
    "items_text",
]


def items_text(items: Iterable[Any]) -> str:
    """The text of V2 items, one block per line (the plain-text layer).

    A heading (``# title``) becomes a bold line: Discord only renders a
    heading at the start of a line, so the marker would show up as text in
    a plain message.
    """
    texts: list[str] = []

    def walk(node: Any) -> None:
        content = getattr(node, "content", None)
        if isinstance(content, str) and content.strip():
            body = content.strip()
            if body.startswith("# "):
                body = f"**{body[2:].strip()}**"
            texts.append(body)
        for child in list(getattr(node, "children", []) or []):
            walk(child)

    for item in items:
        walk(item)
    return "\n".join(texts)


def header_display(title: str, description: str = "") -> TextDisplay:
    """Build a TextDisplay that acts as a message header.

    Args:
        title: The title line (Markdown allowed).
        description: Optional sub-line below the title.
    """
    content = title if not description else f"{title}\n{description}"
    return TextDisplay(content)


def divider(spacing: str = "small", visible: bool = True) -> Separator:
    """Build a visual separator.

    Args:
        spacing: ``"small"`` or ``"large"``.
        visible: ``False`` renders an invisible spacer.
    """
    if spacing == "large":
        size = discord.SeparatorSpacingSize.large
    else:
        size = discord.SeparatorSpacingSize.small
    return Separator(spacing=size, divider=visible)


def designer_container(color: discord.Colour, *items: discord.ui.ViewItem) -> Container:
    """Assemble a Container with an accent color and the given V2 items."""
    return Container(*items, color=color)
