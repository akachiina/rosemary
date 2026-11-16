"""Per-theme assets: the media and emoji files a theme package carries.

A theme package may ship an ``assets/`` folder, split in two:

* ``assets/media/``  - images, fonts and other files a theme references in
  its HTML templates (``color_panel``, ``snapshot``) and, later, in cards and
  roleplay gifs. Allowed by default (the host may forbid it with
  ``themes.assets.media: false``).
* ``assets/emojis/`` - emoji images the theme uploads as application emojis
  (namespaced ``<theme>/<name>``). Off by default, because every uploaded
  emoji is a **global** application emoji and consumes the app quota
  (``themes.assets.emojis: true`` to enable).

The module is Discord-free on purpose: it only knows about paths and strings.
The card/HTML send paths build the actual ``discord.File`` from these paths.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The two asset sub-folders of a theme package.
MEDIA_DIR = "media"
EMOJIS_DIR = "emojis"

#: The placeholder a theme writes for a media file: ``{media/<name>}``.
MEDIA_REF_RE = re.compile(r"\{media/([A-Za-z0-9_.\-/]+)\}")

#: File types allowed under ``assets/media/`` (images + fonts for the HTML
#: renderers; video/audio are allowed too because the card/roleplay paths
#: attach them as-is, never through WeasyPrint).
MEDIA_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".svg",
        ".bmp",
        ".tif",
        ".tiff",
        ".ico",
        ".mp4",
        ".webm",
        ".mov",
        ".mp3",
        ".ogg",
        ".wav",
        ".flac",
        ".m4a",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
    }
)

#: File types allowed under ``assets/emojis/`` (Discord's emoji limits).
EMOJI_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"})

#: Hard caps (a theme is decoration, not a CDN): a per-file cap and a total
#: cap keep WeasyPrint and the uploader fast.
MAX_MEDIA_FILE_BYTES = 5 * 1024 * 1024
MAX_MEDIA_TOTAL_BYTES = 20 * 1024 * 1024
MAX_MEDIA_FILES = 128


@dataclasses.dataclass(frozen=True)
class ThemeAssets:
    """The assets a theme package carries, indexed by relative key.

    ``media`` and ``emojis`` map a normalized key (the path without the
    extension, e.g. ``logo`` or ``bg/padrao``) to the file on disk.
    """

    media: dict[str, Path] = dataclasses.field(default_factory=dict)
    emojis: dict[str, Path] = dataclasses.field(default_factory=dict)

    def media_path(self, key: str) -> Path | None:
        """The file of a media key (``None`` when the theme has none)."""
        return self.media.get(_normalize_key(key))

    def emoji_path(self, key: str) -> Path | None:
        """The file of an emoji key (``None`` when the theme has none)."""
        return self.emojis.get(_normalize_key(key))

    def media_url(self, key: str) -> str:
        """The ``file://`` URL of a media file (``""`` when it is absent)."""
        path = self.media_path(key)
        return path.resolve().as_uri() if path is not None else ""

    def is_empty(self) -> bool:
        return not self.media and not self.emojis


def _normalize_key(key: str) -> str:
    """The registry key of a reference (braces, extension and dots off)."""
    clean = (key or "").strip().strip("{}").replace("\\", "/").strip()
    while clean.startswith("./"):
        clean = clean[2:]
    clean = clean.lstrip("/")
    lower = clean.lower()
    for suffix in MEDIA_SUFFIXES | EMOJI_SUFFIXES:
        if lower.endswith(suffix):
            clean = clean[: -len(suffix)]
            break
    return clean


def scan_theme_assets(root: Path | str) -> ThemeAssets:
    """Index the ``assets/`` folder of a theme package (empty when absent)."""
    base = Path(root) / "assets"
    media = _scan_dir(base / MEDIA_DIR, MEDIA_SUFFIXES)
    emojis = _scan_dir(base / EMOJIS_DIR, EMOJI_SUFFIXES)
    return ThemeAssets(media=media, emojis=emojis)


def _scan_dir(directory: Path, suffixes: frozenset[str]) -> dict[str, Path]:
    """``{key: path}`` for the files of one asset sub-folder."""
    found: dict[str, Path] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in suffixes:
            continue
        rel = path.relative_to(directory)
        key = rel.as_posix().rsplit(".", 1)[0]
        found[key] = path
    return found


