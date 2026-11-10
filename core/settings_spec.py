"""The model of a setting: its type, its category and its spec.

The registry itself lives in YAML (``settings/*.yaml``); this module holds
only the vocabulary: the kinds of value, the menu categories and the
:class:`SettingSpec` the loader builds. It is a separate module so the loader
can build the specs without importing the service (and the service without
importing the loader).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: Default idle timeout (seconds) for interactive menus. Discord rejects
#: message-component clicks stop working after 15 minutes, so 900 is the hard cap.
MENU_TIMEOUT = 900


class SettingType(StrEnum):
    """The value kind of a setting, which drives coercion and editing UI."""

    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    CHOICE = "choice"
    CHANNEL = "channel"
    CHANNEL_LIST = "channel_list"
    CATEGORY = "category"
    ROLE = "role"


class SettingCategory(StrEnum):
    """Menu category a setting belongs to."""

    GENERAL = "general"
    MODERATION = "moderation"
    LOGGING = "logging"
    EVENTS = "events"
    AUDIT = "audit"
    BOOST = "boost"
    BUMP = "bump"
    STARBOARD = "starboard"
    SUGGESTIONS = "suggestions"
    BIRTHDAYS = "birthdays"
    INVITES = "invites"
    TICKETS = "tickets"
    PARTNERSHIPS = "partnerships"
    BROADCAST_REMINDERS = "broadcast_reminders"
    COLORS = "colors"
    ROLEPLAY = "roleplay"
    CONFESSIONS = "confessions"


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
    #: STRING only: edit through a paragraph modal (multi-line, 2000 chars)
    #: instead of the 100-char single-line one. Message-template settings
    #: (e.g. partnership invite texts) need real newlines and links.
    multiline: bool = False
    #: STRING only: catalog key whose localized text is the effective default.
    #: Used by the settings UI so a pt-BR guild never sees the English literal
    #: baked into ``default`` before the setting is first saved.
    default_catalog_key: str | None = None
    #: CHANNEL_LIST only: a renamed-away scalar setting whose stored value
    #: seeds the list until the menu saves it canonically (see get_setting).
    legacy_single_key: str | None = None
