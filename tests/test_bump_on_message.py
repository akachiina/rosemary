"""Tests for Disboard bump detection in BumpReminderCog.on_message.

Covers the primary path (content/embeds contain "Bump done") and the
interaction_metadata fallback used when the MESSAGE_CONTENT privileged intent
is revoked (content and embeds come back empty).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from rosemary.core.bump import BumpStore
from rosemary.core.storage import GuildStorage


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


class FakeTheme:
    def color(self, name):
        return MagicMock()

    def style(self, name):
        return MagicMock(color="brand")

    def md(self, template, title=""):
        return title


class FakeBot:
    def __init__(self, storage, enabled=True, cooldown=7200, detection_text="Bump done"):
        self.storage = storage
        self.theme = FakeTheme()
        self.translator = FakeTranslator()
        self._enabled = enabled
        self._cooldown = cooldown
        self._detection_text = detection_text

    def get_guild(self, guild_id):
        return None


async def _make_cog(tmp_path, enabled=True, cooldown=7200, detection_text="Bump done"):
    from rosemary.cogs.bump_reminder import BumpReminderCog

    storage = GuildStorage(tmp_path)
    bot = FakeBot(
        storage, enabled=enabled, cooldown=cooldown, detection_text=detection_text
    )

    async def fake_get_setting(storage, guild_id, key):
        return {
            "bump.enabled": enabled,
            "bump.cooldown": cooldown,
            "bump.detection_text": detection_text,
        }.get(key)

    cog = BumpReminderCog.__new__(BumpReminderCog)
    cog.bot = bot
    cog.store = BumpStore(tmp_path)
    cog._reminder_tasks = {}
    cog._unlock_tasks = {}
    cog._schedule_reminder = MagicMock()
    cog.send_thank_you = AsyncMock()

    import rosemary.cogs.bump_reminder as mod

    orig = mod.get_setting
    mod.get_setting = fake_get_setting
    return cog, mod, orig


async def _make_message(content="", embeds=None, interaction_metadata=None, mentions=None):
    msg = SimpleNamespace()
    msg.content = content
    msg.embeds = embeds or []
    msg.interaction_metadata = interaction_metadata
    msg.mentions = mentions or []
    msg.author = SimpleNamespace(id=302050872383242240)
    msg.guild = SimpleNamespace(id=1)
    msg.channel = SimpleNamespace(mention="#bump")
    if interaction_metadata is not None:
        msg.interaction_metadata = SimpleNamespace(user=SimpleNamespace(id=123))
    return msg


@pytest.fixture
def tmp_path_factory_fixture(tmp_path_factory):
    return tmp_path_factory


class _FakeEmbed:
    def __init__(self, title=None, description=None):
        self.title = title
        self.description = description


# ---------------------------------------------------------------------------

async def test_detects_bump_from_content(tmp_path):
    """Content containing 'Bump done' → detected as bump."""
    cog, mod, orig = await _make_cog(tmp_path)
    try:
        msg = await _make_message(
            content="Thank you! Bump done! Bump the server.",
            interaction_metadata=object(),
        )
        cog.store.record_bump = AsyncMock()
        cog.store.add_bump = AsyncMock()

        await cog.on_message(msg)

        assert cog.store.record_bump.called
        assert cog.store.add_bump.called
        assert cog._schedule_reminder.called
        cog.send_thank_you.assert_awaited_once()
    finally:
        mod.get_setting = orig


async def test_detects_bump_from_ptbr_marker(tmp_path):
    """Configurable marker: pt-BR Disboard 'Bump feito!' → detected."""
    cog, mod, orig = await _make_cog(tmp_path, detection_text="Bump feito")
    try:
        embed = _FakeEmbed(title=None, description="Bump feito!")
        msg = await _make_message(
            content="", embeds=[embed], interaction_metadata=object()
        )
        cog.store.record_bump = AsyncMock()
        cog.store.add_bump = AsyncMock()

        await cog.on_message(msg)

        assert cog.store.record_bump.called
        cog.send_thank_you.assert_awaited_once()
    finally:
        mod.get_setting = orig


async def test_detection_marker_is_case_insensitive(tmp_path):
    """Marker matching ignores case: 'bump feito' vs 'BUMP FEITO'."""
    cog, mod, orig = await _make_cog(tmp_path, detection_text="bump feito")
    try:
        embed = _FakeEmbed(title="BUMP FEITO!", description=None)
        msg = await _make_message(
            content="", embeds=[embed], interaction_metadata=object()
        )
        cog.store.record_bump = AsyncMock()
        cog.store.add_bump = AsyncMock()

        await cog.on_message(msg)

        assert cog.store.record_bump.called
    finally:
        mod.get_setting = orig


async def test_detects_ptbr_embed_via_interaction_metadata(tmp_path):
    """pt-BR embed ('Bump feito!') that doesn't match the default marker is
    still detected via interaction_metadata (outside cooldown)."""
    cog, mod, orig = await _make_cog(tmp_path)
    try:
        embed = _FakeEmbed(title=None, description="Bump feito!")
        msg = await _make_message(
            content="", embeds=[embed], interaction_metadata=object()
        )
        cog.store.record_bump = AsyncMock()
        cog.store.add_bump = AsyncMock()
        cog.store.get_last_bump_time = AsyncMock(return_value=None)

        await cog.on_message(msg)

        cog.store.record_bump.assert_awaited_once()
        cog.send_thank_you.assert_awaited_once_with(1, 123)
    finally:
        mod.get_setting = orig


async def test_ptbr_embed_within_cooldown_ignored(tmp_path):
    """pt-BR embed within cooldown → ignored (Disboard cooldown error)."""
    cog, mod, orig = await _make_cog(tmp_path, cooldown=7200)
    try:
        embed = _FakeEmbed(title=None, description="Bump feito!")
        msg = await _make_message(
            content="", embeds=[embed], interaction_metadata=object()
        )
        cog.store.record_bump = AsyncMock()
        cog.store.add_bump = AsyncMock()
        cog.store.get_last_bump_time = AsyncMock(
            return_value=datetime.now(UTC) - timedelta(minutes=30)
        )

        await cog.on_message(msg)

        assert not cog.store.record_bump.called
        cog.send_thank_you.assert_not_called()
    finally:
        mod.get_setting = orig


async def test_detects_via_disboard_brand_text(tmp_path):
    """Embed with only the 'DISBOARD' brand footer (no interaction_metadata)
    → detected via the brand-text fallback."""
    cog, mod, orig = await _make_cog(tmp_path)
    try:
        embed = _FakeEmbed(title=None, description="[DISBOARD:](https://disboard.org/)")
        mention = SimpleNamespace(id=456)
        msg = await _make_message(
            content="", embeds=[embed], interaction_metadata=None, mentions=[mention]
        )
        cog.store.record_bump = AsyncMock()
        cog.store.add_bump = AsyncMock()
        cog.store.get_last_bump_time = AsyncMock(return_value=None)

        await cog.on_message(msg)

        cog.store.record_bump.assert_awaited_once()
        cog.send_thank_you.assert_awaited_once_with(1, 456)
    finally:
        mod.get_setting = orig


async def test_detects_bump_from_embed(tmp_path):
    """Embed containing 'Bump done' → detected as bump."""
    cog, mod, orig = await _make_cog(tmp_path)
    try:
        embed = _FakeEmbed(title="Bump done!", description=None)
        msg = await _make_message(
            content="", embeds=[embed], interaction_metadata=object()
        )
        cog.store.record_bump = AsyncMock()
        cog.store.add_bump = AsyncMock()

        await cog.on_message(msg)

        assert cog.store.record_bump.called
    finally:
        mod.get_setting = orig


async def test_detects_bump_from_embed_description(tmp_path):
    """Embed description containing 'Bump done' → detected as bump."""
    cog, mod, orig = await _make_cog(tmp_path)
    try:
        embed = _FakeEmbed(title=None, description="Something Bump done!")
        msg = await _make_message(
            content="", embeds=[embed], interaction_metadata=object()
        )
        cog.store.record_bump = AsyncMock()
        cog.store.add_bump = AsyncMock()

        await cog.on_message(msg)

        assert cog.store.record_bump.called
    finally:
        mod.get_setting = orig


async def test_detects_bump_via_interaction_metadata_fallback(tmp_path):
    """When content and embeds are empty (MESSAGE_CONTENT revoked),
    interaction_metadata still catches the Disboard bump."""
    cog, mod, orig = await _make_cog(tmp_path)
    try:
        msg = await _make_message(
            content="", embeds=[], interaction_metadata=object()
        )
        cog.store.record_bump = AsyncMock()
        cog.store.add_bump = AsyncMock()
        # Ensure no prior bump in store (so cooldown guard passes)
        cog.store.get_last_bump_time = AsyncMock(return_value=None)

        await cog.on_message(msg)

        assert cog.store.record_bump.called
        cog.send_thank_you.assert_awaited_once_with(1, 123)
    finally:
        mod.get_setting = orig


async def test_cooldown_guard_blocks_false_positive(tmp_path):
    """Fallback detection within cooldown → ignored (likely Disboard error msg)."""
    cog, mod, orig = await _make_cog(tmp_path, cooldown=3600)
    try:
        msg = await _make_message(
            content="", embeds=[], interaction_metadata=object()
        )
        cog.store.record_bump = AsyncMock()
        cog.store.add_bump = AsyncMock()
        # Last bump was 30 min ago, cooldown is 2h → within cooldown
        cog.store.get_last_bump_time = AsyncMock(
            return_value=datetime.now(UTC) - timedelta(minutes=30)
        )

        await cog.on_message(msg)

        assert not cog.store.record_bump.called
        assert not cog._schedule_reminder.called
        cog.send_thank_you.assert_not_called()
    finally:
        mod.get_setting = orig


async def test_cooldown_guard_allows_outside_cooldown(tmp_path):
    """Fallback detection outside cooldown → allowed (cooldown has elapsed)."""
    cog, mod, orig = await _make_cog(tmp_path, cooldown=3600)
    try:
        msg = await _make_message(
            content="", embeds=[], interaction_metadata=object()
        )
        cog.store.record_bump = AsyncMock()
        cog.store.add_bump = AsyncMock()
        # Last bump was 3h ago, cooldown is 1h → past cooldown
        cog.store.get_last_bump_time = AsyncMock(
            return_value=datetime.now(UTC) - timedelta(hours=3)
        )

        await cog.on_message(msg)

        assert cog.store.record_bump.called
    finally:
        mod.get_setting = orig


async def test_non_disboard_message_ignored(tmp_path):
    """Messages not from Disboard → ignored."""
    cog, mod, orig = await _make_cog(tmp_path)
    try:
        msg = await _make_message(
            content="Bump done!", interaction_metadata=None
        )
        msg.author = SimpleNamespace(id=999)  # not Disboard
        cog.store.record_bump = AsyncMock()

        await cog.on_message(msg)

        assert not cog.store.record_bump.called
    finally:
        mod.get_setting = orig


async def test_non_bump_disboard_message_ignored(tmp_path):
    """Disboard message with no marker, no interaction_metadata and no
    'disboard' brand text → ignored."""
    cog, mod, orig = await _make_cog(tmp_path)
    try:
        msg = await _make_message(
            content="just a random server notice",
            embeds=[],
            interaction_metadata=None,
        )
        cog.store.record_bump = AsyncMock()

        await cog.on_message(msg)

        assert not cog.store.record_bump.called
    finally:
        mod.get_setting = orig


async def test_bump_disabled_guild_ignored(tmp_path):
    """When bump.enabled is False → no detection."""
    cog, mod, orig = await _make_cog(tmp_path, enabled=False)
    try:
        msg = await _make_message(
            content="Bump done!", interaction_metadata=None
        )
        cog.store.record_bump = AsyncMock()

        await cog.on_message(msg)

        assert not cog.store.record_bump.called
    finally:
        mod.get_setting = orig


async def test_user_id_from_mentions_fallback(tmp_path):
    """When interaction_metadata is missing but mentions exist → use mention."""
    cog, mod, orig = await _make_cog(tmp_path)
    try:
        mention = SimpleNamespace(id=456)
        msg = await _make_message(
            content="Bump done!",
            embeds=[],
            interaction_metadata=None,
            mentions=[mention],
        )
        cog.store.record_bump = AsyncMock()
        cog.store.add_bump = AsyncMock()

        await cog.on_message(msg)

        cog.send_thank_you.assert_awaited_once_with(1, 456)
    finally:
        mod.get_setting = orig


async def test_no_user_id_no_record(tmp_path):
    """If neither interaction_metadata nor mentions provide a user → no record."""
    cog, mod, orig = await _make_cog(tmp_path)
    try:
        msg = await _make_message(
            content="Bump done!", embeds=[], interaction_metadata=None, mentions=[]
        )
        cog.store.record_bump = AsyncMock()

        await cog.on_message(msg)

        assert not cog.store.record_bump.called
    finally:
        mod.get_setting = orig