def ensure_assets_layout(root: Path | str) -> None:
    """Create the ``assets/media`` and ``assets/emojis`` folders (export base).

    A ``.gitkeep`` keeps the (otherwise empty) folders inside the exported
    ZIP and inside git, so the theme author sees where the files go.
    """
    base = Path(root) / "assets"
    for name in (MEDIA_DIR, EMOJIS_DIR):
        folder = base / name
        folder.mkdir(parents=True, exist_ok=True)
        keep = folder / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")


def media_urls(assets: ThemeAssets) -> dict[str, str]:
    """``{key: file:// url}`` for every media file of a theme."""
    return {key: path.resolve().as_uri() for key, path in assets.media.items()}


def resolve_media_refs(text: str, urls: Mapping[str, str]) -> str:
    """Replace every ``{media/<name>}`` in ``text`` with its URL.

    An unknown reference becomes ``""`` (never the raw placeholder): a
    broken media reference must not leak ``{media/...}`` into a message,
    the same contract the asset registry keeps for emojis.
    """
    if not text or "{" not in text:
        return text

    def replace(match: re.Match[str]) -> str:
        return urls.get(_normalize_key(match.group(1)), "")

    return MEDIA_REF_RE.sub(replace, text)


def validate_theme_assets(root: Path | str) -> list[str]:
    """The problems of a theme's ``assets/`` folder (empty means it is sound).

    Returns human-readable reasons (the caller turns them into a
    ``ThemeError``). The checks are size/count/type: a theme is decoration,
    so an oversized or non-asset file is rejected at import instead of
    breaking a render later.
    """
    base = Path(root) / "assets"
    problems: list[str] = []
    if not base.is_dir():
        return problems
    total = 0
    count = 0
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.name == ".gitkeep":
            continue
        rel = path.relative_to(base).as_posix()
        if rel.split("/")[0] not in (MEDIA_DIR, EMOJIS_DIR):
            problems.append(f"{rel}: only assets/{MEDIA_DIR} and assets/{EMOJIS_DIR} are read")
            continue
        if path.suffix.lower() not in (MEDIA_SUFFIXES | EMOJI_SUFFIXES):
            problems.append(f"{rel}: unsupported asset type")
            continue
        size = path.stat().st_size
        if size > MAX_MEDIA_FILE_BYTES:
            problems.append(f"{rel}: {size} bytes is over the {MAX_MEDIA_FILE_BYTES} limit")
        total += size
        count += 1
    if count > MAX_MEDIA_FILES:
        problems.append(f"{count} files is over the {MAX_MEDIA_FILES} limit")
    if total > MAX_MEDIA_TOTAL_BYTES:
        problems.append(f"{total} bytes total is over the {MAX_MEDIA_TOTAL_BYTES} limit")
    return problems


def emoji_asset_key(theme_name: str, key: str) -> str:
    """The registry key of a theme emoji (``<theme>/<name>``)."""
    return f"{theme_name}/{_normalize_key(key)}"


def emoji_asset_filename(theme_name: str, key: str) -> str:
    """The Discord-safe application-emoji name for a theme emoji.

    Namespaced with the theme so two themes can carry an emoji with the same
    local name without colliding in the global application quota.
    """
    from rosemary.core.assets import emoji_name

    return emoji_name(f"t_{theme_name}_{Path(key).stem}")


def _unused(*_args: Any) -> None:  # pragma: no cover - keeps linters calm
    """Placeholder for future helpers (kept out of ``__all__``)."""


__all__ = [
    "EMOJIS_DIR",
    "EMOJI_SUFFIXES",
    "MAX_MEDIA_FILES",
    "MAX_MEDIA_FILE_BYTES",
    "MAX_MEDIA_TOTAL_BYTES",
    "MEDIA_DIR",
    "MEDIA_REF_RE",
    "MEDIA_SUFFIXES",
    "ThemeAssets",
    "emoji_asset_filename",
    "emoji_asset_key",
    "ensure_assets_layout",
    "media_urls",
    "resolve_media_refs",
    "scan_theme_assets",
    "validate_theme_assets",
]
