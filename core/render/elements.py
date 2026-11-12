"""The drawn elements of a card: the pictures the bot draws for it.

An element is a picture the bot rasterizes for a card: a kind (``author``),
the HTML template the theme writes and the values the template consumes.
The PNG rides the message as an attachment and the card references it with
``attachment://<filename>``.

The renderer is optional: :func:`render_element` returns ``None`` when
WeasyPrint or PyMuPDF is missing, when the template fails or when the
result is blank. The caller then keeps the card's native shape, so a host
without the rasterizer ships the same card, only less pretty.

The pictures are cached: the same author and the same template draw the
same strip, and a card that ships again (a repaint) reuses it.
"""

from __future__ import annotations

import hashlib
import logging

log = logging.getLogger(__name__)

#: The kind of the strip that carries the author line.
AUTHOR = "author"

#: The ceiling of the picture cache (the PNGs are small, but a busy guild
#: draws many).
CACHE_SIZE = 128

#: The size of the drawn picture (the pixels Discord shows). This is the
#: button of the strip: the whole picture is scaled to this width, so the
#: avatar and the name shrink together and keep the proportion the theme
#: wrote. The strip is an author line, so it is small: the author line of
#: an embed is a 24px icon and a 14px name.
MAX_WIDTH = 240
MAX_HEIGHT = 120

#: The budget of one drawing (seconds). The document is local (the picture
#: is a data URI), so the renderer never waits on the network: the budget is
#: the safety net of a template that misbehaves.
DRAW_TIMEOUT = 10.0

#: The drawn pictures (the cache key -> the PNG).
_cache: dict[str, bytes] = {}


def render_element(kind: str, template: str, values: dict[str, str]) -> bytes | None:
    """The PNG of one element (``None`` when it cannot be drawn).

    The template is an HTML document: every ``{name}`` in it is replaced by
    the matching value. The values are already safe to put in the document
    (the text is escaped, the picture is a data URI).
    """
    if not template:
        return None
    key = _cache_key(kind, template, values)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    png = _rasterize(template, values)
    if png is None:
        return None
    if len(_cache) >= CACHE_SIZE:
        _cache.clear()  # crude LRU: one flush, cheap and bounded
    _cache[key] = png
    return png


def element_filename(kind: str, values: dict[str, str]) -> str:
    """The name of the file that rides the card (stable for the same values)."""
    digest = hashlib.sha1(_cache_key(kind, "", values).encode("utf-8")).hexdigest()
    return f"{kind}_{digest[:12]}.png"


def _cache_key(kind: str, template: str, values: dict[str, str]) -> str:
    """The key of one drawn picture (the kind, the template and the values)."""
    raw = "|".join([kind, template, *(f"{name}={value}" for name, value in sorted(values.items()))])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _rasterize(template: str, values: dict[str, str]) -> bytes | None:
    """The HTML template to a transparent PNG (the color panel's pipeline)."""
    html = template
    for name, value in values.items():
        html = html.replace("{" + name + "}", value)
    try:
        from weasyprint import HTML
    except Exception as exc:
        log.info("element renderer unavailable: %s", exc)
        return None
    try:
        pdf = HTML(string=html).write_pdf()
    except Exception:
        log.warning("element HTML render failed", exc_info=True)
        return None
    from rosemary.core.color_image import rasterize_pdf

    return rasterize_pdf(
        pdf, full_width=True, max_width=MAX_WIDTH, max_height=MAX_HEIGHT
    )


__all__ = [
    "AUTHOR",
    "CACHE_SIZE",
    "DRAW_TIMEOUT",
    "MAX_HEIGHT",
    "MAX_WIDTH",
    "element_filename",
    "render_element",
]
