"""Per-guild settings registry and service for Rosemary.

The registry in this module is the single source of truth for the /settings
menu: every editable value has a SettingSpec describing its category, type and
constraints. Values are persisted flat in the guild settings JSON under their
dotted key (e.g. ``moderation.warn_limit``).

Labels and descriptions are not stored here; they live in the language
catalogs under ``settings.<key>.label`` and ``settings.<key>.description``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from rosemary.core.storage import GuildStorage
from rosemary.core.time_parser import TimeParser

log = logging.getLogger(__name__)

#: Default idle timeout (seconds) for interactive menus. Discord rejects
#: message-component interactions after 15 minutes, so 900 is the hard cap.
MENU_TIMEOUT = 900


class SettingType(StrEnum):
    """The value kind of a setting, which drives coercion and editing UI."""

    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    CHOICE = "choice"
    CHANNEL = "channel"
    ROLE = "role"


class SettingCategory(StrEnum):
    """Menu category a setting belongs to."""

    GENERAL = "general"
    MODERATION = "moderation"
    LOGGING = "logging"
    EVENTS = "events"
    BOOST = "boost"
    BUMP = "bump"
    STARBOARD = "starboard"
    BIRTHDAYS = "birthdays"
    INVITES = "invites"
    TICKETS = "tickets"
    PARTNERSHIPS = "partnerships"


@dataclass(frozen=True)
class SettingSpec:
    """Describes one editable setting."""

    key: str
    category: SettingCategory
    value_type: SettingType
    default: Any
    min_value: int | None = None
    max_value: int | None = None
    choices: tuple[str, ...] = ()
    is_duration: bool = False


def _spec(
    key: str,
    category: SettingCategory,
    value_type: SettingType,
    default: Any,
    **extra: Any,
) -> SettingSpec:
    return SettingSpec(key=key, category=category, value_type=value_type, default=default, **extra)


#: Full settings registry, ordered by category (display order in the menu).
SETTINGS: dict[str, SettingSpec] = {
    spec.key: spec
    for spec in [
        _spec(
            "general.timezone",
            SettingCategory.GENERAL,
            SettingType.STRING,
            "UTC",
        ),
        _spec(
            "general.prefix",
            SettingCategory.GENERAL,
            SettingType.STRING,
            "!",
        ),
        _spec(
            "general.menu_timeout",
            SettingCategory.GENERAL,
            SettingType.INTEGER,
            MENU_TIMEOUT,
            min_value=60,
            max_value=MENU_TIMEOUT,
            is_duration=True,
        ),
        _spec(
            "moderation.enabled",
            SettingCategory.MODERATION,
            SettingType.BOOLEAN,
            True,
        ),
        _spec(
            "moderation.warn_limit",
            SettingCategory.MODERATION,
            SettingType.INTEGER,
            3,
            min_value=1,
            max_value=10,
        ),
        _spec(
            "moderation.auto_ban_enabled",
            SettingCategory.MODERATION,
            SettingType.BOOLEAN,
            False,
        ),
        _spec(
            "cleaner.enabled",
            SettingCategory.MODERATION,
            SettingType.BOOLEAN,
            True,
        ),
        _spec(
            "anti_invite.enabled",
            SettingCategory.MODERATION,
            SettingType.BOOLEAN,
            False,
        ),
        _spec(
            "anti_invite.warn_on_delete",
            SettingCategory.MODERATION,
            SettingType.BOOLEAN,
            True,
        ),
        _spec(
            "anti_invite.exempt_channel",
            SettingCategory.MODERATION,
            SettingType.CHANNEL,
            None,
        ),
        _spec(
            "reminders.enabled",
            SettingCategory.GENERAL,
            SettingType.BOOLEAN,
            False,
        ),
        _spec(
            "reminders.delay_seconds",
            SettingCategory.GENERAL,
            SettingType.INTEGER,
            2,
            min_value=0,
            max_value=30,
        ),
        _spec(
            "broadcast.enabled",
            SettingCategory.GENERAL,
            SettingType.BOOLEAN,
            False,
        ),
        _spec(
            "broadcast.delay_seconds",
            SettingCategory.GENERAL,
            SettingType.INTEGER,
            2,
            min_value=0,
            max_value=30,
        ),
        _spec(
            "tickets.enabled",
            SettingCategory.TICKETS,
            SettingType.BOOLEAN,
            False,
        ),
        _spec(
            "tickets.panel_channel",
            SettingCategory.TICKETS,
            SettingType.CHANNEL,
            None,
        ),
        _spec(
            "tickets.category",
            SettingCategory.TICKETS,
            SettingType.CHANNEL,
            None,
        ),
        _spec(
            "tickets.max_open_per_user",
            SettingCategory.TICKETS,
            SettingType.INTEGER,
            3,
            min_value=1,
            max_value=10,
        ),
        _spec(
            "partnerships.enabled",
            SettingCategory.PARTNERSHIPS,
            SettingType.BOOLEAN,
            False,
        ),
        _spec(
            "partnerships.channel",
            SettingCategory.PARTNERSHIPS,
            SettingType.CHANNEL,
            None,
        ),
        _spec(
            "partnerships.role",
            SettingCategory.PARTNERSHIPS,
            SettingType.ROLE,
            None,
        ),
        _spec(
            "partnerships.ping_role",
            SettingCategory.PARTNERSHIPS,
            SettingType.ROLE,
            None,
        ),
        _spec(
            "partnerships.renewal_days",
            SettingCategory.PARTNERSHIPS,
            SettingType.INTEGER,
            15,
            min_value=1,
            max_value=90,
        ),
        _spec(
            "partnerships.grace_days",
            SettingCategory.PARTNERSHIPS,
            SettingType.INTEGER,
            3,
            min_value=0,
            max_value=30,
        ),
        _spec(
            "updater.enabled",
            SettingCategory.GENERAL,
            SettingType.BOOLEAN,
            False,
        ),
        _spec(
            "updater.check_interval_hours",
            SettingCategory.GENERAL,
            SettingType.INTEGER,
            24,
            min_value=1,
            max_value=168,
        ),
        _spec(
            "updater.channel",
            SettingCategory.GENERAL,
            SettingType.CHOICE,
            "stable",
            choices=("stable", "git"),
        ),
        _spec(
            "updater.branch",
            SettingCategory.GENERAL,
            SettingType.STRING,
            "main",
        ),
        _spec(
            "moderation.mute_seconds",
            SettingCategory.MODERATION,
            SettingType.INTEGER,
            3600,
            min_value=60,
            max_value=604800,
            is_duration=True,
        ),
        _spec(
            "logging.enabled",
            SettingCategory.LOGGING,
            SettingType.BOOLEAN,
            False,
        ),
        _spec(
            "logging.channel",
            SettingCategory.LOGGING,
            SettingType.CHANNEL,
            None,
        ),
        _spec(
            "events.enabled",
            SettingCategory.EVENTS,
            SettingType.BOOLEAN,
            True,
        ),
        _spec(
            "events.welcome_channel",
            SettingCategory.EVENTS,
            SettingType.CHANNEL,
            None,
        ),
        _spec(
            "events.leave_channel",
            SettingCategory.EVENTS,
            SettingType.CHANNEL,
            None,
        ),
        _spec(
            "events.ban_channel",
            SettingCategory.EVENTS,
            SettingType.CHANNEL,
            None,
        ),
        _spec(
            "events.raid_protection",
            SettingCategory.EVENTS,
            SettingType.BOOLEAN,
            False,
        ),
        _spec(
            "starboard.enabled",
            SettingCategory.STARBOARD,
            SettingType.BOOLEAN,
            False,
        ),
        _spec(
            "starboard.channel",
            SettingCategory.STARBOARD,
            SettingType.CHANNEL,
            None,
        ),
        _spec(
            "starboard.threshold",
            SettingCategory.STARBOARD,
            SettingType.INTEGER,
            3,
            min_value=1,
            max_value=25,
        ),
        _spec(
            "starboard.self_star_counts",
            SettingCategory.STARBOARD,
            SettingType.BOOLEAN,
            False,
        ),
        _spec(
            "starboard.allow_bots",
            SettingCategory.STARBOARD,
            SettingType.BOOLEAN,
            False,
        ),
        _spec(
            "birthdays.enabled",
            SettingCategory.BIRTHDAYS,
            SettingType.BOOLEAN,
            True,
        ),
        _spec(
            "birthdays.channel",
            SettingCategory.BIRTHDAYS,
            SettingType.CHANNEL,
            None,
        ),
        _spec(
            "birthdays.role",
            SettingCategory.BIRTHDAYS,
            SettingType.ROLE,
            None,
        ),
        _spec(
            "birthdays.announce_time",
            SettingCategory.BIRTHDAYS,
            SettingType.STRING,
            "12:00",
        ),
        _spec(
            "birthdays.change_cooldown_days",
            SettingCategory.BIRTHDAYS,
            SettingType.INTEGER,
            15,
            min_value=0,
            max_value=365,
        ),
        _spec(
            "boost.enabled",
            SettingCategory.BOOST,
            SettingType.BOOLEAN,
            True,
        ),
        _spec(
            "boost.max_members",
            SettingCategory.BOOST,
            SettingType.INTEGER,
            10,
            min_value=1,
            max_value=25,
        ),
        _spec(
            "boost.invite_seconds",
            SettingCategory.BOOST,
            SettingType.INTEGER,
            86400,
            min_value=3600,
            max_value=604800,
            is_duration=True,
        ),
        _spec(
            "boost.max_pending_invites",
            SettingCategory.BOOST,
            SettingType.INTEGER,
            5,
            min_value=1,
            max_value=20,
        ),
        _spec(
            "bump.enabled",
            SettingCategory.BUMP,
            SettingType.BOOLEAN,
            True,
        ),
        _spec(
            "bump.channel",
            SettingCategory.BUMP,
            SettingType.CHANNEL,
            None,
        ),
        _spec(
            "bump.ping_role",
            SettingCategory.BUMP,
            SettingType.ROLE,
            None,
        ),
        _spec(
            "bump.cooldown",
            SettingCategory.BUMP,
            SettingType.INTEGER,
            7200,
            min_value=60,
            max_value=86400,
            is_duration=True,
        ),
        _spec(
            "bump.anti_camping.enabled",
            SettingCategory.BUMP,
            SettingType.BOOLEAN,
            True,
        ),
        _spec(
            "bump.anti_camping.lock_delay",
            SettingCategory.BUMP,
            SettingType.INTEGER,
            900,
            min_value=60,
            max_value=3600,
            is_duration=True,
        ),
        _spec(
            "bump.anti_camping.unlock_delay",
            SettingCategory.BUMP,
            SettingType.INTEGER,
            15,
            min_value=0,
            max_value=120,
            is_duration=True,
        ),
        _spec(
            "bump.anti_camping.message",
            SettingCategory.BUMP,
            SettingType.STRING,
            "🔒 O canal foi bloqueado temporariamente. O bump estará disponível em instantes!",
        ),
        _spec(
            "bump.detection_text",
            SettingCategory.BUMP,
            SettingType.STRING,
            "Bump done",
        ),
        _spec(
            "bump.leaderboard.enabled",
            SettingCategory.BUMP,
            SettingType.BOOLEAN,
            True,
        ),
        _spec(
            "bump.leaderboard.winner_role",
            SettingCategory.BUMP,
            SettingType.ROLE,
            None,
        ),
        _spec(
            "bump.leaderboard.reset_day",
            SettingCategory.BUMP,
            SettingType.CHOICE,
            "Sunday",
            choices=("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
        ),
        _spec(
            "bump.leaderboard.reset_hour",
            SettingCategory.BUMP,
            SettingType.INTEGER,
            20,
            min_value=0,
            max_value=23,
        ),
        _spec(
            "invites.enabled",
            SettingCategory.INVITES,
            SettingType.BOOLEAN,
            True,
        ),
        _spec(
            "invites.show_inviter",
            SettingCategory.INVITES,
            SettingType.BOOLEAN,
            True,
        ),
        _spec(
            "invites.track_leaves",
            SettingCategory.INVITES,
            SettingType.BOOLEAN,
            True,
        ),
        _spec(
            "invites.count_rejoins",
            SettingCategory.INVITES,
            SettingType.BOOLEAN,
            False,
        ),
        _spec(
            "invites.fake_delay_days",
            SettingCategory.INVITES,
            SettingType.INTEGER,
            0,
            min_value=0,
            max_value=30,
        ),
        _spec(
            "invites.anti_cheat_days",
            SettingCategory.INVITES,
            SettingType.INTEGER,
            0,
            min_value=0,
            max_value=90,
        ),
        _spec(
            "invites.leaderboard_enabled",
            SettingCategory.INVITES,
            SettingType.BOOLEAN,
            True,
        ),
        _spec(
            "invites.leaderboard_size",
            SettingCategory.INVITES,
            SettingType.INTEGER,
            10,
            min_value=5,
            max_value=25,
        ),
    ]
}

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

    if value_type is SettingType.BOOLEAN:
        if isinstance(raw, bool):
            return (True, raw)
        return (False, None)

    if value_type is SettingType.CHOICE:
        if raw in spec.choices:
            return (True, str(raw))
        return (False, None)

    if value_type in (SettingType.CHANNEL, SettingType.ROLE):
        if raw is None or raw == "" or raw == 0:
            return (True, None)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return (False, None)
        return (True, value if value > 0 else None)

    log.warning("Unhandled setting type %r for %s", value_type, spec.key)
    return (False, None)


@dataclass(frozen=True)
class FormattedValue:
    """A value rendered for display in the settings menu."""

    display: str
    translate: bool
    is_default: bool


def format_value(spec: SettingSpec, value: Any) -> FormattedValue:
    """Render a canonical value for display.

    ``translate=True`` means ``display`` is an i18n key that the caller must
    translate; otherwise ``display`` is already a literal string.
    """
    is_default = value == spec.default
    value_type = spec.value_type

    if value_type is SettingType.BOOLEAN:
        return FormattedValue(
            "settings.bool.true" if value else "settings.bool.false",
            translate=True,
            is_default=is_default,
        )

    if value_type is SettingType.CHANNEL:
        if value is None:
            return FormattedValue("settings.none", translate=True, is_default=is_default)
        return FormattedValue(f"<#{value}>", translate=False, is_default=is_default)

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

    return FormattedValue(str(value), translate=False, is_default=is_default)


async def get_setting(storage: GuildStorage, guild_id: int, key: str) -> Any:
    """Return the effective value for a setting, falling back to its default."""
    spec = SETTINGS[key]
    data = await storage.get(guild_id)
    ok, value = coerce_value(spec, data.get(key, spec.default))
    return value if ok else spec.default


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
