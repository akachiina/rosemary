"""Per-guild settings registry and service for Rosemary.

The registry is declarative: ``settings/*.yaml`` holds every editable value
(one file per category) and :mod:`rosemary.core.settings_loader` reads it into
the specs this module serves. The labels and descriptions are not stored here:
they live in the language catalogs under ``settings.<key>.label`` and
``settings.<key>.description``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from rosemary.core.settings_loader import SettingsError, load_settings
from rosemary.core.settings_spec import (
    MENU_TIMEOUT,
    SettingCategory,
    SettingSpec,
    SettingType,
)
from rosemary.core.storage import GuildStorage
from rosemary.core.time_parser import TimeParser

log = logging.getLogger(__name__)

__all__ = [
    "CATEGORIES",
    "FormattedValue",
    "MENU_TIMEOUT",
    "SETTINGS",
    "SettingCategory",
    "SettingSpec",
    "SettingType",
    "SettingsError",
    "coerce_value",
    "format_value",
    "get_setting",
    "reset_setting",
    "set_setting",
    "settings_for_category",
]


#: Full settings registry, ordered by category (display order in the menu).
SETTINGS: dict[str, SettingSpec] = load_settings()

#: Display order of categories in the /settings menu.
CATEGORIES: tuple[SettingCategory, ...] = tuple(SettingCategory)


def settings_for_category(category: SettingCategory) -> list[SettingSpec]:
    """Return the settings belonging to a category, in registry order."""
    return [spec for spec in SETTINGS.values() if spec.category is category]


def coerce_value(
    spec: SettingSpec, raw: Any, aliases: dict[str, set[str]] | None = None
) -> tuple[bool, Any]:
    """Coerce a raw input into the setting's canonical value.

    Returns ``(ok, value)``. When ``ok`` is ``False`` the value cannot be
    represented by the spec; callers should reject it. ``aliases`` (from
    ``localized_aliases``) lets duration inputs accept the guild language.
    """
    value_type = spec.value_type

    if value_type is SettingType.STRING:
        value = str(raw).strip()
        return (True, value) if value else (False, None)

    if value_type is SettingType.INTEGER:
        if spec.is_duration and isinstance(raw, str):
            parsed = TimeParser.parse(raw, aliases=aliases)
            if parsed is not None:
                value = int(parsed.total_seconds())
            else:
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    return (False, None)
        else:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return (False, None)

        if spec.min_value is not None and value < spec.min_value:
            return (False, None)
        if spec.max_value is not None and value > spec.max_value:
            return (False, None)
        return (True, value)

    if value_type in (SettingType.BOOLEAN, SettingType.IMAGE):
        if isinstance(raw, bool):
            return (True, raw)
        return (False, None)

    if value_type is SettingType.CHOICE:
        if raw in spec.choices:
            return (True, str(raw))
        return (False, None)

    if value_type in (SettingType.CHANNEL, SettingType.CATEGORY, SettingType.ROLE):
        if raw is None or raw == "" or raw == 0:
            return (True, None)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return (False, None)
        return (True, value if value > 0 else None)

    if value_type is SettingType.CHANNEL_LIST:
        # Stored as a tuple of positive channel/category ids. Accepts a raw
        # list (multi-select values), a single id (migration from the old
        # single-channel setting) and an empty/None marker to clear.
        if raw is None or raw == "" or raw == 0:
            return (True, ())
        raw_items = raw if isinstance(raw, (list, tuple)) else [raw]
        values: list[int] = []
        for item in raw_items:
            try:
                item_value = int(item)
            except (TypeError, ValueError):
                return (False, None)
            if item_value <= 0:
                return (False, None)
            values.append(item_value)
        # Deduplicated, order-preserving (repeat picks are a no-op).
        return (True, tuple(dict.fromkeys(values)))

    log.warning("Unhandled setting type %r for %s", value_type, spec.key)
    return (False, None)


@dataclass(frozen=True)
class FormattedValue:
    """A value rendered for display in the settings menu."""

    display: str
    translate: bool
    is_default: bool
    #: True when the display shows an escaped/truncated excerpt of a longer
    #: template instead of the whole value (callers may label it a preview).
    preview: bool = False


def format_value(spec: SettingSpec, value: Any) -> FormattedValue:
    """Render a canonical value for display.

    ``translate=True`` means ``display`` is an i18n key that the caller must
    translate; otherwise ``display`` is already a literal string.
    """
    is_default = value == spec.default
    value_type = spec.value_type

    if value_type in (SettingType.BOOLEAN, SettingType.IMAGE):
        return FormattedValue(
            "settings.bool.true" if value else "settings.bool.false",
            translate=True,
            is_default=is_default,
        )

    if value_type in (SettingType.CHANNEL, SettingType.CATEGORY):
        # Note: <#id> does not mention-render for categories, but the id is
        # still the unambiguous display (names collide, ids don't).
        if value is None:
            return FormattedValue("settings.none", translate=True, is_default=is_default)
        return FormattedValue(f"<#{value}>", translate=False, is_default=is_default)

    if value_type is SettingType.CHANNEL_LIST:
        items = list(value or ())
        if not items:
            return FormattedValue("settings.none", translate=True, is_default=is_default)
        return FormattedValue(
            " ".join(f"<#{item}>" for item in items),
            translate=False,
            is_default=is_default,
        )

    if value_type is SettingType.ROLE:
        if value is None:
            return FormattedValue("settings.none", translate=True, is_default=is_default)
        return FormattedValue(f"<@&{value}>", translate=False, is_default=is_default)

    if value_type is SettingType.CHOICE:
        return FormattedValue(
            f"settings.{spec.key}.choices.{value}",
            translate=True,
            is_default=is_default,
        )

    if spec.is_duration:
        return FormattedValue(
            TimeParser.format_duration(timedelta(seconds=int(value))),
            translate=False,
            is_default=is_default,
        )

    text = str(value)
    if spec.multiline:
        # Message templates render as one huge markdown blob inside the menu
        # entry (a leading "#" becomes a heading, blank lines spread the
        # panel). Show a flat single-line preview instead: markdown escaped,
        # newlines collapsed, truncated. The edit modal still carries the
        # full text, so nothing is lost.
        escaped = text
        for char in ("\\", "`", "*", "_", "#", ">", "~", "|"):
            escaped = escaped.replace(char, f"\\{char}")
        escaped = " ".join(escaped.split())
        if len(escaped) > 200:
            escaped = escaped[:197].rstrip() + "…"
        return FormattedValue(escaped, translate=False, is_default=is_default, preview=True)

    return FormattedValue(str(value), translate=False, is_default=is_default)


#: Renamed setting keys and the legacy key each reads through (lazy
#: migration: guilds that saved the old key keep its value until the next
#: menu save persists it canonically; the old key is never written again).
_RENAMED_KEYS: dict[str, str] = {
    "roleplay.enabled": "interactions.enabled",
}


async def get_setting(storage: GuildStorage, guild_id: int, key: str) -> Any:
    """Return the effective value for a setting, falling back to its default."""
    spec = SETTINGS[key]
    data = await storage.get(guild_id)
    if key not in data and key in _RENAMED_KEYS and _RENAMED_KEYS[key] in data:
        data = {**data, key: data[_RENAMED_KEYS[key]]}
    ok, value = coerce_value(spec, data.get(key, spec.default))
    if ok:
        if spec.value_type is SettingType.CHANNEL_LIST:
            value = await _migrated_list_value(storage, guild_id, spec, data, value)
        return value
    return spec.default


async def _migrated_list_value(
    storage: GuildStorage,
    guild_id: int,
    spec: SettingSpec,
    data: dict,
    value: tuple[int, ...],
) -> tuple[int, ...]:
    """One-shot migration for list settings renamed from a scalar setting.

    ``anti_invite.exempt_channel`` (single int) became
    ``anti_invite.exempt_places`` (list). When the new key was never written
    and the old one holds a value, that value seeds the list (lazy migration:
    read path only, no write: the next menu save persists it canonically).
    """
    legacy = getattr(spec, "legacy_single_key", None)
    if value or legacy is None:
        return value
    # Migrate only while the new key was never written; the legacy key
    # holding a value is precisely the case that seeds the list.
    if legacy not in data:
        return value
    ok, migrated = coerce_value(spec, data.get(legacy))
    return migrated if ok else value


async def set_setting(
    storage: GuildStorage,
    guild_id: int,
    key: str,
    value: Any,
    aliases: dict[str, set[str]] | None = None,
) -> Any:
    """Coerce and persist a new value for a setting.

    Raises ``ValueError`` when the value cannot be coerced. Returns the stored
    canonical value. ``aliases`` is forwarded to duration coercion.
    """
    spec = SETTINGS[key]
    ok, coerced = coerce_value(spec, value, aliases=aliases)
    if not ok:
        raise ValueError(f"Invalid value {value!r} for setting {key!r}")
    await storage.set(guild_id, key, coerced)
    return coerced


async def reset_setting(storage: GuildStorage, guild_id: int, key: str) -> None:
    """Restore a setting to its registry default by removing the stored value.

    Removing the key (never writing ``spec.default``) keeps the localized
    default lazy: a pt-BR guild whose setting carries ``default_catalog_key``
    falls back to the catalog copy, not to the English literal baked into the
    spec. The legacy key a rename (or a channel-list migration) still reads
    through is removed too, otherwise the reset would not reset.
    """
    spec = SETTINGS[key]
    removed = [key]
    legacy_rename = _RENAMED_KEYS.get(key)
    if legacy_rename:
        removed.append(legacy_rename)
    legacy_scalar = getattr(spec, "legacy_single_key", None)
    if legacy_scalar:
        removed.append(legacy_scalar)
    await storage.delete_keys(guild_id, *removed)
