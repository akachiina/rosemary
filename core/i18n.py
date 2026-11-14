"""Per-guild internationalization for Rosemary.

Loads the catalogs at boot: one folder per language
(``language/<code>/<feature>.yaml``) or the old single file
(``language/<code>.yaml``). All user-facing strings are translation keys;
code and comments stay in English. The per-guild language is resolved through an
injected resolver; missing keys fall back to the default language, then to the
raw key itself.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from rosemary.core.assets import resolve_refs

log = logging.getLogger(__name__)

FALLBACK_LANGUAGE = "en-US"

# Top-level YAML key holding per-language display metadata (name + flag).
_META_KEY = "language_meta"

# A resolver returns the effective language code for a guild (async or sync).
LanguageResolver = Callable[[int | None], str | Awaitable[str]]


class Translator:
    """Holds loaded language catalogs and provides key lookups.

    Usage::

        translator = Translator(Path("language"), resolver=_guild_language)
        text = await translator.t(guild_id, "about.title", **{"version": "0.1.0"})
    """

    def __init__(
        self,
        languages_dir: Path,
        resolver: LanguageResolver | None = None,
        default_placeholders: dict[str, str] | None = None,
    ) -> None:
        self.languages_dir = Path(languages_dir)
        self.resolver = resolver
        # Theme emojis, injected into every format call (explicit vars win).
        self._default_placeholders = dict(default_placeholders or {})
        self._catalogs: dict[str, dict[str, Any]] = {}
        self._meta: dict[str, dict[str, str]] = {}
        self._available: list[str] = []
        self.load()

    def load(self) -> None:
        """Load every catalog, flattened to dotted keys.

        A catalog is a **folder** (``language/<code>/<feature>.yaml``, the
        modular format) or the old single file (``language/<code>.yaml``).
        The folder wins when both exist: the feature files are the source
        of truth, the single file is the legacy shape that still loads.
        """
        self._catalogs = {}
        self._meta = {}
        self._available = []
        if not self.languages_dir.exists():
            log.warning("Languages directory not found: %s", self.languages_dir)
            return
        for code, path in self._catalog_sources():
            try:
                data = self._read_catalog(path)
            except yaml.YAMLError as exc:
                log.error("Failed to load language %s: %s", path, exc)
                continue
            meta = data.get(_META_KEY) or {}
            self._meta[code] = {
                "name": str(meta.get("name", code)),
                "flag": str(meta.get("flag", "")),
            }
            translations = {k: v for k, v in data.items() if k != _META_KEY}
            self._catalogs[code] = _flatten(translations)
            self._available.append(code)
        log.info("Loaded %d languages: %s", len(self._available), ", ".join(self._available))

    def _catalog_sources(self) -> list[tuple[str, Path]]:
        """The catalogs of the directory: the folders first, then the files."""
        folders = {
            path.name: path
            for path in sorted(self.languages_dir.iterdir())
            if path.is_dir()
        }
        sources: list[tuple[str, Path]] = [
            (code, path) for code, path in folders.items()
        ]
        files = sorted(self.languages_dir.glob("*.yaml")) + sorted(
            self.languages_dir.glob("*.yml")
        )
        for path in files:
            if path.stem in folders:
                # The folder carries this language: the single file is the
                # legacy shape and never wins.
                continue
            sources.append((path.stem, path))
        return sources

    def _read_catalog(self, path: Path) -> dict[str, Any]:
        """One catalog read: a folder of feature files or a single file."""
        if not path.is_dir():
            return _read_yaml(path)
        merged: dict[str, Any] = {}
        files = sorted(path.glob("*.yaml")) + sorted(path.glob("*.yml"))
        for file in files:
            data = _read_yaml(file)
            for key in data:
                if key in merged:
                    log.warning(
                        "Language %s: the section %r is declared in two files",
                        path.name,
                        key,
                    )
            merged = _deep_merge(merged, data)
        return merged

    @property
    def available_languages(self) -> list[str]:
        """Return the codes of the loaded language catalogs."""
        return list(self._available)

    def language_display(self, code: str) -> str:
        """Return a display label for a language code: ``"{flag} {name}"``."""
        meta = self._meta.get(code, {})
        flag = meta.get("flag", "")
        name = meta.get("name", code)
        return f"{flag} {name}".strip()

    async def resolve_async(self, guild_id: int | None) -> str:
        """Resolve the guild language, awaiting an async resolver if provided."""
        if self.resolver is not None:
            result = self.resolver(guild_id)
            if isinstance(result, Awaitable):
                result = await result
            if result:
                return str(result)
        return FALLBACK_LANGUAGE

    async def raw(self, guild_id: int | None, key: str) -> Any:
        """Return the unformatted template for ``key`` (placeholders intact).

        Missing keys fall back like :meth:`t` and ultimately return the key
        itself. Theme emojis are NOT injected: callers decide how to render.
        Asset references (``{emojis/op_emoji}``) ARE resolved: they are
        real Discord emojis, not variables.
        """
        language = await self.resolve_async(guild_id)
        catalog = self._catalogs.get(language, {})
        template = catalog.get(key)
        if template is None and language != FALLBACK_LANGUAGE:
            template = self._catalogs.get(FALLBACK_LANGUAGE, {}).get(key)
        if template is None:
            return key
        return resolve_refs(template) if isinstance(template, str) else template

    async def t(self, guild_id: int | None, key: str, **variables: Any) -> str:
        """Translate ``key`` for the given guild's language.

        Falls back to the default language, then to the raw key when missing;
        swallows formatting errors so a bad template never breaks the command.
        """
        template = await self.raw(guild_id, key)
        if not isinstance(template, str):
            log.warning("Translation %r is not a string; returning as-is", key)
            return template
        try:
            variables = {**self._default_placeholders, **variables}
            return resolve_refs(template.format(**variables))
        except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("Failed to format translation %r: %s", key, exc)
            return template


@cache
def _read_yaml(path: Path) -> dict[str, Any]:
    """One YAML file of a catalog (an empty file is an empty section).

    Cached by path: a test run constructs many translators, and the files
    never change while the process runs. The caller only reads the result.
    """
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise yaml.YAMLError(f"{path.name}: the catalog must be a mapping")
    return data


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Two mappings merged deeply (the overlay wins on the leaves)."""
    out = dict(base)
    for key, value in overlay.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = _deep_merge(current, value)
            continue
        out[key] = value
    return out


def _flatten(data: dict, prefix: str = "") -> dict:
    """Flatten a nested dict into dotted-key entries: {'a': {'b': 1}} -> {'a.b': 1}."""
    flat: dict = {}
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, dotted))
        else:
            flat[dotted] = value
    return flat
