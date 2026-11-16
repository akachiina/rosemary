"""Asset registry: the images shipped with the bot, uploaded as app emojis.

``assets/`` is the single source of the bot's visual assets. Everything
under ``assets/emojis/`` becomes a real Discord emoji:

* ``assets/emojis/<name>.png`` is an APPLICATION emoji: the bot uploads it
  once, at startup, and only when Discord does not have it yet. The API is
  the source of truth, so a restart uploads nothing.
* ``assets/emojis/<server>/<name>.png`` is reserved for the per-server
  import: it becomes a GUILD emoji of that server (not wired yet).

Any asset is referenced by its path relative to ``assets/``, with or
without the extension::

    assets/emojis/op_emoji.png  ->  {emojis/op_emoji}

The reference resolves to the real emoji mention (``<:op_emoji:123>``), so
it renders natively in cards, DMs, buttons and plain text. An unknown key
resolves to an empty string: a broken reference never leaks raw text into
a message.

Discord's own limits are enforced before an upload: 2000 emojis per
application, 256 KB per file, JPEG/PNG/GIF/WEBP/AVIF only and a name with
at least two alphanumeric/underscore characters.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Root of the shipped assets (the folder lives next to ``core/``).
ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"

#: The folder that carries the emojis (the only one uploaded at startup).
EMOJIS_DIR = "emojis"

#: Discord's application emoji limits.
MAX_EMOJIS = 2000
MAX_EMOJI_BYTES = 256 * 1024

#: Image types Discord accepts for an emoji.
ALLOWED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"})

#: A reference is a path with at least one folder (``{emojis/op_emoji}``).
#: A plain ``{name}`` is a card variable, never an asset.
REF_RE = re.compile(r"\{([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+)\}")

#: Discord's emoji name rules: alphanumeric and underscore, two chars or more.
_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


@dataclasses.dataclass
class Asset:
    """One registered asset: where it comes from and how Discord names it."""

    key: str
    name: str
    scope: str = "app"  # "app" or "guild:<server>"
    path: Path | None = None
    data: bytes | None = None

    def payload(self) -> bytes | None:
        """The image bytes (``None`` when the file is gone or unreadable)."""
        if self.data is not None:
            return self.data
        if self.path is None:
            return None
        try:
            return self.path.read_bytes()
        except OSError as exc:
            log.warning("Asset %s could not be read: %s", self.key, exc)
            return None


def emoji_name(stem: str) -> str:
    """Discord-safe emoji name for a file stem (alphanumeric + underscore)."""
    name = _NAME_RE.sub("_", stem or "")
    if len(name) < 2:
        name = (name + "__")[:2]
    return name


def _unique_name(name: str, used: set[str]) -> str:
    """A name no other asset took yet (``name``, ``name_2``, ``name_3``...)."""
    if name not in used:
        used.add(name)
        return name
    index = 2
    while f"{name}_{index}" in used:
        index += 1
    unique = f"{name}_{index}"
    used.add(unique)
    return unique


def _normalize_key(key: str) -> str:
    """The registry key of a reference (braces, slashes and extension off)."""
    clean = (key or "").strip().strip("{}").replace("\\", "/").strip()
    while clean.startswith("./"):
        clean = clean[2:]
    clean = clean.lstrip("/")
    for suffix in ALLOWED_SUFFIXES:
        if clean.lower().endswith(suffix):
            clean = clean[: -len(suffix)]
            break
    return clean


class AssetRegistry:
    """The bot's assets, indexed by their path relative to ``assets/``."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else ASSETS_DIR
        self._assets: dict[str, Asset] = {}
        self._mentions: dict[str, str] = {}
        self._warned: set[str] = set()

    # -- indexing ------------------------------------------------------------

    def scan(self) -> list[Asset]:
        """Index every emoji asset under the root.

        Only ``assets/emojis/`` carries emojis: a file in another folder (or
        loose in ``assets/``) is skipped with a warning, because a key
        without a folder would collide with a plain card variable. A file
        directly under ``assets/emojis/`` is an application emoji; a
        subfolder marks the per-server scope (``guild:<folder>``).
        """
        self._assets = {}
        used: set[str] = set()
        found: list[Asset] = []
        if not self.root.is_dir():
            log.info("No assets folder at %s: no asset emoji to upload", self.root)
            return found
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            if rel.split("/")[0] != EMOJIS_DIR:
                log.warning(
                    "Asset %s skipped: only %s/ carries emojis", rel, EMOJIS_DIR
                )
                continue
            if path.suffix.lower() not in ALLOWED_SUFFIXES:
                log.warning("Asset %s skipped: unsupported image type", rel)
                continue
            key = rel.rsplit(".", 1)[0]
            parts = key.split("/")
            scope = "app" if len(parts) == 2 else f"guild:{parts[1]}"
            raw_name = emoji_name(path.stem)
            if raw_name != path.stem:
                log.warning(
                    "Asset %s renamed to %r (Discord's emoji name rules)", rel, raw_name
                )
            asset = Asset(
                key=key,
                name=_unique_name(raw_name, used),
                scope=scope,
                path=path,
            )
            self._assets[key] = asset
            found.append(asset)
        log.info("Indexed %d asset(s) under %s", len(self._assets), self.root)
        return found

    def add_bytes(
        self, key: str, data: bytes, *, name: str = "", scope: str = "guild"
    ) -> Asset:
        """Register an asset that does not come from a file (runtime import).

        The per-server emoji import will use this: the bytes arrive from
        Discord (or from a file the server uploaded) and the registry hands
        the reference back like any shipped asset.
        """
        clean = _normalize_key(key)
        if not clean:
            raise ValueError(f"invalid asset key {key!r}")
        asset = Asset(
            key=clean,
            name=name or emoji_name(clean.rsplit("/", 1)[-1]),
            scope=scope,
            data=data,
        )
        self._assets[clean] = asset
        return asset

    # -- uploading -----------------------------------------------------------

    def scan_theme_emojis(self, theme_name: str, package_dir: Path) -> list[Asset]:
        """Index the ``assets/emojis/`` of a theme package (namespaced).

        Theme emojis are application emojis: the key is ``<theme>/<name>``
        and the Discord name is prefixed (``t_<theme>_<name>``) so two themes
        can carry the same local name without colliding in the global
        application quota. This only registers them; the caller uploads via
        :meth:`sync` when the host allows it (``themes.assets.emojis``).
        """
        from rosemary.core.theme_assets import emoji_asset_filename, scan_theme_assets

        found: list[Asset] = []
        for key, path in scan_theme_assets(package_dir).emojis.items():
            full = f"{theme_name}/{key}"
            asset = Asset(
                key=full,
                name=emoji_asset_filename(theme_name, key),
                scope="app",
                path=path,
            )
            self._assets[full] = asset
            found.append(asset)
        return found

    async def sync(self, bot: Any) -> int:
        """Upload the application emojis Discord does not have yet.

        The API is the source of truth: an emoji the application already
        owns is adopted as-is, so a restart uploads nothing. Returns how
        many emojis were sent.
        """
        import discord

        existing: dict[str, str] = {}
        try:
            for emoji in await bot.fetch_emojis():
                existing[emoji.name] = emoji.mention
        except discord.HTTPException as exc:
            log.warning("Could not fetch the application emojis: %s", exc)
            return 0
        sent = 0
        for key, asset in self._assets.items():
            if asset.scope != "app":
                continue
            mention = existing.get(asset.name)
            if mention:
                self._mentions[key] = mention
                continue
            if len(existing) >= MAX_EMOJIS:
                log.warning(
                    "Asset %s skipped: the application already has %d emojis",
                    key,
                    len(existing),
                )
                continue
            payload = asset.payload()
            if payload is None:
                continue
            if len(payload) > MAX_EMOJI_BYTES:
                log.warning(
                    "Asset %s is %d bytes: the limit is %d",
                    key,
                    len(payload),
                    MAX_EMOJI_BYTES,
                )
                continue
            try:
                emoji = await bot.create_emoji(name=asset.name, image=payload)
            except discord.HTTPException as exc:
                log.warning("Could not upload the asset emoji %s: %s", key, exc)
                continue
            self._mentions[key] = emoji.mention
            existing[asset.name] = emoji.mention
            sent += 1
        owned = {asset.name for asset in self._assets.values() if asset.scope == "app"}
        for name in existing:
            if name not in owned:
                log.info(
                    "Application emoji %r has no local file: left alone", name
                )
        return sent

    # -- resolution ----------------------------------------------------------

    def mention(self, key: str, *, guild_id: int | None = None) -> str:
        """The Discord mention of an asset (``""`` when it is unknown)."""
        clean = _normalize_key(key)
        if not clean:
            return ""
        return self._mentions.get(clean, "")

    def mentions(self, *, guild_id: int | None = None) -> dict[str, str]:
        """Every known asset reference mapped to its mention."""
        return dict(self._mentions)

    def keys(self) -> list[str]:
        """The registry keys (the paths relative to ``assets/``)."""
        return sorted(self._assets)

    def resolve(self, text: str, *, guild_id: int | None = None) -> str:
        """Replace every ``{path/to/asset}`` reference with its mention.

        An unknown reference resolves to an empty string (never the raw
        text): a broken asset must not leak ``{emojis/whatever}`` into a
        message.
        """
        if not text or "{" not in text:
            return text

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            mention = self.mention(key, guild_id=guild_id)
            if not mention and key not in self._warned:
                self._warned.add(key)
                log.warning("Unknown asset reference %r", key)
            return mention

        return REF_RE.sub(replace, text)


#: The process-wide registry: the application's emojis are global, so the
#: resolver is too. ``bot.assets`` points at this very object.
REGISTRY = AssetRegistry()


def resolve_refs(text: str, *, guild_id: int | None = None) -> str:
    """Resolve the asset references of ``text`` (the process-wide registry)."""
    return REGISTRY.resolve(text, guild_id=guild_id)


__all__ = [
    "ALLOWED_SUFFIXES",
    "ASSETS_DIR",
    "EMOJIS_DIR",
    "MAX_EMOJIS",
    "MAX_EMOJI_BYTES",
    "REF_RE",
    "REGISTRY",
    "Asset",
    "AssetRegistry",
    "emoji_name",
    "resolve_refs",
]
