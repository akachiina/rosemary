"""Build the settings menu with fakes to catch V2 construction regressions."""

from __future__ import annotations

import discord

import rosemary.core.card_specs  # noqa: F401  (populates the customize button)
from rosemary.core.settings import (
    SettingCategory,
    get_setting,
    settings_for_category,
)
from rosemary.core.storage import GuildStorage
from rosemary.ui.settings_menu import SettingsMenuView
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


class GuardedTranslator(FakeTranslator):
    """Fails if t() ever resolves a list-valued time_parser key."""

    async def t(self, guild_id, key, **variables):
        if key.startswith("time_parser."):
            raise AssertionError(f"t() resolved list key {key!r} for a non-duration setting")
        return key


class FakeBot:
    theme = load_theme()
    translator = FakeTranslator()

    def __init__(self, storage) -> None:
        self.storage = storage
        self.guilds = []

    def get_cog(self, name):
        return None


async def test_settings_menu_home_builds(tmp_path):
    bot = FakeBot(GuildStorage(tmp_path))
    view = SettingsMenuView(bot, 1, owner_id=1)
    await view.prepare()
    assert view.children


async def test_settings_menu_every_category_builds(tmp_path):
    bot = FakeBot(GuildStorage(tmp_path))
    for category in SettingCategory:
        view = SettingsMenuView(bot, 1, owner_id=1)
        view.category = category
        await view.prepare()
        assert view.children


async def test_settings_menu_bump_main_page_has_single_test_entry(tmp_path):
    bot = FakeBot(GuildStorage(tmp_path))
    view = SettingsMenuView(bot, 1, owner_id=1)
    view.category = SettingCategory.BUMP
    await view.prepare()

    custom_ids = {
        item.custom_id
        for row in view.children
        if isinstance(row, discord.ui.ActionRow)
        for item in row.children
    }
    assert "settings_test_open" in custom_ids
    assert not any(cid.startswith("settings_test:") for cid in custom_ids)


async def test_settings_menu_bump_test_screen_shows_all_actions(tmp_path):
    bot = FakeBot(GuildStorage(tmp_path))
    view = SettingsMenuView(bot, 1, owner_id=1)
    view.category = SettingCategory.BUMP
    view.test_open = True
    await view.prepare()

    custom_ids = {
        item.custom_id
        for row in view.children
        if isinstance(row, discord.ui.ActionRow)
        for item in row.children
    }
    assert {
        "settings_test:thank_you",
        "settings_test:reminder",
        "settings_test:leaderboard",
        "settings_test:lock",
        "settings_test:unlock",
        "settings_test:reset",
        "settings_test_close",
    } <= custom_ids


async def test_settings_menu_non_bump_has_no_test_buttons(tmp_path):
    bot = FakeBot(GuildStorage(tmp_path))
    view = SettingsMenuView(bot, 1, owner_id=1)
    view.category = SettingCategory.GENERAL
    await view.prepare()

    custom_ids = {
        item.custom_id
        for row in view.children
        if isinstance(row, discord.ui.ActionRow)
        for item in row.children
    }
    assert not any(cid.startswith("settings_test:") for cid in custom_ids)


async def test_settings_menu_bump_category_shows_page_indicator(tmp_path):
    bot = FakeBot(GuildStorage(tmp_path))
    view = SettingsMenuView(bot, 1, owner_id=1)
    view.category = SettingCategory.BUMP
    await view.prepare()

    rendered = "\n".join(
        item.content
        for row in view.children
        if isinstance(row, discord.ui.Container)
        for item in row.items
    )
    assert "settings.page_indicator" in rendered


async def test_settings_menu_single_page_category_hides_page_indicator(tmp_path):
    bot = FakeBot(GuildStorage(tmp_path))
    view = SettingsMenuView(bot, 1, owner_id=1)
    view.category = SettingCategory.LOGGING  # single page: indicator must hide
    await view.prepare()

    rendered = "\n".join(
        item.content
        for row in view.children
        if isinstance(row, discord.ui.Container)
        for item in row.items
    )
    assert "settings.page_indicator" not in rendered


async def test_non_duration_setting_skips_localized_aliases(tmp_path):
    """general.timezone (a plain string) must not resolve duration aliases."""
    bot = FakeBot(GuildStorage(tmp_path))
    bot.translator = GuardedTranslator()
    view = SettingsMenuView(bot, 1, owner_id=1)

    class FakeUser:
        mention = "<@1>"

    class FakeInteraction:
        user = FakeUser()

        async def edit(self, **kwargs):
            self.edited = True

    await view._apply_value(FakeInteraction(), "general.timezone", "UTC-3")
    assert await get_setting(bot.storage, 1, "general.timezone") == "UTC-3"


# -- Discord 40-component limit regression (error 50035) -----------------------


def _count_components(view) -> int:
    """Total components exactly as Discord counts them (nested included)."""

    def walk(d: dict) -> int:
        n = 1
        for child in d.get("components") or []:
            n += walk(child)
        accessory = d.get("accessory")
        if isinstance(accessory, dict):
            n += walk(accessory)
        return n

    return sum(walk(item.to_component_dict()) for item in view.children)


async def test_every_category_page_stays_under_discord_limit(tmp_path):
    from rosemary.ui.settings_menu import ITEMS_PER_PAGE

    bot = FakeBot(GuildStorage(tmp_path))
    for category in SettingCategory:
        per_page = ITEMS_PER_PAGE
        pages = max(1, (len(settings_for_category(category)) + per_page - 1) // per_page)
        for page in range(pages):
            for flash in (False, True):
                view = SettingsMenuView(bot, 1, owner_id=1)
                view.category = category
                view.page = page
                if flash:
                    view.flash = "settings.test.thank_you_done"
                await view.prepare()
                total = _count_components(view)
                assert total <= 38, (
                    f"{category.value} page {page} flash={flash}: {total} components"
                )


async def test_bump_test_screen_also_stays_under_limit(tmp_path):
    bot = FakeBot(GuildStorage(tmp_path))
    view = SettingsMenuView(bot, 1, owner_id=1)
    view.category = SettingCategory.BUMP
    view.test_open = True
    await view.prepare()
    assert _count_components(view) <= 38
