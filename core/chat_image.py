"""Chat snapshot: a deleted/edited message rendered as a Discord-style image.

The log card's look lives in a theme's ``snapshot`` section as two HTML
templates: ``page`` (the frame) and ``bubble`` (one message bubble). The bot
fills the placeholders, rasterizes the result through the same pipeline as
the color panel (WeasyPrint to PDF, PyMuPDF 4x raster, cropped to content,
transparent background) and attaches the PNG to the log card via
``attachment://``; the card's gallery references it through the
``{chat_image}`` variable.

Placeholders usable in the templates (besides theme emojis, resolved by the
caller before this module sees the template):

* page: ``{items}`` (the concatenated bubbles) and ``{guild_name}``;
* bubble: ``{author_name}``, ``{author_color}`` (``#rrggbb``, the author's
  top role color; available to theme templates, the default bubble renders
  the name white like Discord does), ``{author_avatar}`` (data URI or an
  SVG fallback with the author's initial), ``{bot}`` (``APP`` badge markup
  or ``""``), ``{time}`` (``dd/mm/yyyy hh:mm:ss``, the bot host's local
  time), ``{content}`` (HTML-escaped, newlines as ``<br>``, capped at
  :data:`_MAX_CONTENT` chars) and ``{image}`` (data URI of the first image
  attachment or non-lottie sticker, or ``""``).

Everything degrades gracefully: a missing WeasyPrint/PyMuPDF, a failing
avatar download or a broken template returns ``None`` and the log card just
keeps its text-only layout. Like the color panel, this is never allowed to
break a send.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import time
from dataclasses import dataclass

from rosemary.core.color_image import rasterize_pdf

log = logging.getLogger(__name__)

#: Hard content cap per bubble: Discord messages can be 4000 chars and a
#: single huge PNG is useless; the full text rides the card's fenced block.
_MAX_CONTENT = 600

#: Per-image byte cap for embedded attachments (Discord serves stickers up
#: to a few MB; embedding everything would blow the rasterizer's memory).
_MAX_IMAGE_BYTES = 4_000_000

#: Avatar download timeout (seconds) and LRU cache size (data URIs).
_AVATAR_TIMEOUT = 8.0
_AVATAR_CACHE_SIZE = 128

#: PNG width cap before downscaling (Discord scales wide images down in
#: cards; 960 = the fixed 480px page at the 4x raster zoom).
_MAX_PNG_SIDE = 960

#: Fallback templates (the theme file ships the same content). WeasyPrint's
#: CSS subset: no flexbox/grid; tables and inline-block work. No ``overflow``
#: rule: WeasyPrint truncates overflow:hidden text silently.
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
    '<td class="body" style="padding: 10px 14px 12px 12px;">\n'
    '  <div class="header" style="font-family: \'Segoe UI\', \'Ubuntu\', '
    '\'Noto Sans\', sans-serif; font-size: 15px; font-weight: 600; '
    'color: #ffffff;">{author_name}{bot}'
    '<span class="time" style="font-size: 12px; font-weight: 400; '
    'color: #949ba4; margin-left: 8px;">{time}</span></div>\n'
    '  <div class="content" style="font-family: \'Segoe UI\', \'Ubuntu\', '
    '\'Noto Sans\', sans-serif; font-size: 15px; line-height: 1.3; '
    'color: #dcddde; white-space: pre-wrap; overflow-wrap: break-word;">'
    '{content}</div>\n'
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

_SVG_AVATAR = (
    "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' "
    "width='48' height='48'><rect width='48' height='48' rx='24' "
    "fill='%235865F2'/><text x='24' y='32' font-family='sans-serif' "
    "font-size='21' fill='white' text-anchor='middle'>{initial}</text></svg>"
)

_BOT_BADGE = (
    '<span style="background: #5865F2; color: white; font-family: sans-serif; '
    'font-size: 10px; padding: 1px 4px; border-radius: 3px; margin-left: 4px; '
    'vertical-align: 2px;">APP</span>'
)


@dataclass(frozen=True)
class SnapshotMessage:
    """Everything the templates need about one message (resolved values)."""

    author_name: str
    author_color: str  # "#rrggbb", neutral gray when unresolvable
    author_avatar: str  # data URI (avatar or SVG fallback)
    is_bot: bool
    timestamp: float
    content: str  # already HTML-escaped with <br>, capped
    image_data_uri: str  # "" when there is nothing visual to embed


_avatar_cache: dict[str, str] = {}


def default_templates() -> tuple[str, str]:
    """The built-in ``(page, bubble)`` pair (theme fallback)."""
    return DEFAULT_PAGE, DEFAULT_BUBBLE


def _escape(text: str) -> str:
    """HTML-escape message content, newlines become ``<br>``."""
    return _html.escape(text or "", quote=False).replace("\n", "<br>")


def _capped(text: str) -> str:
    """Content capped at :data:`_MAX_CONTENT` chars with an ellipsis."""
    if len(text) <= _MAX_CONTENT:
        return text
    return text[:_MAX_CONTENT] + "…"


def _css_color(value: str) -> str:
    """Normalize a role color to ``#rrggbb`` (neutral gray when invalid)."""
    token = (value or "").strip().lstrip("#")
    if len(token) == 6 and all(c in "0123456789abcdefABCDEF" for c in token):
        return f"#{token.lower()}"
    return "#99aab5"


