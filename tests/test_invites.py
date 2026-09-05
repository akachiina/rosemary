"""Invite tracking: attribution, statuses, totals and store roundtrips."""

from __future__ import annotations

from rosemary.core.invites import (
    InviteSnapshot,
    InviteStore,
    find_used_invite,
    join_status,
    total_of,
)


def _snap(code: str, uses: int, inviter: int | None = 1) -> InviteSnapshot:
    return InviteSnapshot(code=code, uses=uses, inviter_id=inviter)


def test_find_used_invite_growth():
    cached = {"a": 3, "b": 1}
    used = find_used_invite(cached, [_snap("a", 3), _snap("b", 2)])
    assert used is not None and used.code == "b"


def test_find_used_invite_new_code_with_uses():
    used = find_used_invite({"a": 3}, [_snap("a", 3), _snap("new", 1)])
    assert used is not None and used.code == "new"


def test_find_used_invite_tie_goes_to_largest_increase():
    used = find_used_invite({"a": 1, "b": 1}, [_snap("a", 2), _snap("b", 5)])
    assert used is not None and used.code == "b"


def test_find_used_invite_nothing_grew():
    assert find_used_invite({"a": 3}, [_snap("a", 3)]) is None
    assert find_used_invite({}, []) is None


def test_join_status_matrix():
    base = {
        "account_age_days": 100.0,
        "fake_delay_days": 7,
        "is_rejoin": False,
        "rejoin_changed_inviter": False,
        "anti_cheat_days": 30,
        "count_rejoins": False,
        "excluded": False,
    }
    assert join_status(**base) == "ok"
    assert join_status(**{**base, "account_age_days": 2.0}) == "fake"
    assert join_status(**{**base, "fake_delay_days": 0, "account_age_days": 0.0}) == "ok"
    assert (
        join_status(
            **{**base, "is_rejoin": True, "rejoin_changed_inviter": True}
        )
        == "flagged"
    )
    assert (
        join_status(
            **{
                **base,
                "is_rejoin": True,
                "rejoin_changed_inviter": True,
                "anti_cheat_days": 0,
                "count_rejoins": False,
            }
        )
        == "rejoin"
    )
    assert (
        join_status(**{**base, "is_rejoin": True, "count_rejoins": True}) == "ok"
    )
    assert join_status(**{**base, "excluded": True}) == "excluded"


def test_total_formula():
    assert total_of({"regular": 5, "bonus": 2, "fake": 1, "left": 1}) == 5
    assert total_of({}) == 0


async def test_record_join_credits_ok_and_fake(tmp_path):
    store = InviteStore(tmp_path)
    await store.record_join(1, 10, 100, "abc", "ok")
    assert (await store.get_stats(1, 100))["total"] == 1
    await store.record_join(1, 11, 100, "abc", "fake")
    stats = await store.get_stats(1, 100)
    assert (stats["regular"], stats["fake"], stats["total"]) == (2, 1, 1)


async def test_record_join_without_credit_statuses(tmp_path):
    store = InviteStore(tmp_path)
    for status in ("flagged", "rejoin", "excluded", "unknown"):
        await store.record_join(1, 20, 100, "abc", status)
    assert (await store.get_stats(1, 100))["total"] == 0
    # Member records are still kept for auditing.
    assert await store.invited_by(1, 20) is not None


async def test_record_leave_decrements_total(tmp_path):
    store = InviteStore(tmp_path)
    await store.record_join(1, 10, 100, "abc", "ok")
    assert await store.record_leave(1, 10) == 100
    stats = await store.get_stats(1, 100)
    assert (stats["left"], stats["total"]) == (1, 0)
    assert await store.record_leave(1, 999) is None


async def test_bonus_add_and_remove(tmp_path):
    store = InviteStore(tmp_path)
    await store.record_join(1, 10, 100, "abc", "ok")
    assert (await store.add_bonus(1, 100, 3))["total"] == 4
    assert (await store.add_bonus(1, 100, -2))["total"] == 2


async def test_leaderboard_order_and_hidden(tmp_path):
    store = InviteStore(tmp_path)
    await store.record_join(1, 10, 100, "a", "ok")
    await store.record_join(1, 11, 200, "b", "ok")
    await store.record_join(1, 12, 200, "b", "ok")
    await store.blacklist_add(1, "hidden", 200)
    rows = await store.leaderboard(1)
    assert [uid for uid, _ in rows] == [100]
    await store.blacklist_remove(1, "hidden", 200)
    rows = await store.leaderboard(1)
    assert [uid for uid, _ in rows] == [200, 100]


async def test_blacklist_roundtrip_and_exclusion(tmp_path):
    store = InviteStore(tmp_path)
    assert await store.blacklist_add(1, "users", 100) is True
    assert await store.blacklist_add(1, "users", 100) is False
    assert await store.is_excluded(1, 100, []) is True
    assert await store.is_excluded(1, 101, []) is False
    await store.blacklist_add(1, "roles", 5)
    assert await store.is_excluded(1, 101, [5, 6]) is True
    assert await store.blacklist_remove(1, "users", 100) is True
    assert await store.blacklist_remove(1, "users", 100) is False


async def test_labels_and_reset_and_lists(tmp_path):
    store = InviteStore(tmp_path)
    await store.record_join(1, 10, 100, "abc", "ok")
    await store.set_label(1, "abc", "tiktok")
    assert await store.get_label(1, "abc") == "tiktok"
    assert await store.get_label(1, "zzz") is None
    await store.set_label(1, "abc", None)
    assert await store.get_label(1, "abc") is None
    rows = await store.invited_list(1, 100)
    assert [uid for uid, _ in rows] == [10]
    totals = await store.server_totals(1)
    assert (totals["regular"], totals["joins"], totals["total"]) == (1, 1, 1)
    await store.reset(1)
    assert await store.leaderboard(1) == []
    assert (await store.server_totals(1))["joins"] == 0
