"""Chat bubbles: the Discord-style message image of the audit snapshots.

One renderer serves the "message rendered as an image" feature: the audit
log snapshots (a deleted or edited message). The layers are:

* the model (:class:`Bubble`): everything one bubble needs (display name,
  role color, avatar data URI, content, embedded image, timestamp);
* the markup (:func:`bubble_markup`): the theme's HTML template filled
  with the resolved values;
* the rasterizer (:func:`render_bubbles`): HTML to PDF (WeasyPrint) to PNG
  (PyMuPDF), cropped to the content.

The theme owns the look: a theme's ``snapshot`` section carries the ``page``
(frame) and ``bubble`` (one message) templates. A missing template falls
back to the built-in one in this module.

Everything degrades to ``None``: a missing renderer, a broken template or an
empty bubble list returns ``None`` and the caller falls back to plain text.
"""

from __future__ import annotations

import base64
import html as _html
import logging
import time
from dataclasses import dataclass

from rosemary.core.color_image import rasterize_pdf

log = logging.getLogger(__name__)

#: Hard content cap per bubble: Discord messages can be 4000 chars and a
#: single huge PNG is useless; the full text rides the card's fenced block.
_MAX_CONTENT = 600

#: PNG width cap before downscaling (Discord scales wide images down in
#: cards; 960 = the fixed 480px page at the 4x raster zoom).
_MAX_PNG_WIDTH = 960

#: PNG height cap: a long bubble keeps its full width and grows down
#: instead of being shrunk into an unreadable thumbnail (the old single
#: 960px cap turned a long message into a 512px wide image).
_MAX_PNG_HEIGHT = 4096

#: Fixed page width (CSS px): every PNG ships at the same scale, so Discord
#: renders them all at the same visual size no matter the message.
_WIDTH_PX = 480

#: Rough text metrics, used only to size the PDF page (the page is a BUDGET,
#: the rasterizer crops the unused space). The content column is ~365px wide
#: at 15px, so ~30 characters fit on a line (fewer for wide glyphs: a wall
#: of capitals packs less). The estimate is deliberately conservative: a
#: page too short makes WeasyPrint paginate the bubble and the rasterizer
#: drops the second page (the text was cut in half).
_CHARS_PER_LINE = 30
_LINE_HEIGHT = 20
_BUBBLE_CHROME = 84  # avatar row, header line and the paddings
_IMAGE_HEIGHT = 256  # the embedded preview (240 + its margin)

#: Fallback templates (the theme file ships the same content). WeasyPrint's
#: CSS subset: no flexbox/grid; tables and inline-block work. No ``overflow``
#: rule: WeasyPrint truncates overflow:hidden text silently.
#:
#: The body cell carries ``max-width``: an auto-width table sizes itself to
#: its content's MINIMUM width, so one long word (a URL, a "AAAAAAAA" wall)
#: pushes the table past the page and the rasterizer crops the overflow
#: (the text was cut in half). The cap lets ``overflow-wrap: break-word``
#: break the word inside the bubble instead, while a short message still
#: hugs its content (the table shrinks below the cap).
DEFAULT_PAGE = """<style>
@page { size: __PAGE_W__px __PAGE_H__px; margin: 0; background: transparent; }
html, body { margin: 0; padding: 0; background: transparent; }
</style>
<div class="page">__ITEMS__</div>"""

DEFAULT_BUBBLE = (
    '<table class="bubble" style="background: #32353b; border-radius: 8px; '
    'margin: 0 0 12px 0; padding: 0; border-collapse: collapse; '
    'border-spacing: 0;"><tr>\n'
    '<td class="avatar" style="vertical-align: top; padding: 12px 0 12px 14px;">'
    '<img src="{author_avatar}" '
    'style="width: 40px; height: 40px; border-radius: 50%; display: block;" />'
    '</td>\n'
    '<td class="body" style="padding: 10px 14px 12px 12px; max-width: 390px;">\n'
    '  <div class="header" style="font-family: \'Segoe UI\', \'Ubuntu\', '
    '\'Noto Sans\', sans-serif; font-size: 15px; font-weight: 600; '
    'color: #ffffff;">{author_name}{bot}'
    '<span class="time" style="font-size: 12px; font-weight: 400; '
    'color: #949ba4; margin-left: 8px;">{time}</span></div>\n'
    '  <div class="content" style="font-family: \'Segoe UI\', \'Ubuntu\', '
    '\'Noto Sans\', sans-serif; font-size: 15px; line-height: 1.3; '
    'color: #dcddde; white-space: pre-wrap; overflow-wrap: break-word;">'
    '{content}{edited}</div>\n'
    '  {image}\n</td>\n</tr></table>'
)

