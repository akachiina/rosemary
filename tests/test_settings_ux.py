"""The settings UX of P7.6: the picker, the search and the reset.

The settings menu is the biggest interactive surface of the bot. These tests
pin the three behaviours the member relies on:

* the category picker names every category with its icon, a short hint and the
  number of settings, and marks the category currently shown;
* the search finds a setting of any category (accent- and case-insensitive)
  and opens its edit screen;
* restoring the default removes the stored key, so the localized default keeps
  winning (never the English literal baked into the spec).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
from golden_panels import make_bot

from rosemary.core.settings import SETTINGS, SettingCategory, get_setting, set_setting
from rosemary.ui.settings_menu import SettingsMenuView


def _walk(node: Any):
    if isinstance(node, (list, tuple)):
        for child in node:
            yield from _walk(child)
        return
    yield node
    nested = list(getattr(node, "children", []) or [])
    for child in nested:
        yield from _walk(child)
    accessory = getattr(node, "accessory", None)
    if accessory is not None:
        yield from _walk(accessory)


def _select(view: SettingsMenuView, custom_id: str) -> discord.ui.Select | None:
    for node in _walk(list(view.children)):
        if isinstance(node, discord.ui.Select) and node.custom_id == custom_id:
            return node
    return None


async def test_the_category_picker_names_and_marks_the_current_category():
    bot = make_bot()
    view = SettingsMenuView(bot, 1, owner_id=1)
    await view.prepare()

    select = _select(view, "settings_category")
    assert select is not None
    assert all(option.emoji for option in select.options)
    assert all(option.description for option in select.options)
    assert all("defini" in (option.description or "") for option in select.options)
    assert [option.value for option in select.options if option.default] == ["general"]

    view.category = SettingCategory.LOGGING
    await view.prepare()
    select = _select(view, "settings_category")
    assert select is not None
    assert [option.value for option in select.options if option.default] == ["logging"]


async def test_search_finds_a_setting_and_opens_its_edit_screen():
    bot = make_bot()
    view = SettingsMenuView(bot, 1, owner_id=1)

    # Case- and accent-insensitive, across categories.
    view.query = "MODERACAO"
    assert "moderation.enabled" in await view._search_results()
    view.query = "canal de log"
    assert "logging.channel" in await view._search_results()

    view.rerender = AsyncMock()
    interaction = SimpleNamespace(data={"values": ["logging.channel"]})
    await view._pick_search_result(interaction)
    assert view.editing_key == "logging.channel"

    await view.prepare()
    assert view._screen == "edit"
    select = _select(view, "settings_set:logging.channel")
    assert select is not None
    assert select.placeholder == "Canal de log"


async def test_reset_removes_the_key_and_restores_the_localized_default():
    bot = make_bot()
    key = "bump.schedule.open_message"
    spec = SETTINGS[key]
    assert spec.default_catalog_key

    await set_setting(bot.storage, 1, key, "Custom message")
    view = SettingsMenuView(bot, 1, owner_id=1)
    view._ack = AsyncMock(return_value=True)
    view.rerender = AsyncMock()
    interaction = SimpleNamespace(
        custom_id=f"settings_reset:{key}",
        user=SimpleNamespace(mention="@you"),
    )

    await view._reset_setting(interaction)

    assert key not in await bot.storage.get(1)
    # The core layer falls back to the spec default; the menu resolves the
    # catalog copy, so a pt-BR guild never sees the English literal.
    assert await get_setting(bot.storage, 1, key) == spec.default
    value, stored = await view._setting_value(spec)
    assert stored is False
    assert value != spec.default
    assert "canal de bump" in value
    assert view.editing_key is None
    assert view.flash_color == "success"
    assert "voltou ao padrão" in view.flash


async def test_reveal_and_seal_are_exclusive():
    """The reveal reads the id, the seal hides it: saving one turns the
    other off, so the member never gets both promises."""
    bot = make_bot()
    await set_setting(bot.storage, 1, "confessions.reveal_author", True)
    view = SettingsMenuView(bot, 1, owner_id=1)
    view._ack = AsyncMock(return_value=True)
    view.rerender = AsyncMock()
    interaction = SimpleNamespace(
        custom_id="settings_value:confessions.reveal_author:true",
        user=SimpleNamespace(mention="@you"),
        response=SimpleNamespace(is_done=lambda: True),
    )

    await view._apply_value(
        interaction, "confessions.reveal_author", True, stay_in_edit=True
    )

    assert await get_setting(bot.storage, 1, "confessions.reveal_author") is True
    assert await get_setting(bot.storage, 1, "confessions.sealed_author") is False
