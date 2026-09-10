"""Stale (pre-restart) customize picker recovery + view error surfacing.

Ephemeral picker messages survive a bot restart looking alive, but the live
view is gone and py-cord silently no-ops the click (ViewStore.dispatch finds
nothing and returns). The persistent ``CustomizeMenuRecoveryView`` catches
those clicks via the store's ``(component_type, None, custom_id)`` fallback
and re-opens a fresh picker instead.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import discord

from rosemary.bot import RosemaryBot
from rosemary.core.storage import GuildStorage
from rosemary.ui.customize_menu import (
    _PICKER_PERSISTENT_IDS,
    CustomizeMenuRecoveryView,
    CustomizeMenuView,
    register_recovery_view,
)
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


class FakeUser:
    id = 1


def make_bot(tmp_path):
    class Bot:
        theme = load_theme()
        translator = FakeTranslator()

        def __init__(self):
            self.storage = GuildStorage(tmp_path)
            self.added_views = []

        def add_view(self, view, **kwargs):
            self.added_views.append(view)

    return Bot()


class FakeResponse:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.order.append("defer")
        self._done = True

    async def send_message(self, *args, **kwargs) -> None:
        self.order.append("send_message")


class FakeFollowup:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def send(self, *args, **kwargs) -> None:
        self.order.append("send")


class FakeInteraction:
    def __init__(self, custom_id="", data=None, edit_error=None):
        self.order: list[str] = []
        self.response = FakeResponse(self.order)
        self.followup = FakeFollowup(self.order)
        self.data = data or {}
        self.custom_id = custom_id
        self.user = FakeUser()
        self.guild_id = 1
        self.message = None
        self.edit_kwargs: dict = {}
        self.edit_error = edit_error

    async def edit(self, **kwargs) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        self.edit_kwargs = kwargs
        self.order.append("edit")


def _not_found() -> discord.NotFound:
    response = SimpleNamespace(status=404, reason="Not Found")
    return discord.NotFound(
        response, {"code": 10062, "message": "Unknown Interaction"}
    )


# -- registration --------------------------------------------------------------


async def test_recovery_view_covers_every_picker_id(tmp_path):
    view = CustomizeMenuRecoveryView(make_bot(tmp_path))
    assert view.timeout is None  # persistence requirement
    ids = set()
    for row in view.children:
        for item in getattr(row, "children", [row]):
            if getattr(item, "custom_id", None):
                ids.add(item.custom_id)
    assert ids == set(_PICKER_PERSISTENT_IDS)
    assert view.is_persistent()


async def test_register_recovery_view_adds_view_to_bot(tmp_path):
    bot = make_bot(tmp_path)
    register_recovery_view(bot)
    assert len(bot.added_views) == 1
    assert isinstance(bot.added_views[0], CustomizeMenuRecoveryView)


# -- recovery flow ---------------------------------------------------------------


async def test_recovery_swaps_stale_message_for_fresh_picker(tmp_path):
    bot = make_bot(tmp_path)
    view = CustomizeMenuRecoveryView(bot)
    interaction = FakeInteraction(custom_id="custom_pick_card")
    await view._recover(interaction)
    assert interaction.order == ["defer", "edit"]
    fresh = interaction.edit_kwargs["view"]
    assert isinstance(fresh, CustomizeMenuView)
    assert fresh.author_id == 1  # re-owned by the clicker
    assert fresh.children  # fresh picker actually built


async def test_recovery_falls_back_to_followup_when_message_gone(tmp_path):
    bot = make_bot(tmp_path)
    view = CustomizeMenuRecoveryView(bot)
    interaction = FakeInteraction(custom_id="custom_back", edit_error=_not_found())
    await view._recover(interaction)
    assert interaction.order == ["defer", "send"]  # edit failed, followup sent


async def test_recovery_requires_guild(tmp_path):
    bot = make_bot(tmp_path)
    view = CustomizeMenuRecoveryView(bot)
    interaction = FakeInteraction(custom_id="custom_pick_category")
    interaction.guild_id = None
    await view._recover(interaction)
    # ACKed (never "did not respond") but no menu can be built outside a guild.
    assert interaction.order == ["defer"]


# -- view error surfacing -------------------------------------------------------


def _make_bot_for_errors() -> RosemaryBot:
    bot = RosemaryBot.__new__(RosemaryBot)
    bot.translator = FakeTranslator()
    return bot


async def test_on_view_error_acks_unacknowledged_interaction(caplog):
    bot = _make_bot_for_errors()
    interaction = FakeInteraction()
    item = SimpleNamespace(custom_id="custom_pick_card")
    # Raise inside except so log.exception attaches the traceback (as the
    # real dispatch path does when calling on_error from its except block).
    with caplog.at_level(logging.ERROR, logger="rosemary.bot"):
        try:
            raise RuntimeError("boom")
        except RuntimeError as error:
            await bot.on_view_error(error, item, interaction)
    assert interaction.order == ["send_message"]
    assert "custom_pick_card" in caplog.text
    assert "boom" in caplog.text


async def test_on_view_error_uses_followup_when_already_acked(caplog):
    bot = _make_bot_for_errors()
    interaction = FakeInteraction()
    await interaction.response.defer()
    item = SimpleNamespace(custom_id="card_pings_on")
    with caplog.at_level(logging.ERROR, logger="rosemary.bot"):
        try:
            raise RuntimeError("boom")
        except RuntimeError as error:
            await bot.on_view_error(error, item, interaction)
    assert interaction.order == ["defer", "send"]


async def test_on_view_error_swallows_ack_failures(caplog):
    bot = _make_bot_for_errors()
    interaction = FakeInteraction()

    async def broken(*args, **kwargs):
        raise discord.HTTPException(
            SimpleNamespace(status=500, reason="boom"), "sending failed"
        )

    interaction.response.send_message = broken
    item = SimpleNamespace(custom_id="custom_close")
    with caplog.at_level(logging.DEBUG, logger="rosemary.bot"):
        try:
            raise RuntimeError("boom")
        except RuntimeError as error:
            await bot.on_view_error(error, item, interaction)
    # No exception escaped; the original error is still logged.
    assert "custom_close" in caplog.text