# Attachment preview: max-width AND max-height (ratio preserved, like
# Discord's own chat previews). A tall manga scan at a bare width would
# need ~500px of bubble: past the page budget WeasyPrint splits the table
# across PDF pages and the atomic image jumps to page 2, which the
# rasterizer drops (live: a tall EMPTY bubble in the card).
DEFAULT_BUBBLE_IMAGE = (
    '<img class="attachment" src="{image}" style="border-radius: 4px; '
    'margin-top: 8px; max-width: 320px; max-height: 240px;" />'
)

_AVATAR_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' "
    "width='48' height='48'><rect width='48' height='48' rx='24' "
    "fill='#5865F2'/><text x='24' y='32' font-family='sans-serif' "
    "font-size='21' fill='white' text-anchor='middle'>{initial}</text></svg>"
)

#: The ``APP`` badge Discord draws next to a bot's name.
_BOT_BADGE = (
    '<span style="background: #5865F2; color: white; font-family: sans-serif; '
    'font-size: 10px; padding: 1px 4px; border-radius: 3px; margin-left: 4px; '
    'vertical-align: 2px;">APP</span>'
)

#: The Discord-style ``(editado)`` suffix after the content (one meaning, one
#: emoji rule does not apply: this is a marker, not a labeled field).
_EDITED_BADGE = (
    '<span style="font-family: \'Segoe UI\', \'Ubuntu\', \'Noto Sans\', sans-serif; '
    'font-size: 10px; color: #949ba4;"> {marker}</span>'
)


@dataclass(frozen=True)
class Bubble:
    """Everything the templates need about one message (resolved values).

    ``content`` is already HTML-escaped (newlines as ``<br>``) and capped.
    """

    author_name: str
    author_color: str  # "#rrggbb", neutral gray when unresolvable
    author_avatar: str  # data URI
    is_bot: bool
    timestamp: float
    content: str  # already HTML-escaped with <br>, capped
    image_data_uri: str = ""  # "" when there is nothing visual to embed
    edited: bool = False  # renders the localized ``(editado)`` marker


def default_templates() -> tuple[str, str]:
    """The built-in ``(page, bubble)`` pair (theme fallback)."""
    return DEFAULT_PAGE, DEFAULT_BUBBLE


def escape_content(text: str) -> str:
    """HTML-escape message content, newlines become ``<br>``."""
    return _html.escape(text or "", quote=False).replace("\n", "<br>")


def cap_content(text: str) -> str:
    """Content capped at :data:`_MAX_CONTENT` chars with an ellipsis."""
    if len(text) <= _MAX_CONTENT:
        return text
    return text[:_MAX_CONTENT] + "…"


def css_color(value: str) -> str:
    """Normalize a role color to ``#rrggbb`` (neutral gray when invalid)."""
    token = (value or "").strip().lstrip("#")
    if len(token) == 6 and all(c in "0123456789abcdefABCDEF" for c in token):
        return f"#{token.lower()}"
    return "#99aab5"


def avatar_fallback(name: str) -> str:
    """SVG data URI with the author's initial (no download, no deps)."""
    initial = _html.escape((name or "?")[:1].upper(), quote=True) or "?"
    return _svg_data_uri(_AVATAR_SVG.format(initial=initial))


def _svg_data_uri(svg: str) -> str:
    """SVG markup as a base64 data URI.

    Base64 (not the raw ``;utf8,`` form) because the SVG markup carries
    quotes: a raw SVG inside an HTML ``src="..."`` attribute closes it at
    the first double quote and WeasyPrint drops the image. Base64 has no
    quoting hazard at all.
    """
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def bubble_markup(
    info: Bubble,
    bubble_template: str,
    image_template: str,
    emojis: dict[str, str],
    edited_marker: str = "",
) -> str:
    """One bubble's HTML from the resolved values (``{name}`` placeholders)."""
    from rosemary.core.cards import safe_format

    mapping = {
        "author_name": _html.escape(info.author_name, quote=True),
        "author_color": info.author_color,
        "author_avatar": info.author_avatar,
        "bot": _BOT_BADGE if info.is_bot else "",
        "time": time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(info.timestamp)),
        "content": info.content,
        "image": (
            safe_format(image_template, {"image": info.image_data_uri})
            if info.image_data_uri
            else ""
        ),
        "edited": (
            _EDITED_BADGE.format(marker=_html.escape(edited_marker, quote=True))
            if info.edited and edited_marker
            else ""
        ),
        **emojis,
    }
    return safe_format(bubble_template, mapping)


