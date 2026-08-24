"""Parsing and formatting of mute durations (language-neutral abbreviations).

Shared by the moderation cog (``/mute`` durations) and the settings menu
(displaying ``moderation.mute_seconds`` as a human duration instead of a raw
second count). Full-word unit aliases are language-localized and live in the
language catalogs under ``time_parser.*``; only universal abbreviations live
here.
"""

from __future__ import annotations

import re
from datetime import timedelta

#: Language-neutral abbreviation groups, accepted regardless of locale.
DEFAULT_ALIASES: dict[str, set[str]] = {
    "s": {"s", "sec"},
    "m": {"m", "min"},
    "h": {"h"},
    "d": {"d"},
}

#: Translation key per unit holding localized full-word aliases (lists).
_ALIAS_KEYS = (
    ("s", "time_parser.seconds"),
    ("m", "time_parser.minutes"),
    ("h", "time_parser.hours"),
    ("d", "time_parser.days"),
)


async def localized_aliases(translator, guild_id: int) -> dict[str, set[str]]:
    """Merge the neutral abbreviations with the guild language's full words.

    Reads the ``time_parser.*`` lists from the resolved catalog (English
    fallback applies automatically), so pt-BR accepts ``"2 horas"`` and en-US
    accepts ``"2 hours"`` in addition to the neutral abbreviations.
    """
    aliases = {unit: set(words) for unit, words in DEFAULT_ALIASES.items()}
    for unit, key in _ALIAS_KEYS:
        words = await translator.t(guild_id, key)
        if isinstance(words, list):
            aliases[unit] |= {str(word).strip().lower() for word in words if word}
    return aliases


class TimeParser:
    """Parse and format mute durations (language-neutral abbreviations)."""

    _MULTIPLIERS = {
        "s": timedelta(seconds=1),
        "m": timedelta(minutes=1),
        "h": timedelta(hours=1),
        "d": timedelta(days=1),
    }

    @classmethod
    def parse(
        cls, value: str, aliases: dict[str, set[str]] | None = None
    ) -> timedelta | None:
        """Parse ``"10s"``, ``"5m"``, ``"2h"``, ``"3d"`` into a timedelta.

        ``aliases`` maps a unit group to its accepted strings; when omitted the
        language-neutral abbreviations are used. Pass the result of
        ``localized_aliases()`` to also accept the guild language's full words.
        """
        units = aliases or DEFAULT_ALIASES
        token = "|".join(
            sorted((unit for group in units.values() for unit in group), key=len, reverse=True)
        )
        match = re.fullmatch(rf"(\d+)\s*({token})", value.strip().lower())
        if match is None:
            return None
        unit_name = next(
            name for name, group in units.items() if match.group(2) in group
        )
        return int(match.group(1)) * cls._MULTIPLIERS[unit_name]

    @staticmethod
    def format_duration(duration: timedelta) -> str:
        """Render a timedelta compactly: ``"1d 2h 30m"`` (never empty)."""
        total = int(duration.total_seconds())
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if seconds and not parts:
            parts.append(f"{seconds}s")
        return " ".join(parts) if parts else "0s"
