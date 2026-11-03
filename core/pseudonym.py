"""Pseudonymous identity: the alias seed, its color and its identicon.

An anonymous participant is a random three-character seed, stable for the
whole confession thread. The seed picks a pastel color and draws a
deterministic identicon: the same seed always produces the same avatar
and two different seeds hardly ever collide. The identicon is a PNG (not
an SVG) so it can be uploaded with the message and used as the card's
thumbnail, exactly like the audit logs use the member's avatar.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import logging
import secrets

log = logging.getLogger(__name__)

#: Pastel palette shared by the pseudonymous names and the confession card
#: accents: one stable color per seed, so a participant keeps the same hue
#: for the whole confession thread.
PSEUDONYM_COLORS = (
    "#FFB7C5", "#FFD8A8", "#FFF3B0", "#B5E48C", "#99E2B4",
    "#88D8B0", "#90DBF4", "#A3C4F3", "#BDB2FF", "#FFC6FF",
)

#: Alias seeds avoid the glyphs that read alike (0/O, 1/I, 5/S).
_SEED_ALPHABET = "ABCDEFGHJKLMNPQRTUVWXYZ2346789"

#: Identicon edge (Discord's recommended size for an image).
IDENTICON_SIZE = 128

#: Drawing scale of the 5x5 grid before the resize to :data:`IDENTICON_SIZE`.
_GRID_SCALE = 32


@dataclasses.dataclass(frozen=True)
class Pseudonym:
    """A pseudonymous identity: its seed, its color and its identicon PNG."""

    seed: str
    color: str
    avatar: bytes  # PNG bytes (empty when Pillow is missing)


def new_seed() -> str:
    """A random three-character alias seed."""
    return "".join(secrets.choice(_SEED_ALPHABET) for _ in range(3))


def palette_index(seed: str) -> int:
    """Stable palette index for a seed (hash based, not Python's hash)."""
    digest = hashlib.sha256((seed or "?").encode("utf-8")).digest()
    return int.from_bytes(digest[:2], "big") % len(PSEUDONYM_COLORS)


def identicon_png(seed: str, *, size: int = IDENTICON_SIZE) -> bytes | None:
    """A deterministic identicon (PNG) for a pseudonymous seed.

    Five columns mirrored around the center, two colors from the pastel
    palette. ``None`` when Pillow is missing: the card then shows no
    thumbnail instead of breaking.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log.warning("Pillow is missing: the identicon avatar is disabled")
        return None
    index = palette_index(seed)
    color_a = PSEUDONYM_COLORS[index]
    color_b = PSEUDONYM_COLORS[(index + 5) % len(PSEUDONYM_COLORS)]
    digest = hashlib.sha256((seed or "?").encode("utf-8")).digest()
    bits = int.from_bytes(digest[2:4], "big")
    scale = _GRID_SCALE
    image = Image.new("RGB", (5 * scale, 5 * scale), color_a)
    draw = ImageDraw.Draw(image)
    for row in range(5):
        for col in range(3):
            if not (bits >> (row * 3 + col)) & 1:
                continue
            x = col if col < 2 else 4 - col
            draw.rectangle(
                [
                    x * scale,
                    row * scale,
                    (x + 1) * scale - 1,
                    (row + 1) * scale - 1,
                ],
                fill=color_b,
            )
    if size != 5 * scale:
        image = image.resize((size, size), Image.Resampling.NEAREST)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_pseudonym(seed: str) -> Pseudonym:
    """The identity of a seed: pastel color + generated identicon."""
    return Pseudonym(
        seed=seed,
        color=PSEUDONYM_COLORS[palette_index(seed)],
        avatar=identicon_png(seed) or b"",
    )


__all__ = [
    "IDENTICON_SIZE",
    "PSEUDONYM_COLORS",
    "Pseudonym",
    "identicon_png",
    "make_pseudonym",
    "new_seed",
    "palette_index",
]