def _text_lines(content: str) -> int:
    """How many lines a content blob needs (a pessimistic estimate)."""
    plain = (content or "").replace("<br>", " ")
    return max(1, -(-len(plain) // _CHARS_PER_LINE))


def _bubble_height(info: Bubble) -> int:
    """The page height one bubble needs (its own budget, no margins)."""
    height = _BUBBLE_CHROME + _text_lines(info.content) * _LINE_HEIGHT
    if info.image_data_uri:
        height += _IMAGE_HEIGHT
    return height


def _page_height(bubbles: list[Bubble]) -> int:
    """The PDF page height that holds every bubble.

    A budget, never a target: the rasterizer crops the unused space. It
    must be generous enough that WeasyPrint keeps everything on ONE page
    (the overflow lands on a second page, which the rasterizer drops and
    the text disappears).
    """
    height = 24  # the page's own margins
    for info in bubbles:
        height += _bubble_height(info) + 12  # the bubble's bottom margin
    return height


def render_bubbles(
    bubbles: list[Bubble],
    template: str | None = None,
    bubble: str | None = None,
    image: str | None = None,
    emojis: dict[str, str] | None = None,
    edited_marker: str = "",
) -> bytes | None:
    """Rasterize the chat bubbles for the resolved values (or ``None``).

    Same contract as :func:`rosemary.core.color_image.render_panel`: PNG
    bytes or ``None`` (no content, missing renderer, broken template) and
    callers degrade to a text-only card. Content-less bubbles render an
    em-space so the bubble frame still shows (a message can be image-only).
    ``edited_marker`` is the localized ``(editado)`` text for bubbles flagged
    ``edited`` (empty disables the marker entirely).
    """
    if not bubbles:
        return None
    base_template, base_bubble = default_templates()
    base_template = template if template else base_template
    base_bubble = bubble if bubble else base_bubble
    image_template = image if image else DEFAULT_BUBBLE_IMAGE
    theme_emojis = emojis or {}

    def render_one(info: Bubble) -> str:
        filled = info
        if not info.content.strip():
            filled = Bubble(
                author_name=info.author_name,
                author_color=info.author_color,
                author_avatar=info.author_avatar,
                is_bot=info.is_bot,
                timestamp=info.timestamp,
                content="&#8203;",  # zero-width space keeps the frame visible
                image_data_uri=info.image_data_uri,
                edited=info.edited,
            )
        return bubble_markup(
            filled, base_bubble, image_template, theme_emojis, edited_marker
        )

    items = "".join(render_one(info) for info in bubbles)
    # Fixed-width page (480 CSS px): the PNG keeps every snapshot at the
    # same scale, so Discord renders them all at the same visual size no
    # matter the message (the variable-width crop made each card a
    # different zoom). Height is a BUDGET, not a target: it must hold the
    # whole content or WeasyPrint paginates the overflow onto a second PDF
    # page, which the rasterizer drops (the text was cut in half); the crop
    # trims whatever the actual content leaves unused. The budget follows
    # the content (a three-line message does not reserve a page).
    height_px = _page_height(bubbles)

    try:
        from weasyprint import HTML

        pdf = (
            HTML(
                string=base_template.replace("__ITEMS__", items)
                .replace("__PAGE_W__", str(_WIDTH_PX))
                .replace("__PAGE_H__", str(height_px))
            )
            .write_pdf()
        )
    except Exception:
        log.warning("chat bubble HTML render failed", exc_info=True)
        return None
    return rasterize_pdf(
        pdf,
        full_width=True,
        max_width=_MAX_PNG_WIDTH,
        max_height=_MAX_PNG_HEIGHT,
    )


__all__ = [
    "DEFAULT_BUBBLE",
    "DEFAULT_BUBBLE_IMAGE",
    "DEFAULT_PAGE",
    "Bubble",
    "avatar_fallback",
    "bubble_markup",
    "cap_content",
    "css_color",
    "default_templates",
    "escape_content",
    "render_bubbles",
]
