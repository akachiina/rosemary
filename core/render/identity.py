"""The identity of a card: who it is about.

A card names a member (the display name and the picture). The drawn
elements consume that identity: the author strip shows the avatar and the
name. The picture is fetched once and kept as a **data URI**, because the
renderer (WeasyPrint) draws a document, it does not browse the web: a
remote URL inside the HTML would leave the picture empty.

The fetch never holds a card: it happens in a **daemon thread** and the
card waits for it at most :data:`AVATAR_TIMEOUT` seconds. A CDN that is
slow (or a host whose DNS never answers) costs the card one second, never
more, and the picture that arrives later is ready for the next card. A
picture that cannot be downloaded degrades to the SVG fallback (the initial
of the name), never to a broken image. A picture the bot generates itself
(an ``attachment://`` reference, the identicon of a confession) cannot be
fetched: it degrades the same way.

The chat snapshot uses the same helpers, so both renderers agree on what a
picture is.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import threading
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: How long a card waits for the picture (the fetch itself keeps running in
#: the background). A card never holds on the network.
AVATAR_TIMEOUT = 1.0
#: The timeout of the download itself (the socket).
DOWNLOAD_TIMEOUT = 8.0
#: The ceiling of the picture cache (data URIs).
AVATAR_CACHE_SIZE = 128
#: The ceiling of the pictures being fetched at the same time.
MAX_PENDING = 32

#: The byte cap of one picture (Discord avatars are small; the cap keeps a
#: hostile URL from filling the memory of the bot).
MAX_IMAGE_BYTES = 1_000_000

#: The fetched pictures (the URL -> the data URI).
_cache: dict[str, str] = {}
#: The pictures being fetched right now (one fetch per URL).
_pending: set[str] = set()
#: The lock of the cache and of the pending set.
_lock = threading.Lock()


@dataclass(frozen=True)
class Identity:
    """Who the card is about: the display name and the picture."""

    name: str
    avatar_url: str = ""

    @property
    def initial(self) -> str:
        """The first letter of the name (the fallback picture's mark)."""
        return (self.name.strip() or "?")[0]


def download_bytes(
    url: str, *, timeout: float = DOWNLOAD_TIMEOUT, max_bytes: int = MAX_IMAGE_BYTES
) -> bytes | None:
    """Fetch ``url`` synchronously (best-effort, ``None`` on any failure)."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Rosemary/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
        data = response.read(max_bytes + 1)
    return data if len(data) <= max_bytes else None


def extension_to_mime(url: str) -> str:
    """The MIME type of a picture URL (PNG when the extension says nothing)."""
    ext = url.split("?")[0].lower().rsplit(".", 1)
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(ext[1] if len(ext) == 2 else "", "image/png")


def normalize_image(data: bytes, mime: str, label: str) -> str:
    """Bytes to a PNG data URI the renderer can decode, ``""`` when it cannot.

    Discord CDN images are webp/png/jpg/gif; WeasyPrint's webp support
    depends on the local Pillow build, and a broken file would silently
    render as an empty reserved box (the live bug). Re-encoding through
    Pillow to PNG guarantees a decodable data URI: a failed decode returns
    ``""`` so the picture drops out entirely instead of reserving a blank
    rectangle. The first frame of an animated gif is fine for a still.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img.seek(0)
            scratch = io.BytesIO()
            img.convert("RGBA").save(scratch, format="PNG")
            png_bytes = scratch.getvalue()
    except Exception as exc:
        log.info("picture decode failed for %s: %s", label, exc)
        return ""
    return f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"


async def avatar_data_uri(url: str, fallback_name: str = "?") -> str:
    """The avatar as a data URI (the SVG fallback when it cannot be fetched).

    The picture is cached by its URL: the same avatar is drawn again and
    again (a busy guild draws one strip per card). The card waits for the
    fetch at most :data:`AVATAR_TIMEOUT` seconds; after that the fallback
    answers and the picture that arrives later waits for the next card.
    """
    from rosemary.core.chat_bubble import avatar_fallback

    if not url:
        return avatar_fallback(fallback_name or "?")
    cached = _cache.get(url)
    if cached is not None:
        return cached
    uri = await _fetch_within(url, AVATAR_TIMEOUT)
    if uri:
        return uri
    return avatar_fallback(fallback_name or "?")


async def _fetch_within(url: str, timeout: float) -> str:
    """The picture of a URL, fetched in the background (a bounded wait).

    The download runs in a daemon thread: a host whose DNS never answers
    leaves the thread hanging, never the bot (a daemon thread does not hold
    the interpreter at shutdown).
    """
    loop = asyncio.get_running_loop()
    done = asyncio.Event()

    def _finish() -> None:
        with contextlib.suppress(RuntimeError):
            # The loop is gone: the picture stays in the cache.
            loop.call_soon_threadsafe(done.set)

    def _fetch() -> None:
        try:
            _fetch_into_cache(url)
        finally:
            _finish()

    if _claim(url):
        threading.Thread(target=_fetch, daemon=True, name="rosemary-avatar").start()
    try:
        await asyncio.wait_for(done.wait(), timeout)
    except TimeoutError:
        return ""
    return _cache.get(url, "")


def _claim(url: str) -> bool:
    """Whether this caller owns the fetch of a URL (one fetch per URL)."""
    with _lock:
        if url in _pending or len(_pending) >= MAX_PENDING:
            return False
        _pending.add(url)
        return True


def _fetch_into_cache(url: str) -> None:
    """Download a picture and keep it (the cache is bounded)."""
    try:
        data = download_bytes(url, timeout=DOWNLOAD_TIMEOUT, max_bytes=MAX_IMAGE_BYTES)
        if not data:
            return
        uri = normalize_image(data, extension_to_mime(url), f"avatar {url}")
        if not uri:
            return
        with _lock:
            if len(_cache) >= AVATAR_CACHE_SIZE:
                _cache.clear()  # crude LRU: one flush, cheap and bounded
            _cache[url] = uri
    except Exception as exc:
        log.info("avatar fetch failed for %s: %s", url, exc)
    finally:
        with _lock:
            _pending.discard(url)


async def identity_values(identity: Identity, accent: str = "") -> dict[str, str]:
    """The template values of one identity (the picture as a data URI)."""
    return {
        "name": _escape(identity.name),
        "initial": _escape(identity.initial),
        "avatar": await avatar_data_uri(identity.avatar_url, identity.name),
        "accent": accent or "#57f287",
    }


def identity_key(identity: Identity, accent: str = "") -> dict[str, str]:
    """The stable values of one identity (the name of the drawn picture).

    The bytes of the picture depend on the renderer and on the file that was
    fetched, so the file that rides the card is named after what does not
    change: the name, the address of the picture and the accent. The same
    member always draws the same file, and the reference of the card stays
    the same across restarts and renderers.
    """
    return {
        "name": identity.name,
        "avatar": identity.avatar_url,
        "accent": accent or "",
    }


def _escape(text: str) -> str:
    """The text safe to put inside an HTML template."""
    from rosemary.core.chat_bubble import escape_content

    return escape_content(text or "")


__all__ = [
    "AVATAR_CACHE_SIZE",
    "AVATAR_TIMEOUT",
    "DOWNLOAD_TIMEOUT",
    "MAX_IMAGE_BYTES",
    "Identity",
    "avatar_data_uri",
    "download_bytes",
    "extension_to_mime",
    "identity_key",
    "identity_values",
    "normalize_image",
]