def _avatar_fallback(name: str) -> str:
    """SVG data URI with the author's initial (no download, no deps)."""
    initial = _html.escape((name or "?")[:1].upper(), quote=True) or "?"
    return _SVG_AVATAR.format(initial=initial)


def _download_bytes(url: str, *, timeout: float, max_bytes: int) -> bytes | None:
    """Fetch ``url`` synchronously (best-effort, ``None`` on any failure)."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Rosemary/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
        data = response.read(max_bytes + 1)
    return data if len(data) <= max_bytes else None


def _extension_to_mime(url: str) -> str:
    ext = url.split("?")[0].lower().rsplit(".", 1)
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(ext[1] if len(ext) == 2 else "", "image/png")


def _looks_like_image(url: str) -> bool:
    """Whether the URL's path extension is a raster image Discord serves."""
    ext = url.split("?")[0].lower().rsplit(".", 1)
    return len(ext) == 2 and ext[1] in {"png", "jpg", "jpeg", "gif", "webp"}


def _normalize_image(data: bytes, mime: str, label: str) -> str:
    """Bytes to a PNG data URI WeasyPrint can decode, ``""`` when it cannot.

    Discord CDN images are webp/png/jpg/gif; WeasyPrint's webp support
    depends on the local Pillow build, and a broken file would silently
    render as an empty reserved box (the live bug). Re-encoding through
    Pillow to PNG guarantees a decodable data URI: a failed decode returns
    ``""`` so the bubble drops the image entirely instead of reserving a
    blank rectangle. The first frame of an animated gif is fine for a
    screenshot-style log.
    """
    import base64

    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img.seek(0)
            scratch = io.BytesIO()
            img.convert("RGBA").save(scratch, format="PNG")
            png_bytes = scratch.getvalue()
    except Exception as exc:
        log.info("snapshot image decode failed for %s: %s", label, exc)
        return ""
    return f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"


