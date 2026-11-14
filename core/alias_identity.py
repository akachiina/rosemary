"""The confession identity: the fruit alias and the random pastel accent.

The alias is a fruit drawn from the catalog list (one list per language, so
the copy stays translatable) and the accent is a random pastel (random hue,
fixed saturation/lightness ranges, customizable per call). Both are stored
per participant, so a member keeps the same name and color for the thread.
"""

from __future__ import annotations

import secrets

#: The default saturation range of the pastel accent.
DEFAULT_SATURATION: tuple[float, float] = (0.45, 0.70)

#: The default lightness range of the pastel accent.
DEFAULT_LIGHTNESS: tuple[float, float] = (0.62, 0.78)


def _unit() -> float:
    """A random float in ``[0, 1)`` (the secrets module has no float)."""
    return (secrets.randbits(53) + 1) / (1 << 53)


def _between(low: float, high: float) -> float:
    return low + (high - low) * _unit()


def random_alias(aliases) -> str:
    """One alias from the catalog list (empty when the list is empty)."""
    pool = [str(alias).strip() for alias in aliases if str(alias).strip()]
    return secrets.choice(pool) if pool else ""


def random_color(
    *,
    hue=None,
    saturation: tuple[float, float] = DEFAULT_SATURATION,
    lightness: tuple[float, float] = DEFAULT_LIGHTNESS,
) -> str:
    """A random pastel hex (``#rrggbb``), with customizable ranges.

    ``hue``: ``None`` draws the whole wheel, a float fixes the hue and a
    tuple draws inside the range (values are turns, 0..1). ``saturation``
    and ``lightness`` are the ranges the color is drawn from, so a guild
    can ask for a lighter or a stronger pastel without touching the code.
    """
    import colorsys

    if hue is None:
        picked_hue = _unit()
    elif isinstance(hue, tuple):
        picked_hue = _between(*hue)
    else:
        picked_hue = float(hue)
    picked_sat = _between(*saturation)
    picked_light = _between(*lightness)
    red, green, blue = colorsys.hls_to_rgb(
        picked_hue % 1.0,
        min(max(picked_light, 0.0), 1.0),
        min(max(picked_sat, 0.0), 1.0),
    )
    return f"#{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"


__all__ = [
    "DEFAULT_LIGHTNESS",
    "DEFAULT_SATURATION",
    "random_alias",
    "random_color",
]
