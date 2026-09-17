"""Multi-file theme system: global ``themes/`` plus per-guild imports.

A theme is one YAML file (``cute_theme.yaml``) carrying the look & feel that
:mod:`rosemary.ui.theme` already consumed (``colors``/``emojis``/``markdown``/
``styles``) plus two new optional sections:

* ``cards:``: per-card overrides keyed by card key (``about.card``), written
  in raw Discord Components V2 (see :mod:`rosemary.core.v2_convert`) or as a
  plain string shortcut for single-text cards;
* ``pings:``: per-card mention toggles (``true``/``false``), replacing the
  old per-card editor toggle.

Layout::

    themes/                     # global themes shipped with the bot
    data/<guild_id>/themes/     # themes a guild imported via /themes
    data/<guild_id>/themes.json # {"active": "cute"} per-guild selection

Resolution per guild: the active theme's ``cards``/``pings`` override the
catalog defaults; look & feel falls back to the built-in theme for anything
the file omits. Files are validated fully at load/import: an invalid theme is
rejected with :class:`ThemeError`, never breaks a send later.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from rosemary.core.cards import CardIssue, is_embed_document, validate_document
from rosemary.core.storage import GuildStorage
from rosemary.core.v2_convert import _HEX_RE, ThemeError, convert_card_entry
from rosemary.ui.theme import Theme

log = logging.getLogger(__name__)

#: Global themes shipped with the bot (resolved from the package root, like
#: ``language/`` and ``data/``: never from the CWD).
THEMES_DIR = Path(__file__).resolve().parent.parent / "themes"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_THEME_BYTES = 64 * 1024


class RosemaryTheme(Theme):
    """A loadable theme file: look & feel plus card overrides and pings."""

    def __init__(
        self,
        colors: dict[str, str],
        emojis: dict[str, str],
        markdown: dict[str, str] | None = None,
        styles: dict[str, dict[str, str]] | None = None,
        bump: dict[str, str] | None = None,
        medals: dict[str, str] | None = None,
        star_tier_emoji: dict[Any, str] | None = None,
        *,
        name: str = "",
        cards: dict[str, dict[str, Any]] | None = None,
        pings: dict[str, bool] | None = None,
        color_panel: dict[str, str] | None = None,
    ) -> None:
        super().__init__(colors, emojis, markdown, styles, bump, medals, star_tier_emoji)
        self.name = name
        self.cards = cards or {}
        self.pings = pings or {}
        # Color panel image templates (HTML): ``html`` (page frame) +
        # ``item`` (one row per color). Missing/empty falls back to the
        # built-in layout in :mod:`rosemary.core.color_image`.
        self.color_panel = {
            str(k): str(v)
            for k, v in (color_panel or {}).items()
            if isinstance(v, str) and v.strip()
        }


def load_theme_file(path: Path, *, name: str | None = None) -> RosemaryTheme:
    """Parse and fully validate one theme YAML file.

    Raises :class:`ThemeError` on YAML errors, bad tokens or invalid card
    documents: callers reject the theme instead of failing at send time.
    """
    theme_name = name if name is not None else path.stem
    if not _NAME_RE.match(theme_name):
        raise ThemeError([CardIssue("theme_bad_name", (("name", theme_name),))])
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ThemeError([CardIssue("theme_yaml_invalid")]) from exc
    except OSError as exc:
        raise ThemeError([CardIssue("theme_read_failed")]) from exc
    if not isinstance(data, dict):
        raise ThemeError([CardIssue("theme_document_shape")])

    colors = _string_map(data.get("colors"), "theme_bad_color_token")
    for token, value in colors.items():
        if not _HEX_RE.match(value):
            raise ThemeError(
                [CardIssue("theme_bad_color_token", (("token", token),))]
            )
    emojis = _string_map(data.get("emojis"))
    markdown = _string_map(data.get("markdown"))
    styles = {
        str(style_name): {str(k): str(v) for k, v in (fields or {}).items()}
        for style_name, fields in (data.get("styles") or {}).items()
        if isinstance(fields, dict)
    }
    bump = _string_map(data.get("bump"))
    medals = _string_map(data.get("medals"))
    star_tier_emoji = {
        str(k): str(v)
        for k, v in (data.get("star_tier_emoji") or {}).items()
        if isinstance(v, str)
    }

    color_panel = {
        str(k): str(v)
        for k, v in (data.get("color_panel") or {}).items()
        if isinstance(k, str) and isinstance(v, str)
    }

    theme = RosemaryTheme(
        colors, emojis, markdown, styles, bump, medals,
        star_tier_emoji=star_tier_emoji, name=theme_name,
        color_panel=color_panel,
    )

    cards_section = data.get("cards") or {}
    if not isinstance(cards_section, dict):
        raise ThemeError([CardIssue("theme_cards_shape")])
    cards: dict[str, dict[str, Any]] = {}
    for key, entry in cards_section.items():
        if not isinstance(key, str) or not key:
            raise ThemeError([CardIssue("theme_cards_shape")])
        from rosemary.core.embed_convert import convert_embed_entry, is_embed_entry

        if is_embed_entry(entry):
            cards[key] = convert_embed_entry(key, entry)
        else:
            cards[key] = convert_card_entry(key, entry)
            cards[key]["kind"] = "v2"
        _validate_card_colors(key, cards[key], theme)
    theme.cards = cards

    pings_section = data.get("pings") or {}
    if not isinstance(pings_section, dict):
        raise ThemeError([CardIssue("theme_pings_shape")])
    theme.pings = {
        str(key): bool(value)
        for key, value in pings_section.items()
        if isinstance(value, bool)
    }
    return theme


def _string_map(raw: Any, code: str = "theme_document_shape") -> dict[str, str]:
    """``{str: str}`` from YAML, rejecting non-string values.

    Keys are coerced with ``str()``: YAML 1.1 parses bare numeric keys
    (``medals: 1: ...``) as ints, and a theme file must not be rejected for
    the same content the built-in loader tolerates."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ThemeError([CardIssue(code)])
    for _key, value in raw.items():
        if not isinstance(value, str):
            raise ThemeError([CardIssue(code)])
    return {str(key): value for key, value in raw.items()}


