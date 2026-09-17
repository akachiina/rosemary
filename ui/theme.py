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
import re
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
        star_tier_emoji: dict[Any, str] | None = None,
    ) -> None:
        self.colors = colors
        self.emojis = emojis
        self.markdown = markdown or {}
        self.styles = styles or {}
        self.bump = bump or {}
        self.medals = medals or {}
        self.star_tier_emoji = star_tier_emoji or {}

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

    #: star tier ramp ------------------------------------------------------

    def star_ramp(self, stars: int) -> str:
        """The accent color for a star count, as a hex string.

        Continuous ramp from ``star_ramp.from_color`` (few stars) toward
        ``star_ramp.to_color`` (many stars), reaching the strong end at
        ``star_ramp.limit`` and holding there: every star nudges the tone,
        like the legacy bot. ``from_color``/``to_color`` accept theme color
        token names or ``#rrggbb``. Missing/malformed ramp config falls back
        to the ``star_tier_1`` style color, so guild themes without a ramp
        keep a working look.
        """
        ramp = self.star_ramp_config()
        if ramp is None:
            return self.style("star_tier_1").color
        from_color, to_color, limit = ramp
        try:
            count = max(1, int(stars))
        except (TypeError, ValueError):
            count = 1
        ratio = min(1.0, (count - 1) / max(1, limit - 1))
        return _blend_hex(from_color, to_color, ratio)

    def star_ramp_config(self) -> tuple[str, str, int] | None:
        """``(from_color, to_color, limit)`` for the star ramp, or ``None``.

        ``from_color``/``to_color`` are theme color token names or hex;
        ``limit`` is the star count that reaches the strong end (minimum 1).
        """
        ramp = self.styles.get("star_ramp")
        if not isinstance(ramp, dict):
            return None
        start = ramp.get("from_color")
        end = ramp.get("to_color")
        limit = ramp.get("limit", ramp.get("to", ramp.get("at")))
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return None
        if not isinstance(start, str) or not isinstance(end, str) or limit < 1:
            return None
        from_color = self.colors.get(start, start)
        to_color = self.colors.get(end, end)
        if not _HEX.fullmatch(from_color) or not _HEX.fullmatch(to_color):
            return None
        return from_color, to_color, limit

    def star_title_emoji(self, stars: int) -> str:
        """The title emoji for a star count (milestone map, highest wins).

        Theme ``star_tier_emoji:`` maps a star count to a glyph; the highest
        threshold reached wins, below the lowest one the emoji falls back to
        the plain ``star`` token. (The attribute holds the raw YAML map;
        the method reads it through :meth:`star_tier_emoji_map`.)
        """
        milestones = self.star_tier_emoji_map()
        best = 0
        for threshold in sorted(milestones):
            if stars >= threshold and threshold > best:
                best = threshold
        if best:
            return milestones[best]
        return self.emoji("star") or "⭐"

    def star_tier_emoji_map(self) -> dict[int, str]:
        """Parsed ``star_tier_emoji:`` milestone map (int keys, non-empty)."""
        raw = getattr(self, "star_tier_emoji", None)
        if not isinstance(raw, dict):
            return {}
        parsed: dict[int, str] = {}
        for key, glyph in raw.items():
            try:
                threshold = int(key)
            except (TypeError, ValueError):
                continue
            if threshold >= 1 and isinstance(glyph, str) and glyph.strip():
                parsed[threshold] = glyph
        return parsed

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


#: ``#rrggbb`` / ``rrggbb``: the only color forms the ramp blends.
_HEX = re.compile(r"#?[0-9a-fA-F]{6}")


def _blend_hex(start: str, end: str, ratio: float) -> str:
    """Linear RGB blend ``start`` -> ``end`` (``ratio`` 0..1), as ``#rrggbb``."""
    def channels(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    r1, g1, b1 = channels(start)
    r2, g2, b2 = channels(end)
    mix = lambda a, b: round(a + (b - a) * ratio)  # noqa: E731
    return f"#{mix(r1, r2):02X}{mix(g1, g2):02X}{mix(b1, b2):02X}"


def load_theme(path: Path | None = None) -> Theme:
    """Load the theme YAML, falling back to empty resources on failure.

    Without an explicit path the canonical ``themes/default.yaml`` is loaded
   : the same file :class:`~rosemary.core.themes.ThemeStore` lists as the
    selectable "default" theme, so bot fallback and store stay one file.
    (Lazy import: ``core.themes`` imports this module's ``Theme``.)"""
    if path is not None:
        theme_path = Path(path)
    else:
        from rosemary.core.themes import THEMES_DIR

        theme_path = THEMES_DIR / "default.yaml"
    colors: dict[str, str] = {}
    emojis: dict[str, str] = {}
    markdown: dict[str, str] = {}
    styles: dict[str, dict[str, str]] = {}
    bump: dict[str, str] = {}
    medals: dict[str, str] = {}
    star_tier_emoji: dict[Any, str] = {}
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
            star_tier_emoji = dict((data.get("star_tier_emoji") or {}).items())
        except yaml.YAMLError as exc:
            log.error("Failed to load theme %s: %s", theme_path, exc)
    else:
        log.warning("Theme file not found: %s", theme_path)
    _validate_theme(theme_path, colors, emojis)
    return Theme(colors, emojis, markdown, styles, bump, medals, star_tier_emoji)


def _validate_theme(path: Path, colors: dict[str, str], emojis: dict[str, str]) -> None:
    """Warn about theme problems at boot instead of failing later in sends.

    Shared emoji values (``loading``/``hourglass``, ``swap``/``refresh``) are
    intentional aliases so each surface stays independently customizable.
    """
    import re

    for name, value in colors.items():
        if not re.fullmatch(r"#?[0-9a-fA-F]{6}", str(value)):
            log.warning("Theme %s has invalid color %r=%r (expected hex RRGGBB)", path, name, value)
