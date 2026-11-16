"""Chat snapshot: a deleted/edited message rendered as a Discord-style image.

The renderer itself lives in :mod:`rosemary.core.chat_bubble` (the bubble
model, the theme templates and the rasterizer). This module keeps the
Discord half: it resolves a real message (its author, its avatar, its
attachments) into a :class:`Bubble` and drives the shared pipeline.

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
import base64
import io
import logging
import time
from dataclasses import replace

from rosemary.core.chat_bubble import (
    DEFAULT_BUBBLE,
    DEFAULT_BUBBLE_IMAGE,
    DEFAULT_PAGE,
    Bubble,
    avatar_fallback,
    bubble_markup,
    cap_content,
    css_color,
    default_templates,
    escape_content,
    render_bubbles,
)

log = logging.getLogger(__name__)

#: Avatar download timeout (seconds) and LRU cache size (data URIs).
_AVATAR_TIMEOUT = 8.0
_AVATAR_CACHE_SIZE = 128

#: Per-image byte cap for embedded attachments (Discord serves stickers up
#: to a few MB; embedding everything would blow the rasterizer's memory).
_MAX_IMAGE_BYTES = 4_000_000

#: The historical name of the bubble model (kept: the audit tests and any
#: plugin import it).
SnapshotMessage = Bubble

#: Backwards-compatible aliases of the helpers that moved to the shared
#: bubble module.
_avatar_fallback = avatar_fallback
_bubble_markup = bubble_markup
_capped = cap_content
_css_color = css_color
_escape = escape_content

_avatar_cache: dict[str, str] = {}


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
    try:
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
        return avatar_fallback(getattr(author, "display_name", None) or "?")
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
        return avatar_fallback(getattr(author, "display_name", None) or "?")
    if not data:
        return avatar_fallback(getattr(author, "display_name", None) or "?")
    data_uri = _normalize_image(data, _extension_to_mime(url), f"avatar {url}")
    if not data_uri:
        return avatar_fallback(getattr(author, "display_name", None) or "?")
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
        uri = _normalize_image(data, _extension_to_mime(url), f"sticker {url}")
        if uri:
            return uri
    return ""


async def build_snapshot_message(
    bot, message, *, before=None
) -> Bubble:
    """Resolve one message (or a before/after pair) into bubble values.

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
    content = cap_content(escape_content(source.content or ""))
    image_uri = await _image_data_uri(message)
    avatar_uri = await _avatar_data_uri(bot, author)
    return Bubble(
        author_name=getattr(author, "display_name", None) or author.name,
        author_color=css_color(color_value),
        author_avatar=avatar_uri,
        is_bot=bool(getattr(author, "bot", False)),
        timestamp=stamp,
        content=content,
        image_data_uri=image_uri,
    )


def render_snapshot(
    infos: list[Bubble],
    template: str | None = None,
    bubble: str | None = None,
    image: str | None = None,
    emojis: dict[str, str] | None = None,
    edited_marker: str = "",
    media: dict[str, str] | None = None,
) -> bytes | None:
    """Rasterize the chat snapshot for the resolved bubbles (or ``None``).

    Thin wrapper over :func:`rosemary.core.chat_bubble.render_bubbles`: the
    audit logs never quote a parent, but they share the whole pipeline.
    """
    return render_bubbles(
        infos,
        template=template,
        bubble=bubble,
        image=image,
        emojis=emojis,
        edited_marker=edited_marker,
        media=media,
    )


async def render_message_snapshot(
    bot, message, *, before=None, edited_marker: str = ""
) -> bytes | None:
    """Full async pipeline for one message (or edit pair): PNG or ``None``.

    Wraps :func:`build_snapshot_message` + :func:`render_snapshot`; any
    unexpected error logs at warning and returns ``None`` (text-only card).
    ``edited_marker`` is caller-provided user-facing text (the cog resolves
    it from the catalogs; this module stays i18n-free like color_image).
    """
    try:
        from rosemary.core.themes import theme_for

        theme = theme_for(bot, getattr(message, "guild_id", None))
        templates = getattr(theme, "snapshot", None) or {}
        from rosemary.core.theme_assets import ThemeAssets, media_urls

        media = media_urls(getattr(theme, "assets", None) or ThemeAssets())
        info = await build_snapshot_message(bot, message, before=before)
        infos = [info]
        if before is not None and (before.content or "") != (message.content or ""):
            after = await build_snapshot_message(bot, message)
            infos = [info, after]
        # Discord marks the CURRENT message state: the bottom (after) bubble
        # is the content that now carries the (editado) marker; the top one
        # is the untouched previous text.
        if before is not None and edited_marker:
            infos[-1] = replace(infos[-1], edited=True)
        return render_snapshot(
            infos,
            templates.get("page"),
            templates.get("bubble"),
            templates.get("image"),
            getattr(theme, "emojis", {}) or {},
            edited_marker=edited_marker,
            media=media,
        )
    except Exception:
        log.warning("chat snapshot render failed", exc_info=True)
        return None


__all__ = [
    "DEFAULT_BUBBLE",
    "DEFAULT_BUBBLE_IMAGE",
    "DEFAULT_PAGE",
    "SnapshotMessage",
    "build_snapshot_message",
    "default_templates",
    "render_message_snapshot",
    "render_snapshot",
]
