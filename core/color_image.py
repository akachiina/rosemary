"""Color panel image: HTML template to transparent PNG.

The panel's look lives in a theme's ``color_panel`` section as two HTML
templates (:data:`DEFAULT_TEMPLATE` / :data:`DEFAULT_ITEM`): the bot injects
one ``item`` per color into the ``{items}`` slot and rasterizes the result.

Pipeline: WeasyPrint renders the HTML to a single-page PDF (its native
output), the page width is *measured* against the rendered PDF's own word
boxes until every column's text fits AND no label crosses the page's right
edge (font-metric guesses clip long labels like "Algodão-Doce Escurto"),
PyMuPDF rasterizes the content region at 4x with an alpha channel, and the
bitmap is cropped to the content's bounding box so the transparent margin
disappears. The result is a sharp PNG with a
truly transparent background - Discord composites it over the chat surface,
matching a card written for a dark or light theme alike.

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

#: Raster zoom: 1:1 maps 1pt to 1px, so 26px text rasters at ~19px and looks
#: fuzzy on Discord. 4x keeps it crisp; the bitmap is cropped to content.
_ZOOM = 4

#: Minimum breathing room (pt) between a column's text and the next column.
_COLUMN_GAP_PT = 6.0

#: Minimum breathing room (pt) between the rightmost label and the page
#: edge. The column-gap check only sees overflow INTO the next column; a
#: long label in the LAST column overflows the page itself (WeasyPrint
#: clips at the page boundary) and needs this second check to trigger
#: the width growth.
_PAGE_EDGE_PAD_PT = 8.0

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

#: Fallback templates (the theme file ships the same content). Placeholders
#: usable inside ``item``: ``{number}``, ``{name}``, ``{color}``, ``{hex}``.
#: ``@page`` sizing keeps WeasyPrint from emitting a default A4 page. Layout
#: uses multi-column + inline-block - the CSS subsets WeasyPrint renders
#: faithfully (its flexbox support is too weak for icon+label rows). No
#: ``overflow`` rule anywhere: the renderer grows the page until every label
#: fits, so clipping text silently would be a bug.
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


class _ImageMeasurer:
    """PyMuPDF word boxes: real text extents of one rendered page."""

    def __init__(self, pdf: bytes) -> None:
        import pymupdf

        doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.page_width: float = doc[0].rect.width
        self.words: list[tuple[float, float]] = [
            (word[0], word[2]) for word in doc[0].get_text("words")
        ]

    @property
    def right_pt(self) -> float:
        """Rightmost word edge (0.0 when the page has no words)."""
        return max((x1 for _x0, x1 in self.words), default=0.0)

    def columns_fit(self, column_count: int) -> bool:
        """Whether every column's text clears the next column's start.

        With ``column-count: N`` WeasyPrint shares the content width equally;
        a too-long label ends against (or crosses) the next column's first
        word. Word x-starts cluster per column, so the N-1 largest gaps
        between distinct starts split the regions; each region's max right
        edge must leave :data:`_COLUMN_GAP_PT` before the next region's min
        left edge.
        """
        if column_count < 2 or not self.words:
            return True
        starts = sorted({round(x0, 0) for x0, _x1 in self.words})
        if len(starts) < column_count:
            return True
        gaps = [
            (b - a, i)
            for i, (a, b) in enumerate(zip(starts, starts[1:], strict=False))
        ]
        gaps.sort(reverse=True)
        chosen = sorted(starts[i + 1] for _gap, i in gaps[: column_count - 1])
        bounds = [float("-inf"), *chosen, float("inf")]
        for k in range(1, len(bounds) - 1):
            region = [
                (x0, x1) for x0, x1 in self.words if bounds[k - 1] < x0 < bounds[k]
            ]
            nxt = [(x0, x1) for x0, x1 in self.words if bounds[k] < x0 < bounds[k + 1]]
            if not region or not nxt:
                continue
            gap = min(x0 for x0, _x1 in nxt) - max(x1 for _x0, x1 in region)
            if gap < _COLUMN_GAP_PT:
                return False
        return True


def _measured_pdf(
    items: list[str],
    base_template: str,
    height_px: int,
    column_count: int,
) -> bytes | None:
    """Render a page wide enough for the widest label in any column.

    The width starts at the default and grows until the rendered PDF's own
    word boxes show every column fitting (see :meth:`_ImageMeasurer.columns_fit`)
    and the rightmost label clearing the page edge (a label in the last
    column overflows the page itself, which the column check never sees);
    PDF text extraction measures the real glyphs, which font-file guesses
    overestimate and still got clipped. Returns the PDF bytes or ``None``
    when rendering fails.
    """
    from weasyprint import HTML

    joined = "".join(items)

    def build_page(width_pt: float) -> str:
        return (
            base_template.replace("__ITEMS__", joined)
            .replace("__PANEL_W__", str(width_pt))
            .replace("__PANEL_H__", str(height_px * 0.75))
        )

    width = 560.0 * 0.75  # default page, in pt (CSS px -> pt at 96dpi)
    for _attempt in range(5):
        try:
            pdf = HTML(string=build_page(width)).write_pdf()
        except Exception:
            log.warning("color panel HTML render failed", exc_info=True)
            return None
        try:
            measurer = _ImageMeasurer(pdf)
        except Exception:
            log.warning("color panel measurement failed", exc_info=True)
            return pdf
        # The candidate goes into ``size: Npx`` (CSS px) but word boxes are
        # page points: compare against the PDF's own page width, never the
        # candidate (px vs pt mixed once made this check toothless).
        if (
            measurer.columns_fit(column_count)
            and measurer.right_pt
            <= measurer.page_width - _PAGE_EDGE_PAD_PT
        ):
            return pdf
        width *= 1.3
    return pdf


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

    The probe pass renders the whole page at :data:`_ZOOM` and locates the
    content's bounding box **in probe pixels**; converted to page points it
    becomes the clip rect of the final pass (a module-version Pixmap cannot
    crop an alpha pixmap in place). The final zoom stays at :data:`_ZOOM`
    unless the crop would exceed :data:`_MAX_SIDE`, which downscales instead
    of shipping a huge upload.
    """
    try:
        import pymupdf

        doc = pymupdf.open(stream=pdf, filetype="pdf")
        page = doc[0]
        probe = page.get_pixmap(alpha=True, matrix=pymupdf.Matrix(_ZOOM, _ZOOM))
        bbox = _crop_bbox(
            probe.samples, probe.width, probe.height, probe.n, probe.stride
        )
        if bbox is None:
            return None
        x0, y0, x1, y1 = bbox
        # Probe pixels -> page points (the probe rasterized at _ZOOM).
        rect = pymupdf.Rect(
            x0 / _ZOOM, y0 / _ZOOM, x1 / _ZOOM, y1 / _ZOOM
        )
        zoom = float(_ZOOM)
        if (
            rect.width * zoom > _MAX_SIDE
            or rect.height * zoom > _MAX_SIDE
        ):
            zoom = min(
                _MAX_SIDE / max(rect.width, 1e-6),
                _MAX_SIDE / max(rect.height, 1e-6),
                _ZOOM,
            )
        pix = page.get_pixmap(
            alpha=True, clip=rect, matrix=pymupdf.Matrix(zoom, zoom)
        )
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
    # CSS px must reach WeasyPrint as PDF points (0.75pt/px at 96dpi): raw px
    # values shrink the page to 75% and the columns overflow their edge.
    height_px = max(120, 56 * len(colors) + 32)
    column_count = 2  # the templates ship column-count: 2
    pdf = _measured_pdf(items, base_template, height_px, column_count)
    if pdf is None:
        return None
    return _rasterize(pdf)
