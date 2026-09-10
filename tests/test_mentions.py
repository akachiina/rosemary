"""Mentions engine: store, spec defaults, legacy-value mapping, repair-on-read.

The policy matrix (``none``/``single``/``winner_auto``/``role``/``all``) was
replaced by mentions-as-content (see :mod:`tests.test_mention_placeholders`):
pings are decided by the ``<@id>`` tokens actually present in the resolved
text, gated by a per-card on/off toggle. What stays here is the storage and
the spec-declared defaults that back the toggle.
"""

from __future__ import annotations

import pytest

import rosemary.core.card_specs  # noqa: F401  (populates the card registry)
from rosemary.core.cards import get_card
from rosemary.core.mentions import (
    MODES,
    MentionStore,
    effective_pings,
    effective_policy,
    pings_default,
    set_pings_for,
    spec_default,
    valid_policy,
)


def test_modes_known():
    """Stored vocabulary is unchanged: legacy values keep working."""
    assert set(MODES) == {"none", "single", "winner_auto", "role", "all"}


def test_spec_defaults_back_the_toggle():
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


def test_pings_default_derives_from_spec():
    assert pings_default("bump.thank_you") is True
    assert pings_default("bump.leaderboard") is True
    assert pings_default("bump.no_bumps") is False
    assert pings_default("moderation.logs.ban.description") is False
    assert pings_default("no.such.card") is False


def test_valid_policy():
    assert valid_policy("single") is True
    assert valid_policy("bogus") is False
    assert valid_policy(None) is False
    assert valid_policy(7) is False


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


async def test_effective_pings_maps_legacy_values(tmp_path):
    """Every legacy non-none value means 'pings on' — no migration needed."""
    bot = _Bot(tmp_path)
    store = MentionStore(tmp_path)
    assert await effective_pings(bot, 1, "bump.thank_you") is True  # spec default
    await store.set_policy(1, "bump.thank_you", "single")
    assert await effective_pings(bot, 1, "bump.thank_you") is True
    await store.set_policy(1, "bump.thank_you", "winner_auto")
    assert await effective_pings(bot, 1, "bump.thank_you") is True
    await store.set_policy(1, "bump.thank_you", "role")
    assert await effective_pings(bot, 1, "bump.thank_you") is True
    await store.set_policy(1, "bump.thank_you", "all")
    assert await effective_pings(bot, 1, "bump.thank_you") is True
    await store.set_policy(1, "bump.thank_you", "none")
    assert await effective_pings(bot, 1, "bump.thank_you") is False


async def test_toggle_roundtrip(tmp_path):
    bot = _Bot(tmp_path)
    await set_pings_for(bot, 1, "bump.no_bumps", True)
    assert await effective_pings(bot, 1, "bump.no_bumps") is True
    await set_pings_for(bot, 1, "bump.no_bumps", False)
    assert await effective_pings(bot, 1, "bump.no_bumps") is False


async def test_corrupt_value_repairs_on_read(tmp_path):
    bot = _Bot(tmp_path)
    store = MentionStore(tmp_path)
    await store._storage.set(1, "bump.thank_you", "garbage")
    assert await effective_pings(bot, 1, "bump.thank_you") is True  # falls to default
    assert await store.get_policy(1, "bump.thank_you") is None  # repaired away
