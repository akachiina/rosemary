"""Every interaction must be ACKed before slow network work.

Discord shows "the application did not respond" when a component/slash
interaction takes >3s without defer/send_message/modal, even if the work
later succeeds. These tests use order-tracking fakes to prove deferral
happens before any network call, plus pin the Close-first nav order.
"""

from __future__ import annotations

import datetime
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key

    async def raw(self, guild_id, key):
        return key


class OrderResponse:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._done = True
        self.order.append("defer")


class OrderInteraction:
    def __init__(self, order, user_id=1, custom_id="", data=None):
        self.order = order
        self.response = OrderResponse(order)
        self.user = SimpleNamespace(id=user_id, mention=f"<@{user_id}>")
        self.custom_id = custom_id
        self.data = data or {}
        self.message = None

    async def edit(self, **kwargs) -> None:
        self.order.append("edit")


def make_bot(tmp_path):
    class Bot:
        theme = load_theme()
        translator = FakeTranslator()

        def __init__(self):
            self.storage = GuildStorage(tmp_path)
            self.guilds = []

        def get_guild(self, guild_id):
            return None

    return Bot()


def nav_ids(row) -> list:
    return [getattr(item, "custom_id", None) for item in row.children]


#: Close-first navigation --------------------------------------------------


async def test_settings_nav_close_always_first(tmp_path):
    from rosemary.core.settings import SettingCategory, settings_for_category
    from rosemary.ui.menu import MenuView  # noqa: F401
    from rosemary.ui.settings_menu import SettingsMenuView

    bot = make_bot(tmp_path)
    for category in (SettingCategory.BUMP, SettingCategory.GENERAL):
        total = max(
            1, (len(settings_for_category(category)) + 6) // 7
        )
        for page in range(total):
            view = SettingsMenuView(bot, 1, owner_id=1)
            view.category = category
            view.page = page
            await view.prepare()
            rows = [
                item
                for item in view.children
                if isinstance(item, discord.ui.ActionRow)
            ]
            nav = rows[-1]
            ids = nav_ids(nav)
            assert ids[0] == "settings_close", (category.value, page, ids)
            assert ids == sorted(
                ids,
                key=["settings_close", "settings_prev", "settings_next"].index,
            )


#: defer-before-work ---------------------------------------------------------


async def test_moderation_confirm_defers_first(tmp_path):
    from rosemary.cogs.moderation import ModerationConfirmView

    bot = make_bot(tmp_path)
    order: list[str] = []

    async def slow(notify):
        order.append("work")
        return "done"

    view = ModerationConfirmView(
        bot, guild_id=1, owner_id=9, action="ban", summary="s", on_confirm=slow
    )
    await view._confirm_notify(OrderInteraction(order))
    assert order == ["defer", "work", "edit"]


async def test_moderation_terminal_state_has_no_buttons(tmp_path):
    """After confirm the dialog keeps the full card, minus the buttons."""
    from rosemary.cogs.moderation import ModerationConfirmView

    bot = make_bot(tmp_path)

    async def slow(notify):
        return "done"

    view = ModerationConfirmView(
        bot, guild_id=1, owner_id=9, action="ban", summary="s", on_confirm=slow
    )
    await view._confirm_notify(OrderInteraction([]))
    items = await view.build_items()
    assert len(items) == 1
    assert [type(item).__name__ for item in view.children] == ["Container"]
    texts = [item.content for item in items[0].items]
    assert len(texts) == 3  # title + summary + result
    assert texts[-1] == "done"


async def test_moderation_question_state_is_card_with_buttons(tmp_path):
    """Before deciding: accent container + heading + one button row."""
    import discord

    from rosemary.cogs.moderation import ModerationConfirmView

    bot = make_bot(tmp_path)

    async def slow(notify):
        return "done"

    view = ModerationConfirmView(
        bot, guild_id=1, owner_id=9, action="ban", summary="s", on_confirm=slow
    )
    items = await view.build_items()
    assert [type(item).__name__ for item in items] == ["Container", "ActionRow"]
    container = items[0]
    assert container.color == bot.theme.color("warning")
    assert container.items[0].content.startswith("# ")
    row = items[1]
    assert isinstance(row, discord.ui.ActionRow)
    assert len(row.children) == 3


async def test_clear_warnings_confirm_defers_first(tmp_path):
    from rosemary.cogs.moderation import ClearWarningsView

    bot = make_bot(tmp_path)
    order: list[str] = []

    async def slow():
        order.append("work")
        return 3

    view = ClearWarningsView(
        bot, guild_id=1, owner_id=9, member_mention="<@2>", on_confirm=slow
    )
    await view._confirm(OrderInteraction(order))
    assert order == ["defer", "work", "edit"]


async def test_boost_invite_accept_defers_first(tmp_path):
    import rosemary.ui.boost_invite as mod
    from rosemary.ui import boost_dm
    from rosemary.ui.boost_invite import BoostInviteView

    bot = make_bot(tmp_path)
    order: list[str] = []

    class Role:
        id = 7
        name = "r"
        mention = "<@&7>"

        async def edit(self, **kwargs):
            order.append("work")

    class Owner:
        async def send(self, content):
            order.append("dm")

    class Member:
        id = 5

        async def add_roles(self, role, reason=None):
            order.append("work")

    class Guild:
        id = 1

        def get_role(self, role_id):
            return Role()

        def get_member(self, member_id):
            return Owner() if member_id == 6 else Member()

    bot.get_guild = lambda guild_id: Guild()

    class Store:
        async def get_invite(self, guild_id, invite_id):
            return {"invitee_id": 5, "inviter_id": 6, "role_id": 7, "expires_at": None}

        async def remove_invite(self, guild_id, invite_id):
            return None

    async def fake_dm_send(bot, destination, guild_id, key, **variables):
        await destination.send("dm")

    orig_send = boost_dm.send
    orig_log = mod.send_log
    boost_dm.send = fake_dm_send
    mod.send_log = AsyncMock()
    try:
        view = BoostInviteView(
            bot, guild_id=1, invite_id="x", accept_label="A", decline_label="D"
        )
        view.store = Store()
        await view._accept(OrderInteraction(order, user_id=5))
    finally:
        boost_dm.send = orig_send
        mod.send_log = orig_log

    assert order[0] == "defer"
    assert "work" in order
    assert order.index("defer") < order.index("work")
    assert order[-1] == "edit"


