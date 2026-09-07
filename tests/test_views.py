"""Build each interactive view with fakes to catch V2 construction regressions."""

from __future__ import annotations

import discord
import pytest

from rosemary.cogs.debug import DebugMenuView
from rosemary.cogs.moderation import ClearWarningsView, ModerationConfirmView
from rosemary.core.settings import SETTINGS, SettingCategory
from rosemary.core.storage import GuildStorage
from rosemary.ui.menu import MENU_TIMEOUT
from rosemary.ui.settings_menu import SettingsMenuView
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


class FakeResponse:
    def __init__(self) -> None:
        self.sent = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content, ephemeral=False):
        self.sent.append((content, ephemeral))
        self._done = True


class FakeInteraction:
    def __init__(self, response) -> None:
        self.response = response


@pytest.fixture
def bot(tmp_path):
    class Bot:
        theme = load_theme()
        translator = FakeTranslator()
        storage = GuildStorage(tmp_path)
        guilds = []

    return Bot()


async def test_settings_menu_main_pages(bot):
    view = SettingsMenuView(bot, 1, owner_id=1)
    for category in SettingCategory:
        view.category = category
        await view.prepare()
        assert view.children


async def test_settings_menu_edit_pages(bot):
    view = SettingsMenuView(bot, 1, owner_id=1)
    for key in SETTINGS:
        view.editing_key = key
        await view.prepare()
        assert view.children


async def test_confirm_and_clear_views(bot):
    async def on_confirm(notify):
        return "done"

    confirm = ModerationConfirmView(
        bot, guild_id=1, owner_id=1, action="ban", summary="test", on_confirm=on_confirm
    )
    await confirm.prepare()
    assert confirm.children

    async def clear_all():
        return 0

    clear = ClearWarningsView(
        bot, guild_id=1, owner_id=1, member_mention="<@1>", on_confirm=clear_all
    )
    await clear.prepare()
    assert clear.children


async def test_debug_menu_view(bot):
    view = DebugMenuView(bot, 1, owner_id=1)
    await view.prepare()
    assert view.children


async def test_menu_views_use_idle_timeout_and_disable_on_timeout(bot):
    views = [
        SettingsMenuView(bot, 1, owner_id=1),
        DebugMenuView(bot, 1, owner_id=1),
        ModerationConfirmView(
            bot, guild_id=1, owner_id=1, action="ban", summary="s", on_confirm=None
        ),
        ClearWarningsView(
            bot, guild_id=1, owner_id=1, member_mention="<@1>", on_confirm=None
        ),
    ]
    for view in views:
        assert view.timeout == MENU_TIMEOUT
        assert view.disable_on_timeout is True


async def test_on_check_failure_responds_ephemeral(bot):
    view = SettingsMenuView(bot, 1, owner_id=1)
    response = FakeResponse()
    await view.on_check_failure(FakeInteraction(response))
    assert response.sent == [("menu.owner_only", True)]


async def test_prepare_applies_configured_menu_timeout(bot):
    view = SettingsMenuView(bot, 1, owner_id=1)
    assert view.timeout == MENU_TIMEOUT
    await bot.storage.set(1, "general.menu_timeout", 300)
    await view.prepare()
    assert view.timeout == 300


async def test_modal_fields_wrapped_in_label():
    async def run():
        modal = discord.ui.DesignerModal(title="Edit", custom_id="settings_modal:x")
        field = discord.ui.InputText(
            value="", required=True, custom_id="x_value", min_length=1, max_length=100
        )
        modal.add_item(discord.ui.Label("My Label", item=field))
        return modal, field

    modal, field = await run()

    parent_payload = {"type": 6, "component": {"type": 4, "custom_id": "x_value", "value": "42"}}

    class FakeInteraction:
        data = {"components": []}
        type = 9

    modal._refresh(FakeInteraction(), [parent_payload])
    assert field.value == "42"