def _validate_card_colors(key: str, doc: dict[str, Any], theme: RosemaryTheme) -> None:
    """Color tokens inside a card must be hex or the theme's own palette."""

    def visit(node: Any) -> str | None:
        if isinstance(node, dict):
            color = node.get("color")
            if isinstance(color, str) and not _HEX_RE.match(color) and color not in theme.colors:
                return color
            for value in node.values():
                found = visit(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = visit(item)
                if found is not None:
                    return found
        return None

    if doc.get("kind") == "embed":
        # Embed docs: only the embed color token needs palette validation;
        # layout/limits were already checked by convert_embed_entry.
        color = (doc.get("embed") or {}).get("color")
        if (
            isinstance(color, str)
            and not _HEX_RE.match(color)
            and color not in theme.colors
        ):
            raise ThemeError(
                [
                    CardIssue("theme_card_invalid", (("key", key),)),
                    CardIssue("color_unknown", (("color", color),)),
                ]
            )
        return

    unknown = visit(doc.get("blocks", []))
    if unknown is not None:
        raise ThemeError(
            [
                CardIssue(
                    "theme_card_invalid",
                    (("key", key),),
                ),
                CardIssue("color_unknown", (("color", unknown),)),
            ]
        )
    # Full Discord layout validation (counts, nesting, urls...): hex colors
    # are self-contained, so the theme palette is not passed here.
    errors = validate_document(doc, draft=True)
    if errors:
        raise ThemeError([CardIssue("theme_card_invalid", (("key", key),))] + errors)


class ThemeStore:
    """Discovers, validates, imports and selects themes per guild."""

    def __init__(self, data_dir: Path | str, themes_dir: Path | None = None) -> None:
        self.themes_dir = Path(themes_dir) if themes_dir else THEMES_DIR
        self._data_dir = Path(data_dir)
        self._selection = GuildStorage(
            self._data_dir, filename="themes.json", use_defaults=False
        )
        # (guild_id, theme_name) -> RosemaryTheme; invalidated on every write.
        self._cache: dict[tuple[int, str], RosemaryTheme] = {}

    #: discovery ---------------------------------------------------------

    def _guild_dir(self, guild_id: int) -> Path:
        return self._data_dir / str(guild_id) / "themes"

    def list_global(self) -> list[str]:
        """Names of the global themes shipped in ``themes/``."""
        return self._names_in(self.themes_dir)

    def list_guild(self, guild_id: int) -> list[str]:
        """Names the guild imported (its own file wins on name clashes)."""
        return self._names_in(self._guild_dir(guild_id))

    def list_all(self, guild_id: int) -> list[str]:
        """Every selectable theme name for a guild, sorted."""
        return sorted(set(self.list_global()) | set(self.list_guild(guild_id)))

    @staticmethod
    def _names_in(directory: Path) -> list[str]:
        if not directory.is_dir():
            return []
        return sorted(
            path.stem
            for path in directory.glob("*.yaml")
            if path.is_file() and _NAME_RE.match(path.stem)
        ) + sorted(
            path.stem
            for path in directory.glob("*.yml")
            if path.is_file() and _NAME_RE.match(path.stem)
        )

    def _path_for(self, guild_id: int | None, name: str) -> Path | None:
        """File for ``name``: the guild's own file first, then global."""
        if not _NAME_RE.match(name):
            return None
        if guild_id is not None:
            local = self._guild_dir(guild_id)
            for suffix in (".yaml", ".yml"):
                candidate = local / f"{name}{suffix}"
                if candidate.is_file():
                    return candidate
        for suffix in (".yaml", ".yml"):
            candidate = self.themes_dir / f"{name}{suffix}"
            if candidate.is_file():
                return candidate
        return None

    def load(self, name: str, guild_id: int | None = None) -> RosemaryTheme:
        """Load and validate the theme ``name`` (guild file wins over global)."""
        path = self._path_for(guild_id, name)
        if path is None:
            raise ThemeError([CardIssue("theme_not_found", (("name", name),))])
        return load_theme_file(path, name=name)

    #: per-guild selection -----------------------------------------------

    async def get_active(self, guild_id: int) -> str | None:
        """The guild's selected theme name or ``None`` (built-in default)."""
        data = await self._selection.get(guild_id)
        value = data.get("active")
        return value if isinstance(value, str) and value else None

    async def set_active(self, guild_id: int, name: str) -> None:
        """Select a theme for the guild (must exist). Clears any cache."""
        if self._path_for(guild_id, name) is None:
            raise ThemeError([CardIssue("theme_not_found", (("name", name),))])
        await self._selection.set(guild_id, "active", name)
        self._cache.pop((guild_id, name), None)

    async def clear_active(self, guild_id: int) -> None:
        """Back to the built-in default theme."""
        await self._selection.delete_keys(guild_id, "active")

    def invalidate_guild(self, guild_id: int) -> None:
        """Drop every cached theme for this guild (files edited on disk).

        The next ``effective_theme`` re-reads the active file instead of
        returning the stale cached object."""
        for key in [k for k in self._cache if k[0] == guild_id]:
            self._cache.pop(key, None)

    async def effective_theme(self, bot, guild_id: int | None) -> Theme:
        """The guild's active theme, falling back to the built-in one.

        Loaded themes are cached per ``(guild_id, name)``; writes invalidate.
        A missing/corrupt active file falls back to the built-in theme so one
        deleted file can never break sends.
        """
        if guild_id is None:
            return bot.theme
        active = await self.get_active(guild_id)
        if active is None:
            return bot.theme
        cached = self._cache.get((guild_id, active))
        if cached is not None:
            return cached
        try:
            theme = self.load(active, guild_id=guild_id)
        except (ThemeError, OSError) as exc:
            log.warning(
                "Active theme %r unusable in guild %s: %s", active, guild_id, exc
            )
            return bot.theme
        self._cache[(guild_id, active)] = theme
        return theme

    #: import / export ----------------------------------------------------

    async def import_theme(self, guild_id: int, filename: str, payload: bytes) -> str:
        """Validate and store an uploaded theme; returns its name.

        The file is parsed and fully validated *before* anything is written,
        so a rejected import leaves the guild's theme folder untouched.
        """
        stem = Path(filename).stem.lower().replace(" ", "_")
        if not _NAME_RE.match(stem):
            raise ThemeError([CardIssue("theme_bad_name", (("name", stem),))])
        if not filename.lower().endswith((".yaml", ".yml")):
            raise ThemeError([CardIssue("theme_bad_extension")])
        if len(payload) > _MAX_THEME_BYTES:
            raise ThemeError([CardIssue("theme_too_large")])
        target = self._guild_dir(guild_id) / f"{stem}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        try:
            self.load(stem, guild_id=guild_id)
        except ThemeError:
            target.unlink(missing_ok=True)
            raise
        self._cache.pop((guild_id, stem), None)
        return stem

    def export_theme(self, guild_id: int, name: str) -> tuple[str, bytes] | None:
        """Raw file bytes for ``name`` (guild's own first, then global)."""
        path = self._path_for(guild_id, name)
        if path is None:
            return None
        return (path.name, path.read_bytes())

    async def remove_guild_theme(self, guild_id: int, name: str) -> bool:
        """Delete a guild-imported theme file; deselects it when active."""
        removed = False
        for suffix in (".yaml", ".yml"):
            candidate = self._guild_dir(guild_id) / f"{name}{suffix}"
            if candidate.is_file():
                candidate.unlink()
                removed = True
        self._cache.pop((guild_id, name), None)
        if removed and await self.get_active(guild_id) == name:
            await self.clear_active(guild_id)
        return removed


def theme_store(bot) -> ThemeStore:
    """The bot's store (created once in ``_setup``), or a transient one."""
    store = getattr(bot, "_theme_store", None)
    if store is not None:
        return store
    return ThemeStore(bot.storage.data_dir)


#: resolution ------------------------------------------------------------


async def preload_themes(bot) -> None:
    """Snapshot every guild's effective theme into ``bot._theme_cache``.

    ``theme_for`` is synchronous (mention checks run inside send paths), so the
    async work happens here: at boot, when a guild joins and after any
    ``/themes`` selection or import. Guilds without an active theme resolve to
    the built-in theme and are simply absent from the cache.
    """
    store = theme_store(bot)
    cache: dict[int, Theme] = getattr(bot, "_theme_cache", {}) or {}
    cache.clear()
    for guild in list(getattr(bot, "guilds", [])):
        try:
            theme = await store.effective_theme(bot, guild.id)
        except Exception as exc:  # defensive: one bad file never breaks boot
            log.warning("Theme preload failed for guild %s: %s", guild.id, exc)
            continue
        if theme is not bot.theme:
            cache[guild.id] = theme
    bot._theme_cache = cache


def theme_for(bot, guild_id: int | None) -> Theme:
    """The guild's effective theme, synchronously (see :func:`preload_themes`).

    Falls back to the built-in theme when the guild has no active theme or its
    snapshot is cold: a cold snapshot means the built-in look until the next
    preload, never a broken send.
    """
    if guild_id is None:
        return bot.theme
    cache = getattr(bot, "_theme_cache", None)
    if isinstance(cache, dict):
        theme = cache.get(guild_id)
        if theme is not None:
            return theme
    return bot.theme


async def card_document(bot, guild_id: int | None, key: str) -> dict[str, Any] | None:
    """The guild's themed override document for ``key`` (``None`` = default).

    Resolution: active theme file's ``cards.<key>`` -> ``None`` (the caller
    keeps its own catalog/builder default). Validates lazily too: files were
    already validated at load, this is belt-and-braces against hand edits.
    """
    if guild_id is None:
        return None
    store = theme_store(bot)
    theme = await store.effective_theme(bot, guild_id)
    cards = getattr(theme, "cards", None)
    doc = cards.get(key) if isinstance(cards, dict) else None
    if doc is None:
        return None
    if is_embed_document(doc):
        # Embed docs carry no V2 blocks; shape and limits were fully
        # validated at load (convert_embed_entry): pass them through.
        return doc
    if validate_document(doc, draft=True):
        log.warning(
            "Themed card %s in %r is invalid; using default",
            key,
            getattr(theme, "name", "?"),
        )
        return None
    return doc


def card_origin(bot, guild_id: int | None, key: str) -> tuple[str | None, str | None]:
    """``(theme_name, key)`` when the active theme overrides ``key``, else ``(None, None)``.

    Synchronous on purpose: reads the same snapshot :func:`theme_for` uses.
    """
    if guild_id is None:
        return (None, None)
    theme = theme_for(bot, guild_id)
    cards = getattr(theme, "cards", None)
    if isinstance(cards, dict) and key in cards:
        return (getattr(theme, "name", None) or "?", key)
    return (None, None)


def dump_selection(data_dir: Path | str, guild_id: int) -> dict[str, Any]:
    """Debug helper: the raw selection file for one guild."""
    path = Path(data_dir) / str(guild_id) / "themes.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