async def _avatar_data_uri(bot, author) -> str:
    """The author's avatar as a data URI, SVG fallback when unavailable."""
    url = getattr(getattr(author, "display_avatar", None), "url", "") or ""
    if not url:
        return _avatar_fallback(getattr(author, "display_name", None) or "?")
    cached = _avatar_cache.get(url)
    if cached is not None:
        return cached
    loop = asyncio.get_running_loop()

    def _fetch() -> bytes | None:
        return _download_bytes(url, timeout=_AVATAR_TIMEOUT, max_bytes=1_000_000)

    try:
        data = await asyncio.wait_for(loop.run_in_executor(None, _fetch), _AVATAR_TIMEOUT + 2)
    except Exception as exc:
        log.info("snapshot avatar fetch failed for %s: %s", url, exc)
        return _avatar_fallback(getattr(author, "display_name", None) or "?")
    if not data:
        return _avatar_fallback(getattr(author, "display_name", None) or "?")
    data_uri = _normalize_image(data, _extension_to_mime(url), f"avatar {url}")
    if not data_uri:
        return _avatar_fallback(getattr(author, "display_name", None) or "?")
    if len(_avatar_cache) >= _AVATAR_CACHE_SIZE:
        _avatar_cache.clear()  # crude LRU: one flush, cheap and bounded
    _avatar_cache[url] = data_uri
    return data_uri


async def _image_data_uri(message) -> str:
    """First embeddable image (image attachment / non-lottie sticker), or ``""``.

    The extension decides (Discord often serves ``content_type: ""`` on
    re-uploads): a webp/jpg/png/gif attachment is embedded, everything else
    (video, audio, archives) stays as a card link only.
    """
    for att in getattr(message, "attachments", None) or []:
        url = getattr(att, "url", "") or ""
        if not url or not _looks_like_image(url):
            continue
        loop = asyncio.get_running_loop()

        def _fetch(u=url):
            return _download_bytes(u, timeout=_AVATAR_TIMEOUT, max_bytes=_MAX_IMAGE_BYTES)

        try:
            data = await asyncio.wait_for(loop.run_in_executor(None, _fetch), _AVATAR_TIMEOUT + 2)
        except Exception as exc:
            log.info("snapshot attachment fetch failed for %s: %s", url, exc)
            continue
        if not data:
            continue
        uri = _normalize_image(data, _extension_to_mime(url), f"attachment {url}")
        if uri:
            return uri
    for item in getattr(message, "stickers", None) or []:
        if getattr(item.format, "name", "") == "lottie":
            continue
        url = getattr(item, "url", "") or ""
        if not url:
            continue
        loop = asyncio.get_running_loop()

        def _fetch(u=url):
            return _download_bytes(u, timeout=_AVATAR_TIMEOUT, max_bytes=_MAX_IMAGE_BYTES)

        try:
            data = await asyncio.wait_for(loop.run_in_executor(None, _fetch), _AVATAR_TIMEOUT + 2)
        except Exception as exc:
            log.info("snapshot sticker fetch failed for %s: %s", url, exc)
            continue
        if not data:
            continue
        uri = _normalize_image(data, _extension_to_mime(url), f"sticker {url}")
        if uri:
            return uri
    return ""


async def build_snapshot_message(
    bot, message, *, before=None
) -> SnapshotMessage:
    """Resolve one message (or a before/after pair) into template values.

    With ``before`` (message_edit) the bubble shows the OLD content with the
    author identity from ``before``: that is the state the log documents.
    """
    source = before if before is not None else message
    author = source.author
    guild = getattr(source, "guild", None)
    color_value = ""
    member = guild.get_member(author.id) if guild is not None else None
    if member is not None:
        top = member.top_role
        if top is not None and getattr(top, "colour", None) is not None:
            value = getattr(top.colour, "value", 0)
            if isinstance(value, int) and value:  # live Colour.value; degrade otherwise
                color_value = f"{value:06x}"
    moment = getattr(source, "created_at", None)
    stamp = moment.timestamp() if moment is not None else time.time()
    # True epoch: the bubble renders it in the bot host's local wall clock
    # (a chat screenshot shows local time, like Discord's own tooltips).
    content = _capped(_escape(source.content or ""))
    image_uri = await _image_data_uri(message)
    avatar_uri = await _avatar_data_uri(bot, author)
    return SnapshotMessage(
        author_name=getattr(author, "display_name", None) or author.name,
        author_color=_css_color(color_value),
        author_avatar=avatar_uri,
        is_bot=bool(getattr(author, "bot", False)),
        timestamp=stamp,
        content=content,
        image_data_uri=image_uri,
    )


