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
            self.guild = None

        def get_guild(self, guild_id):
            return self.guild

        def add_view(self, view):
            self.views.append(view)

    return Bot()


def _channel():
    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock()
    message.id = 777
    channel.send = AsyncMock(return_value=message)
    channel.fetch_message = AsyncMock(return_value=message)
    partial = MagicMock()
    partial.edit = AsyncMock()
    channel.get_partial_message = MagicMock(return_value=partial)
    channel.partial = partial
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
    bot.guild = guild
    await cog._restore_guild(guild)
    # Panel already exists: edited in place (freshened), no repost, and both
    # views registered with items.
    assert channel.send.await_count == 0
    channel.partial.edit.assert_awaited()
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
    # The stored panel message is gone: the in-place edit 404s, so the
    # repaint falls back to a fresh post.
    channel.partial.edit = AsyncMock(
        side_effect=discord.NotFound(MagicMock(), MagicMock())
    )
    guild = _guild(channel)
    bot.guild = guild
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


async def test_delete_confirms_before_removing_channel(tmp_path):
    """Regression: followup.send after channel.delete raised 400 Unknown
    Channel, since followups post into the ticket channel itself."""
    from rosemary.cogs.tickets import TicketActionsView, TicketsCog

    bot = _bot(tmp_path)
    await bot.storage.set(1, "tickets.enabled", True)
    cog = TicketsCog(bot)
    bot.get_cog = lambda name: cog if name == "TicketsCog" else None
    await cog.store.open_ticket(1, 10, 100, "report")
    await cog.store.set_status(1, 10, CLOSED)

    order = []
    channel = MagicMock(spec=discord.TextChannel)

    async def fake_delete(*, reason=None):
        order.append("delete")

    channel.delete = fake_delete
    guild = MagicMock()
    guild.id = 1
    guild.get_channel = MagicMock(return_value=channel)
    bot.get_guild = lambda gid: guild

    class FakeResponse:
        def is_done(self):
            return False

        async def defer(self, *, ephemeral=False):
            order.append(("defer", ephemeral))

        async def send_message(self, content, **kwargs):
            order.append("send_message")

    class FakeFollowup:
        async def send(self, content, **kwargs):
            order.append("followup")

    interaction = SimpleNamespace(
        response=FakeResponse(),
        followup=FakeFollowup(),
        custom_id="tickets_delete:10",
        user=SimpleNamespace(id=100),
        guild=guild,
    )
    view = TicketActionsView(bot, 1, 10)
    await view._delete(interaction)
    assert ("defer", True) in order
    assert order.index("followup") < order.index("delete")


# channel naming + live registration ==========================================


def test_slug_keeps_non_ascii_names():
    """CJK/Cyrillic/fullwidth display names used to collapse to the
    'ticket' fallback, so the channel lost the user's name entirely."""
    from rosemary.cogs.tickets import _slug

    assert _slug("団小狼") == "団小狼"
    assert _slug(".freebuff") == "freebuff"
    assert _slug("Salada") == "salada"
    assert _slug("＠ｆｕｌｌｗｉｄｔｈ") == "ｆｕｌｌｗｉｄｔｈ"
    assert _slug("мир") == "мир"
    assert _slug("!!!") == "ticket"  # nothing keepable: fallback stands
    assert _slug("a" * 50) == "a" * 20  # cap


def test_channel_name_carries_prefix_and_owner(tmp_path):
    """The full name is ``<type>-<owner>`` with BOTH slugs non-empty."""
    from rosemary.cogs.tickets import TICKET_TYPES, _slug

    for ticket_type in TICKET_TYPES:
        assert _slug(f"tipo-{ticket_type}"), ticket_type


async def test_create_ticket_registers_dispatch_view_with_children(tmp_path):
    """Regression (live: 'O aplicativo não respondeu a tempo'): the view was
    registered item-less, so py-cord indexed no custom_ids and every click on
    the fresh ticket's buttons died until the next restart."""
    from rosemary.cogs.tickets import TicketsCog

    bot = _bot(tmp_path)
    await bot.storage.set(1, "tickets.enabled", True)
    await bot.storage.set(1, "tickets.category", 7)
    cog = TicketsCog(bot)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.me = MagicMock()
    guild.me.guild_permissions = discord.Permissions(manage_channels=True)
    category = MagicMock(spec=discord.CategoryChannel)
    guild.get_channel = MagicMock(return_value=category)

    created = {}

    async def fake_create(name, *, category=None, overwrites=None, reason=None):
        created["name"] = name
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 4242
        channel.mention = "<#4242>"
        channel.send = AsyncMock(return_value=MagicMock(id=999))
        return channel

    guild.create_text_channel = fake_create

    owner = MagicMock(spec=discord.Member)
    owner.id = 100
    owner.display_name = "団小狼"
    owner.mention = "<@100>"

    ok, detail = await cog.create_ticket(guild, owner, "partnership")
    assert ok is True
    # The channel kept the owner's name (CJK survived the slug).
    assert "団小狼" in created["name"]
    # The dispatch view was registered WITH its action row.
    assert len(bot.views) == 1
    ids = _view_custom_ids(bot.views[0])
    assert "tickets_close:4242" in ids
    assert "tickets_delete:4242" in ids
    assert await cog.store.get_ticket(1, 10) is None
