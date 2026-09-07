"""Tests for ``rosemary.core.settings`` (registry, coercion, persistence)."""

from __future__ import annotations

import pytest

from rosemary.core.settings import (
    SETTINGS,
    SettingCategory,
    SettingType,
    coerce_value,
    format_value,
    get_setting,
    set_setting,
    settings_for_category,
)
from rosemary.core.storage import GuildStorage


async def test_registry_covers_all_types_and_categories():
    types = {s.value_type for s in SETTINGS.values()}
    assert types == {
        SettingType.STRING,
        SettingType.INTEGER,
        SettingType.BOOLEAN,
        SettingType.CHANNEL,
        SettingType.CATEGORY,
        SettingType.CHOICE,
        SettingType.ROLE,
    }
    categories = {s.category for s in SETTINGS.values()}
    assert categories == set(SettingCategory)


async def test_coerce_integer():
    spec = SETTINGS["moderation.warn_limit"]
    assert coerce_value(spec, "3") == (True, 3)
    assert coerce_value(spec, 5) == (True, 5)
    assert coerce_value(spec, "0") == (False, None)
    assert coerce_value(spec, "99") == (False, None)
    assert coerce_value(spec, "abc") == (False, None)


async def test_coerce_duration_accepts_localized_words():
    spec = SETTINGS["bump.cooldown"]
    pt = {
        "s": {"s", "sec", "segundo", "segundos"},
        "m": {"m", "min", "minuto", "minutos"},
        "h": {"h", "hora", "horas"},
        "d": {"d", "dia", "dias"},
    }
    assert coerce_value(spec, "2h") == (True, 7200)
    assert coerce_value(spec, "2 horas", aliases=pt) == (True, 7200)
    assert coerce_value(spec, "90 min", aliases=pt) == (True, 5400)
    assert coerce_value(spec, "2 hours") == (False, None)


async def test_bump_detection_text_setting_default():
    spec = SETTINGS["bump.detection_text"]
    assert spec.category is SettingCategory.BUMP
    assert spec.value_type is SettingType.STRING
    assert spec.default == "Bump done"
    assert coerce_value(spec, "Bump feito") == (True, "Bump feito")
    assert coerce_value(spec, " ") == (False, None)


async def test_coerce_boolean_accepts_only_bool():
    spec = SETTINGS["moderation.enabled"]
    assert coerce_value(spec, True) == (True, True)
    assert coerce_value(spec, False) == (True, False)
    assert coerce_value(spec, "true") == (False, None)
    assert coerce_value(spec, "1") == (False, None)
    assert coerce_value(spec, "on") == (False, None)
    assert coerce_value(spec, 1) == (False, None)


async def test_coerce_string():
    spec = SETTINGS["general.timezone"]
    assert coerce_value(spec, "  UTC  ") == (True, "UTC")
    assert coerce_value(spec, "") == (False, None)


async def test_coerce_channel():
    spec = SETTINGS["logging.channel"]
    assert coerce_value(spec, None) == (True, None)
    assert coerce_value(spec, "123") == (True, 123)
    assert coerce_value(spec, "0") == (True, None)
    assert coerce_value(spec, "abc") == (False, None)


async def test_category_setting_coerces_like_channel():
    spec = SETTINGS["tickets.category"]
    assert spec.value_type is SettingType.CATEGORY
    assert coerce_value(spec, None) == (True, None)
    assert coerce_value(spec, "123") == (True, 123)
    assert coerce_value(spec, "abc") == (False, None)
    formatted = format_value(spec, 456)
    assert formatted.display == "<#456>"
    assert formatted.translate is False


async def test_format_value():
    boolean = format_value(SETTINGS["moderation.enabled"], True)
    assert boolean.display == "settings.bool.true"
    assert boolean.translate is True
    assert boolean.is_default is True

    off = format_value(SETTINGS["moderation.enabled"], False)
    assert off.display == "settings.bool.false"
    assert off.is_default is False

    channel = format_value(SETTINGS["logging.channel"], 456)
    assert channel.display == "<#456>"
    assert channel.translate is False

    unset = format_value(SETTINGS["logging.channel"], None)
    assert unset.display == "settings.none"
    assert unset.translate is True

    integer = format_value(SETTINGS["moderation.mute_seconds"], 3600)
    assert integer.display == "1h"
    assert integer.translate is False
    assert integer.is_default is True

    duration = format_value(SETTINGS["moderation.mute_seconds"], 90)
    assert duration.display == "1m"
    assert duration.translate is False
    assert duration.is_default is False

    timeout = format_value(SETTINGS["general.menu_timeout"], 900)
    assert timeout.display == "15m"
    assert timeout.is_default is True


async def test_get_and_set_setting(tmp_path):
    store = GuildStorage(tmp_path)
    assert await get_setting(store, 1, "moderation.warn_limit") == 3
    assert await set_setting(store, 1, "moderation.warn_limit", "5") == 5
    assert await get_setting(store, 1, "moderation.warn_limit") == 5

    with pytest.raises(ValueError):
        await set_setting(store, 1, "moderation.warn_limit", "0")


async def test_set_setting_duration_with_localized_aliases(tmp_path):
    store = GuildStorage(tmp_path)
    pt = {
        "s": {"s", "sec", "segundo", "segundos"},
        "m": {"m", "min", "minuto", "minutos"},
        "h": {"h", "hora", "horas"},
        "d": {"d", "dia", "dias"},
    }
    assert await set_setting(store, 1, "bump.cooldown", "2 horas", aliases=pt) == 7200
    assert await get_setting(store, 1, "bump.cooldown") == 7200

    assert await set_setting(store, 1, "bump.detection_text", "Bump feito") == "Bump feito"
    assert await get_setting(store, 1, "bump.detection_text") == "Bump feito"


async def test_get_setting_falls_back_to_default_on_bad_stored_value(tmp_path):
    store = GuildStorage(tmp_path)
    await store.set(1, "moderation.enabled", "nope")
    assert await get_setting(store, 1, "moderation.enabled") is True


async def test_settings_for_category_ordering():
    keys = [s.key for s in settings_for_category(SettingCategory.MODERATION)]
    assert keys == [
        "moderation.enabled",
        "moderation.warn_limit",
        "moderation.auto_ban_enabled",
        "cleaner.enabled",
        "anti_invite.enabled",
        "anti_invite.warn_on_delete",
        "anti_invite.exempt_channel",
        "moderation.mute_seconds",
    ]


async def test_bump_essentials_come_first():
    """Most-used bump settings (incl. the schedule toggle) fit on page one."""
    keys = [s.key for s in settings_for_category(SettingCategory.BUMP)]
    assert keys[:7] == [
        "bump.enabled",
        "bump.channel",
        "bump.ping_role",
        "bump.cooldown",
        "bump.schedule.enabled",
        "bump.schedule.open_time",
        "bump.schedule.close_time",
    ]


async def test_general_prefix_setting_exists():
    spec = SETTINGS["general.prefix"]
    assert spec.value_type == SettingType.STRING
    assert spec.category == SettingCategory.GENERAL
    assert spec.default == "!"
