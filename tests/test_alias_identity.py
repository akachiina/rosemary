"""The confession identity: the fruit alias and the random pastel accent.

The alias is drawn from the catalog list (the copy lives in the catalogs),
the accent is a random pastel whose ranges are customizable per call (a
guild can ask for a lighter or a stronger pastel without touching code).
"""

from __future__ import annotations

from rosemary.core.alias_identity import random_alias, random_color


def test_random_alias_picks_from_the_list():
    pool = ("Maçã", "Banana", "Morango")
    for _ in range(10):
        assert random_alias(pool) in pool
    assert random_alias([]) == ""


def test_random_color_is_a_pastel_hex():
    for _ in range(10):
        value = random_color()
        assert value.startswith("#") and len(value) == 7
        red, green, blue = (int(value[i : i + 2], 16) for i in (1, 3, 5))
        assert min(red, green, blue) > 40, value


def test_random_color_takes_custom_ranges():
    # A fixed hue and a fixed lightness/saturation is a constant color.
    assert random_color(hue=0.0, saturation=(0.5, 0.5), lightness=(0.5, 0.5)) == random_color(
        hue=0.0, saturation=(0.5, 0.5), lightness=(0.5, 0.5)
    )
