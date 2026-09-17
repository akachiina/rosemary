"""Color panel image: HTML template to transparent PNG.

The panel's look lives in a theme's ``color_panel`` section as two HTML
templates (:data:`DEFAULT_TEMPLATE` / :data:`DEFAULT_ITEM`): the bot injects
one ``item`` per color into the ``{items}`` slot and rasterizes the result.

Pipeline: WeasyPrint renders the HTML to a single-page PDF (its native
output; the page is sized in CSS pixels via ``@page``), PyMuPDF rasterizes
that page with an alpha channel, and the bitmap is cropped to the content's
bounding box so the transparent margin disappears. The result is a PNG with
a truly transparent background - Discord composites it over the chat
surface, matching a card written for a dark or light theme alike.

Both libraries are optional runtime deps (see ``requirements.txt``):
:func:`render_panel` returns ``None`` when either is missing or the template
fails to render, and every caller must treat that as "post the panel without
the image" - a missing gallery block simply vanishes from the card, so a
headless host without Pango never breaks color picking.
"""

from __future__ import annotations

import html as _html
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# WeasyPrint logs its render pipeline ("Step 2 - Fetching and parsing CSS")
# at INFO on its own logger; that is noise in bot.log on every repaint.
logging.getLogger("weasyprint").setLevel(logging.WARNING)

#: Hard rasterization cap: Discord downscales large images and the panel is
#: decorative, so oversized templates are clipped instead of failing.
_MAX_SIDE = 1600

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

#: Fallback templates (the theme file ships the same content). Placeholders
#: usable inside ``item``: ``{number}``, ``{name}``, ``{color}``, ``{hex}``.
#: ``@page`` sizing keeps WeasyPrint from emitting a default A4 page. Layout
#: uses multi-column + inline-block - the CSS subsets WeasyPrint renders
#: faithfully (its flexbox support is too weak for icon+label rows).
DEFAULT_TEMPLATE = """<style>
@page { size: __PANEL_W__px __PANEL_H__px; margin: 0; background: transparent; }
html, body { margin: 0; padding: 0; background: transparent; }
.grid {
  column-count: 2;
  column-gap: 64px;
  padding: 8px;
}
.item {
  font-family: "Segoe UI", "Ubuntu", "Noto Sans", sans-serif;
  font-size: 26px;
  font-weight: 600;
  color: #dbdee1;
  white-space: nowrap;
}
.dot {
  display: inline-block;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  vertical-align: -4px;
  margin-right: 10px;
}
</style>
<div class="grid">__ITEMS__</div>"""

#: Per-color markup: the color itself rides inline ({color}/{hex}), never in
#: the shared stylesheet - template-level CSS cannot vary per item.
DEFAULT_ITEM = (
    '<div class="item"><span class="dot" style="background: {color}"></span>'
    "{number}. {name}</div>"
)


@dataclass(frozen=True)
class PanelColor:
    """One row of the panel image (resolved values, not raw store dicts)."""

    number: int
    name: str
    color: str


def _css_color(value: str) -> str:
    """Normalize a stored color to ``#rrggbb`` for CSS injection.

    Invalid values resolve to a neutral gray - a bad role color must never
    break the whole render (the store only accepts validated hex anyway;
    this guards role colors edited mid-flight).
    """
    token = (value or "").strip()
    if not _HEX_RE.match(token):
        return "#99aab5"
    return token if token.startswith("#") else f"#{token}"


def default_templates() -> tuple[str, str]:
    """The built-in ``(template, item)`` pair (theme fallback)."""
    return DEFAULT_TEMPLATE, DEFAULT_ITEM


def _escape(text: str) -> str:
    """HTML-escape a color name before injecting it into the template."""
    return _html.escape(text, quote=True)


def _crop_bbox(
    samples: bytes, width: int, height: int, n: int, stride: int
) -> tuple[int, int, int, int] | None:
    """Bounding box of non-transparent pixels, or ``None`` for a blank image."""
    min_x, min_y, max_x, max_y = width, height, -1, -1
    for y in range(height):
        row = samples[y * stride : (y + 1) * stride]
        for x in range(width):
            if row[x * n + 3]:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
    if max_x < 0:
        return None
    return min_x, min_y, max_x + 1, max_y + 1


def _rasterize(pdf: bytes) -> bytes | None:
    """PDF bytes to a cropped transparent PNG (``None`` when blank/failed).

    The first pass locates the content's bounding box at 1:1 scale (the page
    is sized in CSS px, which PyMuPDF maps to points at 72dpi); a second pass
    with ``clip=`` renders just that region - the module version's Pixmap
    copy-constructor cannot crop an alpha pixmap in place."""
    try:
        import pymupdf

        doc = pymupdf.open(stream=pdf, filetype="pdf")
        page = doc[0]
        probe = page.get_pixmap(alpha=True)
        bbox = _crop_bbox(probe.samples, probe.width, probe.height, probe.n, probe.stride)
        if bbox is None:
            return None
        x0, y0, x1, y1 = bbox
        scale = 1.0
        # Downscale oversized results (template authors control size, not us).
        if x1 - x0 > _MAX_SIDE or y1 - y0 > _MAX_SIDE:
            scale = min(_MAX_SIDE / (x1 - x0), _MAX_SIDE / (y1 - y0))
        rect = pymupdf.Rect(x0 / scale, y0 / scale, x1 / scale, y1 / scale)
        pix = page.get_pixmap(alpha=True, clip=rect, matrix=pymupdf.Matrix(scale, scale))
        return pix.tobytes("png")
    except Exception:
        log.warning("color panel rasterization failed", exc_info=True)
        return None


def render_panel(
    colors: list[PanelColor],
    template: str | None = None,
    item: str | None = None,
) -> bytes | None:
    """Render the panel image for ``colors`` (sorted row list).

    Returns transparent PNG bytes, or ``None`` when nothing is drawable
    (no colors), the renderer is unavailable, or the template failed.
    Callers must degrade gracefully - the panel posts without its image.
    """
    if not colors:
        return None
    base_template, base_item = default_templates()
    base_template = template if template else base_template
    base_item = item if item else base_item

    items = []
    for entry in colors:
        items.append(
            base_item.format(
                number=entry.number,
                name=_escape(entry.name),
                color=_css_color(entry.color),
                hex=_css_color(entry.color),
            )
        )
    # Page size only anchors the PDF page; the PNG is cropped to content.
    width = 560
    height = max(120, 56 * len(colors) + 32)
    page = base_template.replace("__ITEMS__", "".join(items)).replace(
        "__PANEL_W__", str(width)
    ).replace("__PANEL_H__", str(height))
    try:
        from weasyprint import HTML

        pdf = HTML(string=page).write_pdf()
    except Exception:
        log.warning("color panel HTML render failed", exc_info=True)
        return None
    return _rasterize(pdf)
