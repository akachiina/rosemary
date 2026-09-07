"""Visual theme loader: shared tokens, text templates and named styles.

The theme is the single customization surface for everything visual. It is
split into three layers:

* ``colors`` and ``emojis`` are design tokens. The Translator injects
  ``Theme.emojis`` as default placeholders, so any ``{<key>}`` in a catalog
  resolves to the emoji automatically.
* ``markdown`` holds structural text templates (titles, entries, footers).
* ``styles`` maps an open-ended style name to presentation attributes
  (``color``, ``template``, ``prefix``, ``suffix``, ``footer``) with
  inheritance through ``extends``. Any component can ask for a style by name;
  unknown names fall back to ``base``, so new themed surfaces need no schema
  changes in code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord
import yaml

log = logging.getLogger(__name__)

THEME_PATH = Path(__file__).resolve().parent / "theme.yaml"

#: Presentation properties every style can carry.
_STYLE_FIELDS = ("color", "template", "prefix", "suffix", "footer")


@dataclass(frozen=True)
class Style:
    """Resolved presentation attributes for a named style."""

    color: str
    template: str
    prefix: str
    suffix: str
    footer: str


class Theme:
    """Shared visual resources loaded from ``theme.yaml``."""

    def __init__(
        self,
        colors: dict[str, str],
        emojis: dict[str, str],
        markdown: dict[str, str] | None = None,
        styles: dict[str, dict[str, str]] | None = None,
        bump: dict[str, str] | None = None,
        medals: dict[str, str] | None = None,
    ) -> None:
        self.colors = colors
        self.emojis = emojis
        self.markdown = markdown or {}
        self.styles = styles or {}
        self.bump = bump or {}
        self.medals = medals or {}

    def color(self, name: str) -> discord.Colour:
        """Return a ``discord.Colour`` for a named theme color."""
        hex_value = self.colors.get(name)
        if hex_value is None:
            raise KeyError(f"Unknown theme color {name!r}")
        return discord.Colour(int(hex_value.lstrip("#"), 16))

    def emoji(self, name: str) -> str:
        """Return an emoji token by name (empty string when unknown)."""
        value = self.emojis.get(name, "")
        if not value:
            log.debug("Unknown theme emoji %r", name)
        return value

    def md(self, name: str, **kwargs: Any) -> str:
        """Format a named markdown template, injecting theme emojis as defaults."""
        template = self.markdown.get(name)
        if template is None:
            raise KeyError(f"Unknown markdown template {name!r}")
        return template.format(**{**self.emojis, **kwargs})

    def style(self, name: str) -> Style:
        """Resolve a style by name, merging ``extends`` and falling back to base.

        Unknown styles resolve to ``base``, so new themed surfaces work out of
        the box. Emoji placeholders inside style fields are resolved on read.
        """
        raw = self._resolve_style(name)
        return Style(
            color=self._fill(raw.get("color", "info")),
            template=raw.get("template", "title"),
            prefix=self._fill(raw.get("prefix", "")),
            suffix=self._fill(raw.get("suffix", "")),
            footer=self._fill(raw.get("footer", "")),
        )

    def styled(self, name: str, **kwargs: Any) -> str:
        """Render a style's template with its prefix/suffix decoration."""
        style = self.style(name)
        template = self.markdown.get(style.template, "{title}")
        body = template.format(**{**self.emojis, **kwargs})
        return f"{style.prefix}{body}{style.suffix}"

    def _resolve_style(self, name: str) -> dict[str, str]:
        seen: set[str] = set()
        merged: dict[str, str] = {}
        current = self.styles.get(name) or self.styles.get("base") or {}
        if name not in self.styles:
            log.debug("Unknown theme style %r; falling back to base", name)
        while current:
            merged = {**current, **merged}
            parent = current.get("extends")
            if parent is None or parent in seen:
                break
            seen.add(parent)
            current = self.styles.get(parent) or {}
        return merged

    def _fill(self, value: str) -> str:
        return value.format(**self.emojis)


def load_theme(path: Path | None = None) -> Theme:
    """Load the theme YAML, falling back to empty resources on failure."""
    theme_path = Path(path) if path else THEME_PATH
    colors: dict[str, str] = {}
    emojis: dict[str, str] = {}
    markdown: dict[str, str] = {}
    styles: dict[str, dict[str, str]] = {}
    bump: dict[str, str] = {}
    medals: dict[str, str] = {}
    if theme_path.exists():
        try:
            with theme_path.open("r", encoding="utf-8") as fh:
                data: dict[str, Any] = yaml.safe_load(fh) or {}
            colors = {str(k): str(v) for k, v in (data.get("colors") or {}).items()}
            emojis = {str(k): str(v) for k, v in (data.get("emojis") or {}).items()}
            markdown = {str(k): str(v) for k, v in (data.get("markdown") or {}).items()}
            styles = {
                str(k): {str(k2): str(v2) for k2, v2 in (v or {}).items()}
                for k, v in (data.get("styles") or {}).items()
            }
            bump = {str(k): str(v) for k, v in (data.get("bump") or {}).items()}
            medals = {str(k): str(v) for k, v in (data.get("medals") or {}).items()}
        except yaml.YAMLError as exc:
            log.error("Failed to load theme %s: %s", theme_path, exc)
    else:
        log.warning("Theme file not found: %s", theme_path)
    _validate_theme(theme_path, colors, emojis)
    return Theme(colors, emojis, markdown, styles, bump, medals)


def _validate_theme(path: Path, colors: dict[str, str], emojis: dict[str, str]) -> None:
    """Warn about theme problems at boot instead of failing later in sends.

    Shared emoji values (``loading``/``hourglass``, ``swap``/``refresh``) are
    intentional aliases so each surface stays independently customizable.
    """
    import re

    for name, value in colors.items():
        if not re.fullmatch(r"#?[0-9a-fA-F]{6}", str(value)):
            log.warning("Theme %s has invalid color %r=%r (expected hex RRGGBB)", path, name, value)
