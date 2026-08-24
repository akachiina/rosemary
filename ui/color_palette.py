"""Pastel role-color palette shared by boost-role menus.

Each entry references an i18n key for its translated label; the hex value is
what gets applied to the role. The "custom" option is handled by the menu itself
(opens a hex-color modal), not present here.
"""

from __future__ import annotations

#: (translation key, hex color without '#')
COLOR_PALETTE: list[tuple[str, str]] = [
    ("boost.colors.pink", "FFB3BA"),
    ("boost.colors.peach", "FFDFBA"),
    ("boost.colors.yellow", "FFFFBA"),
    ("boost.colors.green", "BAFFC9"),
    ("boost.colors.blue", "BAE1FF"),
    ("boost.colors.lavender", "E0BBE4"),
    ("boost.colors.beige", "F4E4C1"),
    ("boost.colors.gray", "D3D3D3"),
    ("boost.colors.white", "FFF9F0"),
]
