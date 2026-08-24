"""Small reusable pagination helpers for V2 menus.

Menus keep ``page`` state themselves; this module only provides the math and the
prev/next button row so every list screen (server roles, my roles, admin list)
builds it the same way.
"""

from __future__ import annotations

import discord

from rosemary.ui.menu import MenuView


def page_count(total: int, page_size: int) -> int:
    """Number of pages needed for ``total`` items at ``page_size`` each."""
    if total <= 0:
        return 1
    return (total + page_size - 1) // page_size


def paginate(items: list, page: int, page_size: int) -> list:
    """Return the slice of ``items`` belonging to ``page`` (0-based)."""
    if page < 0:
        page = 0
    return items[page * page_size : (page + 1) * page_size]


def build_pager_row(
    view: MenuView,
    *,
    custom_id_prev: str,
    custom_id_next: str,
    page: int,
    total_pages: int,
    prev_label: str,
    next_label: str,
) -> discord.ui.ActionRow:
    """Build a prev/next ActionRow with buttons disabled at the ends."""
    return discord.ui.ActionRow(
        view.make_button(
            custom_id=custom_id_prev,
            label=prev_label,
            disabled=page <= 0,
        ),
        view.make_button(
            custom_id=custom_id_next,
            label=next_label,
            disabled=page >= total_pages - 1,
        ),
    )
