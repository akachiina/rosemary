"""Mention policies: resolver, store and card-spec defaults."""

from __future__ import annotations

import pytest

import rosemary.core.card_specs  # noqa: F401  (populates the card registry)
from rosemary.core.cards import get_card
from rosemary.core.mentions import (
    MODES,
    MentionStore,
    effective_policy,
    mentions_for,
    resolve_allowed_mentions,
    spec_default,
)


def test_modes_known():
    assert set(MODES) == {"none", "single", "winner_auto", "role", "all"}


def test_resolve_none_never_pings():
    allowed = resolve_allowed_mentions("none", [1, 2, 3], [4])
    assert allowed.to_dict() == {"parse": []}


def test_resolve_single_pings_first_user_only():
    allowed = resolve_allowed_mentions("single", [111, 222])
    data = allowed.to_dict()
    assert data.get("users") == [111]
    assert "users" not in data.get("parse", [])


def test_resolve_single_without_candidates_is_none():
    assert resolve_allowed_mentions("single", []).to_dict() == {"parse": []}


def test_resolve_winner_auto_only_on_auto_source():
    auto = resolve_allowed_mentions("winner_auto", [111, 222], source="auto")
    assert auto.to_dict().get("users") == [111]
    cmd = resolve_allowed_mentions("winner_auto", [111, 222], source="command")
    assert cmd.to_dict() == {"parse": []}


def test_resolve_role_pings_role_only():
    allowed = resolve_allowed_mentions("role", [], [555])
    data = allowed.to_dict()
    assert data.get("roles") == [555]
    assert data.get("users", None) in (None, False)


def test_resolve_role_without_role_is_none():
    assert resolve_allowed_mentions("role", [111], []).to_dict() == {"parse": []}


def test_resolve_unknown_policy_is_none():
    assert resolve_allowed_mentions("bogus", [1]).to_dict() == {"parse": []}


def test_spec_defaults():
    assert spec_default("bump.leaderboard") == "winner_auto"
    assert spec_default("bump.reminder") == "role"
    assert spec_default("bump.thank_you") == "single"
    assert spec_default("birthdays.announce") == "single"
    assert spec_default("events.welcome") == "single"
    assert spec_default("bump.no_bumps") == "none"
    assert spec_default("bump.logs.week_reset.description") == "none"
    assert spec_default("boost.logs.register.description") == "none"
    assert spec_default("no.such.card") == "none"
    assert get_card("bump.leaderboard").mention_default == "winner_auto"


async def test_store_roundtrip(tmp_path):
    store = MentionStore(tmp_path)
    assert await store.get_policy(1, "bump.leaderboard") is None
    await store.set_policy(1, "bump.leaderboard", "all")
    assert await store.get_policy(1, "bump.leaderboard") == "all"
    await store.reset(1, "bump.leaderboard")
    assert await store.get_policy(1, "bump.leaderboard") is None
    with pytest.raises(ValueError):
        await store.set_policy(1, "bump.leaderboard", "bogus")


class _Bot:
    def __init__(self, tmp_path):
        from rosemary.core.storage import GuildStorage

        self.storage = GuildStorage(tmp_path)


async def test_effective_policy_prefers_override(tmp_path):
    bot = _Bot(tmp_path)
    assert await effective_policy(bot, 1, "bump.leaderboard") == "winner_auto"
    store = MentionStore(tmp_path)
    await store.set_policy(1, "bump.leaderboard", "none")
    assert await effective_policy(bot, 1, "bump.leaderboard") == "none"


async def test_mentions_for_command_never_pings_leaderboard(tmp_path):
    bot = _Bot(tmp_path)
    allowed = await mentions_for(bot, 1, "bump.leaderboard", source="command", user_ids=[7])
    assert allowed.to_dict() == {"parse": []}
    auto = await mentions_for(bot, 1, "bump.leaderboard", source="auto", user_ids=[7])
    assert auto.to_dict().get("users") == [7]
