"""Per-guild internationalization for Rosemary.

Loads ``language/*.yaml`` at boot. All user-facing strings are translation keys;
code and comments stay in English. The per-guild language is resolved through an
injected resolver; missing keys fall back to the default language, then to the
raw key itself.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml

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
        """Load every ``*.yaml`` in the languages directory, flattened to dotted keys."""
        self._catalogs = {}
        self._meta = {}
        self._available = []
        if not self.languages_dir.exists():
            log.warning("Languages directory not found: %s", self.languages_dir)
            return
        for path in sorted(self.languages_dir.glob("*.yaml")):
            code = path.stem
            try:
                with path.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                meta = data.get(_META_KEY) or {}
                self._meta[code] = {
                    "name": str(meta.get("name", code)),
                    "flag": str(meta.get("flag", "")),
                }
                translations = {k: v for k, v in data.items() if k != _META_KEY}
                self._catalogs[code] = _flatten(translations)
                self._available.append(code)
            except yaml.YAMLError as exc:
                log.error("Failed to load language %s: %s", path, exc)
        log.info("Loaded %d languages: %s", len(self._available), ", ".join(self._available))

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
        itself. Theme emojis are NOT injected — callers decide how to render.
        """
        language = await self.resolve_async(guild_id)
        catalog = self._catalogs.get(language, {})
        template = catalog.get(key)
        if template is None and language != FALLBACK_LANGUAGE:
            template = self._catalogs.get(FALLBACK_LANGUAGE, {}).get(key)
        return template if template is not None else key

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
            return template.format(**variables)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("Failed to format translation %r: %s", key, exc)
            return template


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
