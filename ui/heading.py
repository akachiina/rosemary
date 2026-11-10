"""Backward-compatible import of the kit's themed menu heading.

The helper moved to :mod:`rosemary.ui.kit` (``menu_heading_items``): the
heading is part of the panel frame, so it lives with the rest of the kit.
"""

from __future__ import annotations

from rosemary.ui.kit import menu_heading_items

__all__ = ["menu_heading_items"]
