"""Ticket store roundtrips and panel record handling."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from rosemary.core.storage import GuildStorage
from rosemary.core.tickets import CLOSED, OPEN, TicketStore
from rosemary.ui.theme import load_theme


async def test_open_close_reopen_delete(tmp_path):
    store = TicketStore(tmp_path)
    assert await store.get_ticket(1, 10) is None
    await store.open_ticket(1, 10, 100, "report")
    ticket = await store.get_ticket(1, 10)
    assert ticket is not None
    assert ticket["owner_id"] == 100
    assert ticket["type"] == "report"
    assert ticket["status"] == OPEN
    assert await store.user_open_count(1, 100) == 1

    assert await store.set_status(1, 10, CLOSED) is True
    assert (await store.get_ticket(1, 10))["status"] == CLOSED
    assert await store.user_open_count(1, 100) == 0
    assert await store.set_status(1, 10, OPEN) is True
    assert await store.user_open_count(1, 100) == 1

    assert await store.delete_ticket(1, 10) is True
    assert await store.delete_ticket(1, 10) is False
    assert await store.get_ticket(1, 10) is None
    assert await store.set_status(1, 10, CLOSED) is False


async def test_open_tickets_and_panel(tmp_path):
    store = TicketStore(tmp_path)
    await store.open_ticket(1, 10, 100, "report")
    await store.open_ticket(1, 11, 200, "boost")
    await store.set_status(1, 11, CLOSED)
    opened = await store.open_tickets(1)
    assert sorted(opened) == [10]
    assert await store.get_panel(1) is None
    await store.set_panel(1, 555)
    assert await store.get_panel(1) == 555
    await store.set_panel(1, None)
    assert await store.get_panel(1) is None
    # Guild isolation.
    assert await store.open_tickets(2) == {}


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


def _bot(tmp_path):
    class Bot:
        theme = load_theme()
        translator = FakeTranslator()

        def __init__(self):
            self.storage = GuildStorage(tmp_path)
            self.views = []

        def add_view(self, view):
            self.views.append(view)

    return Bot()


def _channel():
    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock()
    message.id = 777
    channel.send = AsyncMock(return_value=message)
    channel.fetch_message = AsyncMock(return_value=message)
    return channel


def _guild(channel):
    guild = MagicMock()
    guild.id = 1
    guild.get_channel = MagicMock(return_value=channel)
    return guild


def _view_custom_ids(view):
    """Every button/select custom_id reachable in a (possibly nested) view."""
    found = []

    def walk(node):
        custom_id = getattr(node, "custom_id", None)
        if custom_id:
            found.append(custom_id)
        for child in list(getattr(node, "items", []) or []) + list(
            getattr(node, "children", []) or []
        ):
            walk(child)

    for child in view.children:
        walk(child)
    return found


async def test_post_panel_sends_select_inside_action_row(tmp_path):
    """Regression: raw Select added straight to DesignerView raised ValueError,
    so the panel never reached the channel and nothing was stored."""
    from rosemary.cogs.tickets import TicketsCog

    bot = _bot(tmp_path)
    await bot.storage.set(1, "tickets.enabled", True)
    cog = TicketsCog(bot)
    channel = _channel()
    guild = _guild(channel)
    await cog._post_panel(guild, channel)
    assert channel.send.await_count == 1
    assert await cog.store.get_panel(1) == 777
    view = channel.send.call_args.kwargs.get("view")
    ids = _view_custom_ids(view)
    assert "tickets_open" in ids


async def test_restore_registers_views_with_children(tmp_path):
    """Persistent dispatch matches on item custom_ids: restored views must
    carry their rows or every button stays dead after a restart."""
    from rosemary.cogs.tickets import TicketsCog

    bot = _bot(tmp_path)
    await bot.storage.set(1, "tickets.enabled", True)
    await bot.storage.set(1, "tickets.panel_channel", 111)
    cog = TicketsCog(bot)
    await cog.store.open_ticket(1, 10, 100, "report")
    await cog.store.set_panel(1, 555)
    channel = _channel()
    guild = _guild(channel)
    await cog._restore_guild(guild)
    # Panel already exists: no repost, but both views registered with items.
    assert channel.send.await_count == 0
    assert len(bot.views) == 2
    ids = [cid for view in bot.views for cid in _view_custom_ids(view)]
    assert "tickets_open" in ids
    assert "tickets_close:10" in ids


async def test_restore_posts_missing_panel(tmp_path):
    from rosemary.cogs.tickets import TicketsCog

    bot = _bot(tmp_path)
    await bot.storage.set(1, "tickets.enabled", True)
    await bot.storage.set(1, "tickets.panel_channel", 111)
    cog = TicketsCog(bot)
    channel = _channel()
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), MagicMock()))
    guild = _guild(channel)
    await cog._restore_guild(guild)
    assert channel.send.await_count == 1
    assert await cog.store.get_panel(1) == 777


async def test_pick_type_invalid_value_acks(tmp_path):
    """A tampered select value must still ACK the interaction."""
    from rosemary.cogs.tickets import TicketPanelView

    bot = _bot(tmp_path)
    view = TicketPanelView(bot, 1)
    order = []

    class FakeResponse:
        def is_done(self):
            return False

        async def defer(self, *, ephemeral=False):
            order.append(("defer", ephemeral))

    interaction = SimpleNamespace(response=FakeResponse(), data={"values": ["bogus"]})
    await view._pick_type(interaction)
    assert ("defer", True) in order
