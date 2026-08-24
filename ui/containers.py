"""Reusable Components V2 container builders for Rosemary.

Everything here renders through ``DesignerView`` + ``Container`` items; no
embeds, no ``content=`` messages.
"""

from __future__ import annotations

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
]


def header_display(title: str, description: str = "") -> TextDisplay:
    """Build a TextDisplay that acts as a message header.

    Args:
        title: The title line (Markdown allowed).
        description: Optional sub-line below the title.
    """
    content = title if not description else f"{title}\n{description}"
    return TextDisplay(content)


def divider(spacing: str = "small") -> Separator:
    """Build a visual separator.

    Args:
        spacing: ``"small"`` or ``"large"``.
    """
    if spacing == "large":
        size = discord.SeparatorSpacingSize.large
    else:
        size = discord.SeparatorSpacingSize.small
    return Separator(spacing=size)


def designer_container(color: discord.Colour, *items: discord.ui.ViewItem) -> Container:
    """Assemble a Container with an accent color and the given V2 items."""
    return Container(*items, color=color)