async def test_debug_test_log_defers_first(tmp_path, monkeypatch):
    import rosemary.cogs.debug as mod
    from rosemary.cogs.debug import DebugMenuView

    bot = make_bot(tmp_path)
    order: list[str] = []

    async def slow_log(*args, **kwargs):
        order.append("work")
        return False

    monkeypatch.setattr(mod, "send_log", slow_log)
    view = DebugMenuView(bot, 1, owner_id=1)
    await view._test_log(OrderInteraction(order))
    assert order == ["defer", "work", "edit"]


async def test_settings_apply_defers_first(tmp_path, monkeypatch):
    import rosemary.ui.settings_menu as mod
    from rosemary.ui.settings_menu import SettingsMenuView

    bot = make_bot(tmp_path)
    order: list[str] = []

    async def slow_log(*args, **kwargs):
        order.append("work")
        return False

    monkeypatch.setattr(mod, "send_log", slow_log)
    view = SettingsMenuView(bot, 1, owner_id=1)
    interaction = OrderInteraction(order)
    await view._apply_value(interaction, "general.prefix", "Q")
    assert order[0] == "defer"
    assert order.index("defer") < order.index("work")
    assert order[-1] == "edit"


async def test_boost_pick_color_defers_first(tmp_path):
    from rosemary.ui.boost_menu import BoostMenuView

    bot = make_bot(tmp_path)
    order: list[str] = []

    class Role:
        id = 123
        name = "r"
        mention = "<@&123>"
        icon = None
        unicode_emoji = None
        color = "brand"

        async def edit(self, **kwargs):
            order.append("work")

    class Guild:
        id = 1

        def get_role(self, role_id):
            return Role()

    bot.get_guild = lambda guild_id: Guild()
    view = BoostMenuView(bot, 1, owner_id=1)
    view.role_id = 123
    interaction = OrderInteraction(order, data={"values": ["ff0000"]})
    await view._pick_color(interaction)
    assert order[0] == "defer"
    assert order.index("defer") < order.index("work")


#: stale interactions (dead token) -----------------------------------------


class DeadResponse:
    """Response fake for an interaction whose token is already gone."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.deferred = 0

    def is_done(self) -> bool:
        return False

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred += 1
        if self.error is not None:
            raise self.error


def _not_found() -> discord.NotFound:
    """The exact error Discord answers for a dead interaction token."""
    return discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"),
        {"code": 10062, "message": "Unknown interaction"},
    )


def _interaction_at(age_seconds: int):
    """An interaction created ``age_seconds`` ago (its token may be dead)."""
    created = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        seconds=age_seconds
    )
    interaction = SimpleNamespace(
        id=discord.utils.time_snowflake(created),
        created_at=created,
        custom_id="colors_pick:abc",
        user=SimpleNamespace(id=7),
        guild_id=1,
        data={},
    )
    interaction.response = DeadResponse()
    interaction.followup = SimpleNamespace(send=AsyncMock())
    return interaction


async def test_expired_interaction_is_never_acked():
    """A token past Discord's 15 minute window must not even be tried."""
    from rosemary.core.interactions import TOKEN_TTL, ack, is_expired

    interaction = _interaction_at(TOKEN_TTL + 60)
    assert is_expired(interaction)
    assert await ack(interaction) is False
    assert interaction.response.deferred == 0
    interaction.followup.send.assert_not_awaited()


async def test_fresh_interaction_is_acked():
    from rosemary.core.interactions import ack, is_expired

    interaction = _interaction_at(5)
    assert not is_expired(interaction)
    assert await ack(interaction) is True
    assert interaction.response.deferred == 1


async def test_dead_token_error_is_swallowed():
    """Discord answering 10062 (a click replayed after a RESUME) is not a crash."""
    from rosemary.core.interactions import ack

    interaction = _interaction_at(5)
    interaction.response.error = _not_found()
    assert await ack(interaction) is False
    assert interaction.response.deferred == 1


async def test_other_http_errors_still_propagate():
    """A real HTTP failure stays visible: only the dead token is swallowed."""
    from rosemary.core.interactions import ack

    interaction = _interaction_at(5)
    interaction.response.error = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"),
        {"code": 50013, "message": "Missing permissions"},
    )
    with pytest.raises(discord.Forbidden):
        await ack(interaction)


async def test_view_error_stale_interaction_warns_without_answering(caplog):
    """A dead token must not log an ERROR traceback nor a doomed answer."""
    from rosemary.bot import RosemaryBot

    bot = RosemaryBot.__new__(RosemaryBot)
    interaction = _interaction_at(5)
    item = SimpleNamespace(custom_id="colors_pick:abc")
    with caplog.at_level(logging.WARNING):
        await bot.on_view_error(_not_found(), item, interaction)
    assert "Stale interaction ignored" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert interaction.response.deferred == 0
    interaction.followup.send.assert_not_awaited()