def _bubble_markup(
    info: SnapshotMessage,
    bubble_template: str,
    image_template: str,
    emojis: dict[str, str],
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
            image_template.format(image=info.image_data_uri)
            if info.image_data_uri
            else ""
        ),
        **emojis,
    }
    return safe_format(bubble_template, mapping)


def render_snapshot(
    infos: list[SnapshotMessage],
    template: str | None = None,
    bubble: str | None = None,
    image: str | None = None,
    emojis: dict[str, str] | None = None,
) -> bytes | None:
    """Rasterize the chat snapshot for the resolved bubbles (or ``None``).

    Same contract as :func:`rosemary.core.color_image.render_panel`: PNG
    bytes or ``None`` (no content, missing renderer, broken template) and
    callers degrade to a text-only card. Content-less bubbles render an
    em-space so the bubble frame still shows (a message can be image-only).
    """
    if not infos:
        return None
    base_template, base_bubble = default_templates()
    base_template = template if template else base_template
    base_bubble = bubble if bubble else base_bubble
    image_template = image if image else DEFAULT_BUBBLE_IMAGE
    emojis = emojis or {}

    def render_one(info: SnapshotMessage) -> str:
        filled = info
        if not info.content.strip():
            filled = SnapshotMessage(
                author_name=info.author_name,
                author_color=info.author_color,
                author_avatar=info.author_avatar,
                is_bot=info.is_bot,
                timestamp=info.timestamp,
                content="&#8203;",  # zero-width space keeps the frame visible
                image_data_uri=info.image_data_uri,
            )
        return _bubble_markup(filled, base_bubble, image_template, emojis)

    items = "".join(render_one(info) for info in infos)
    # Fixed-width page (480 CSS px): the PNG keeps every snapshot at the
    # same scale, so Discord renders them all at the same visual size no
    # matter the message (the variable-width crop made each card a
    # different zoom). Height is a BUDGET, not a target: it must hold the
    # worst bubble (capped content plus the 240px image preview) or
    # WeasyPrint paginates the overflow onto a second PDF page, which the
    # rasterizer drops (the live empty-bubble bug); the crop trims whatever
    # the actual content leaves unused.
    width_px = 480
    height_px = 560 * len(infos) + 80
    from weasyprint import HTML

    try:
        pdf = (
            HTML(
                string=base_template.replace("__ITEMS__", items)
                .replace("__PAGE_W__", str(width_px))
                .replace("__PAGE_H__", str(height_px))
            )
            .write_pdf()
        )
    except Exception:
        log.warning("chat snapshot HTML render failed", exc_info=True)
        return None
    return rasterize_pdf(pdf, full_width=True, max_side=_MAX_PNG_SIDE)


async def render_message_snapshot(bot, message, *, before=None) -> bytes | None:
    """Full async pipeline for one message (or edit pair): PNG or ``None``.

    Wraps :func:`build_snapshot_message` + :func:`render_snapshot`; any
    unexpected error logs at warning and returns ``None`` (text-only card).
    """
    try:
        theme = bot.theme
        templates = {}
        from rosemary.core.themes import theme_for

        theme = theme_for(bot, getattr(message, "guild_id", None))
        templates = getattr(theme, "snapshot", None) or {}
        info = await build_snapshot_message(bot, message, before=before)
        infos = [info]
        if before is not None and (before.content or "") != (message.content or ""):
            after = await build_snapshot_message(bot, message)
            infos = [info, after]
        return render_snapshot(
            infos,
            templates.get("page"),
            templates.get("bubble"),
            templates.get("image"),
            getattr(theme, "emojis", {}) or {},
        )
    except Exception:
        log.warning("chat snapshot render failed", exc_info=True)
        return None
