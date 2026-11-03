"""Pseudonymous identity (core/pseudonym.py): the seed, the color, the icon.

The identicon is a PNG (not an SVG) so the card can upload it with the
message and use it as the section's thumbnail, exactly like the audit logs
use the member's avatar.
"""

from __future__ import annotations

import io

from rosemary.core.pseudonym import (
    PSEUDONYM_COLORS,
    identicon_png,
    make_pseudonym,
    new_seed,
    palette_index,
)


def test_new_seed_is_three_readable_characters():
    seed = new_seed()
    assert len(seed) == 3
    assert seed.isalnum()
    # The glyphs that read alike are left out (0/O, 1/I, 5/S).
    assert not set(seed) & set("01IOS")


def test_the_color_is_stable_per_seed():
    assert palette_index("K7Q") == palette_index("K7Q")
    assert PSEUDONYM_COLORS[palette_index("K7Q")].startswith("#")


def test_identicon_is_a_png_and_deterministic():
    from PIL import Image

    first = identicon_png("K7Q")
    second = identicon_png("K7Q")
    assert first is not None
    assert first == second
    assert first.startswith(b"\x89PNG")
    with Image.open(io.BytesIO(first)) as image:
        assert image.size == (128, 128)


def test_different_seeds_draw_different_avatars():
    assert identicon_png("K7Q") != identicon_png("ABC")


def test_make_pseudonym_carries_the_seed_the_color_and_the_icon():
    identity = make_pseudonym("K7Q")
    assert identity.seed == "K7Q"
    assert identity.color in PSEUDONYM_COLORS
    assert identity.avatar.startswith(b"\x89PNG")
