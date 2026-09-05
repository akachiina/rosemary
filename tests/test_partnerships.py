"""Partnership store roundtrips, renewal math and expiry flags."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rosemary.core.partnerships import PartnershipStore


async def test_add_get_remove(tmp_path):
    store = PartnershipStore(tmp_path)
    assert await store.get(1, 100) is None
    await store.add(1, 100, 100, 1, 555, "Hello", ["http://x/y.png"])
    entry = await store.get(1, 100)
    assert entry is not None
    assert entry["rep_id"] == 100
    assert entry["message_id"] == 555
    assert entry["warning_sent"] is False
    assert (await store.all(1))[100]["content"] == "Hello"
    removed = await store.remove(1, 100)
    assert removed is not None and removed["rep_id"] == 100
    assert await store.get(1, 100) is None
    assert await store.remove(1, 100) is None


async def test_touch_renew_resets_warning(tmp_path):
    store = PartnershipStore(tmp_path)
    await store.add(1, 100, 100, 1, 555, "Hi", [])
    await store.mark_warned(1, 100)
    assert (await store.get(1, 100))["warning_sent"] is True
    assert await store.touch_renew(1, 100, 556) is True
    entry = await store.get(1, 100)
    assert entry is not None
    assert entry["warning_sent"] is False
    assert entry["message_id"] == 556
    assert await store.touch_renew(1, 999, None) is False


def test_days_since():
    now = datetime.now(UTC)
    old = (now - timedelta(days=16, hours=1)).isoformat()
    assert 16.0 < PartnershipStore.days_since(old, now) < 17.0
    assert PartnershipStore.days_since(None) == 0.0
    assert PartnershipStore.days_since("not-a-date") == 0.0


async def test_guild_isolation(tmp_path):
    store = PartnershipStore(tmp_path)
    await store.add(1, 100, 100, 1, None, "Hi", [])
    assert await store.all(2) == {}
