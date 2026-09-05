"""Ticket store roundtrips and panel record handling."""

from __future__ import annotations

from rosemary.core.tickets import CLOSED, OPEN, TicketStore


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
