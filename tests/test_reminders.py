"""Reminder store roundtrips and anti-invite pattern checks."""

from __future__ import annotations

from rosemary.cogs.anti_invite import _INVITE_RE
from rosemary.core.reminders import ReminderStore


async def test_reminder_store_crud(tmp_path):
    store = ReminderStore(tmp_path)
    assert await store.all(1) == {}
    await store.save(1, "Evento", "Amanhã tem evento!")
    assert await store.get(1, "evento") == "Amanhã tem evento!"
    assert await store.get(1, "missing") is None
    assert list((await store.all(1)).keys()) == ["evento"]
    assert await store.delete(1, "Evento") is True
    assert await store.delete(1, "Evento") is False
    # Guild isolation.
    await store.save(1, "a", "x")
    assert await store.all(2) == {}


def test_invite_link_pattern():
    assert _INVITE_RE.search("join https://discord.gg/abc123") is not None
    assert _INVITE_RE.search("see discord.com/invite/xyz-9") is not None
    assert _INVITE_RE.search("no links here") is None
    assert _INVITE_RE.search("https://google.com") is None
    match = _INVITE_RE.search("discord.gg/abc123")
    assert match is not None and match.group(1) == "abc123"
