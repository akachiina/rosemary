"""Tests for ``rosemary.ui.menu`` (MenuView base)."""

from __future__ import annotations

import discord

from rosemary.ui.menu import MenuView


class _FakeMenu(MenuView):
    def __init__(self) -> None:
        super().__init__(author_id=42)
        self.clicks = 0
        self.register("btn", self._on_click)
        self.register("sel", self._on_select)

    async def _on_click(self, interaction):
        self.clicks += 1

    async def _on_select(self, interaction):
        self.clicks += 10

    async def build_items(self):
        return [
            discord.ui.ActionRow(self.make_button(custom_id="btn", label="Go")),
            discord.ui.ActionRow(
                self.make_select(
                    custom_id="sel",
                    placeholder="Pick",
                    options=[discord.SelectOption(label="A", value="a")],
                )
            ),
        ]


async def test_prepare_builds_items():
    menu = _FakeMenu()
    await menu.prepare()
    assert menu._prepared is True
    assert len(menu.children) == 2
    for row in menu.children:
        assert isinstance(row, discord.ui.ActionRow)
    assert len(menu.children[0].children) == 1
    assert len(menu.children[1].children) == 1


async def test_prepare_clears_previous_items():
    menu = _FakeMenu()
    await menu.prepare()
    await menu.prepare()
    assert len(menu.children) == 2


async def test_make_button_binds_registered_handler():
    menu = _FakeMenu()
    button = menu.make_button(custom_id="btn", label="Go")
    assert button.callback is menu._handlers["btn"]
    await button.callback(None)
    assert menu.clicks == 1


async def test_make_select_binds_registered_handler():
    menu = _FakeMenu()
    select = menu.make_select(
        custom_id="sel",
        placeholder="Pick",
        options=[discord.SelectOption(label="A", value="a")],
    )
    assert select.callback is menu._handlers["sel"]


async def test_add_row_wraps_in_action_row():
    menu = _FakeMenu()
    button = menu.make_button(custom_id="btn", label="Go")
    menu.add_row(button)
    assert isinstance(menu.children[-1], discord.ui.ActionRow)


async def test_buttons_cannot_be_added_directly():
    menu = _FakeMenu()
    button = menu.make_button(custom_id="btn", label="Go")
    try:
        menu.add_item(button)
    except ValueError:
        return
    raise AssertionError("DesignerView must reject bare Button items")


class _FakeUser:
    def __init__(self, uid: int) -> None:
        self.id = uid


class _FakeInteraction:
    def __init__(self, uid: int) -> None:
        self.user = _FakeUser(uid)


async def test_interaction_check_owner_only():
    menu = _FakeMenu()
    assert await menu.interaction_check(_FakeInteraction(42)) is True
    assert await menu.interaction_check(_FakeInteraction(7)) is False


async def test_interaction_check_open_when_no_owner():
    menu = MenuView()
    assert await menu.interaction_check(_FakeInteraction(7)) is True
