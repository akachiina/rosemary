"""Small reusable pagination helpers for V2 menus.

Menus keep ``page`` state themselves; this module only provides the math so
every list screen (server roles, my roles, admin list) slices it the same
way. Button rows stay in each menu: labels and disabled states are part of
the screen's own build step.
"""

from __future__ import annotations


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
