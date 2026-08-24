"""Cover the bump reminder restart-recovery path.

The bot must resend an overdue bump reminder after a restart (matching the old
Alecrins bot). ``BumpReminderCog.__init__`` does not start the scheduled loops
(only ``start()`` does — py-cord 2.8.1 never calls ``cog_load``), so the cog can
be built directly with fakes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


class FakeBot:
    theme = load_theme()
    translator = FakeTranslator()

    def __init__(self, storage, enabled=True, cooldown=7200) -> None:
        self.storage = storage
        self.guilds = []
        self._enabled = enabled
        self._cooldown = cooldown

    def get_guild(self, guild_id):
        return None

    async def wait_until_ready(self) -> None:
        return None


class FakeStore:
    def __init__(self, state, last_bump):
        self._state = state
        self._last_bump = last_bump
        self.get_reminder = AsyncMock(return_value=state)
        self.get_last_bump_time = AsyncMock(return_value=last_bump)


async def _make_cog(tmp_path, state, last_bump, enabled=True, cooldown=7200):
    from rosemary.cogs.bump_reminder import BumpReminderCog

    storage = GuildStorage(tmp_path)
    bot = FakeBot(storage, enabled=enabled, cooldown=cooldown)

    async def fake_get_setting(storage, guild_id, key):
        return {
            "bump.enabled": enabled,
            "bump.cooldown": cooldown,
            "bump.detection_text": "Bump done",
        }.get(key)

    cog = BumpReminderCog(bot)
    cog.store = FakeStore(state, last_bump)
    cog.send_reminder = AsyncMock()
    cog._schedule_reminder = MagicMock()  # sync method, spied (not actually scheduled)

    monkeypatch_set = pytest.MonkeyPatch()
    monkeypatch_set.setattr(
        "rosemary.cogs.bump_reminder.get_setting", fake_get_setting
    )
    return cog, monkeypatch_set


async def test_restart_sends_overdue_reminder(tmp_path):
    """Bot down during cooldown, restarted past it -> reminder fires on startup."""
    state = {"reminder_sent": False}
    last_bump = datetime.now(UTC) - timedelta(seconds=8000)  # past a 7200s cooldown
    cog, mp = await _make_cog(tmp_path, state, last_bump, cooldown=7200)
    try:
        await cog._check_pending_reminders(1)
    finally:
        mp.undo()

    assert cog.send_reminder.called
    assert cog.send_reminder.await_count == 1
    cog.send_reminder.assert_awaited_once_with(1)
    assert not cog._schedule_reminder.called


async def test_restart_schedules_pending_reminder(tmp_path):
    """Bump recorded recently, bot restarts before cooldown ends -> schedule."""
    state = {"reminder_sent": False}
    last_bump = datetime.now(UTC) - timedelta(seconds=60)  # 100s left of 7200s
    cog, mp = await _make_cog(tmp_path, state, last_bump, cooldown=7200)
    try:
        await cog._check_pending_reminders(1)
    finally:
        mp.undo()

    assert not cog.send_reminder.called
    assert cog._schedule_reminder.called
    delay = cog._schedule_reminder.call_args.args[1]
    assert 0 < delay <= 7140


async def test_restart_skips_when_already_sent(tmp_path):
    state = {"reminder_sent": True}
    last_bump = datetime.now(UTC) - timedelta(seconds=8000)
    cog, mp = await _make_cog(tmp_path, state, last_bump, cooldown=7200)
    try:
        await cog._check_pending_reminders(1)
    finally:
        mp.undo()

    assert not cog.send_reminder.called
    assert not cog._schedule_reminder.called


async def test_restart_skips_when_disabled(tmp_path):
    state = {"reminder_sent": False}
    last_bump = datetime.now(UTC) - timedelta(seconds=8000)
    cog, mp = await _make_cog(tmp_path, state, last_bump, enabled=False, cooldown=7200)
    try:
        await cog._check_pending_reminders(1)
    finally:
        mp.undo()

    assert not cog.send_reminder.called
    assert not cog._schedule_reminder.called


async def test_restart_skips_without_a_bump(tmp_path):
    state = {"reminder_sent": False}
    cog, mp = await _make_cog(tmp_path, state, last_bump=None, cooldown=7200)
    try:
        await cog._check_pending_reminders(1)
    finally:
        mp.undo()

    assert not cog.send_reminder.called
    assert not cog._schedule_reminder.called


async def test_send_reminder_separates_content_and_view(tmp_path):
    """Regression: send_reminder must never pass content and a V2 view in the
    same channel.send() call, or py-cord raises:

        TypeError: cannot send embeds or content with a view using v2 component logic

    The reminder must send content (role ping) and the DesignerView in two
    separate calls.
    """
    import rosemary.cogs.bump_reminder as mod
    from rosemary.cogs.bump_reminder import BumpReminderCog

    storage = GuildStorage(tmp_path)

    channel = AsyncMock(spec=discord.TextChannel)
    guild = MagicMock()
    guild.id = 1
    guild.get_channel = MagicMock(return_value=channel)

    bot = FakeBot(storage)
    bot.get_guild = MagicMock(return_value=guild)

    async def fake_get_setting(storage, guild_id, key):
        return {
            "bump.enabled": True,
            "bump.cooldown": 7200,
            "bump.channel": 111,
            "bump.ping_role": 42,
            "bump.detection_text": "Bump done",
        }.get(key)

    orig_get_setting = mod.get_setting
    orig_send_channel_log = mod.send_channel_log

    mod.get_setting = fake_get_setting
    mod.send_channel_log = AsyncMock()

    cog = BumpReminderCog.__new__(BumpReminderCog)
    cog.bot = bot
    cog.store = AsyncMock()
    cog.store.mark_reminder_sent = AsyncMock()
    cog._build_reminder_view = AsyncMock(return_value=MagicMock())

    try:
        await cog.send_reminder(1)
    finally:
        mod.get_setting = orig_get_setting
        mod.send_channel_log = orig_send_channel_log

    # Two send calls: one for the role ping, one for the view alone.
    assert channel.send.await_count == 2

    first_call = channel.send.await_args_list[0]
    second_call = channel.send.await_args_list[1]

    # First call: content only (no view)
    assert first_call.args[0] == "<@&42>"
    assert first_call.kwargs.get("view") is None

    # Second call: view only (no content)
    assert second_call.kwargs.get("view") is not None
    assert len(second_call.args) == 0

    cog.store.mark_reminder_sent.assert_awaited_once_with(1)


async def test_start_schedules_recovery_per_guild(tmp_path):
    """``start()`` must schedule reminder + channel recovery for every guild.

    Regression: py-cord 2.8.1's sync ``add_cog`` never calls ``cog_load``, so
    pending reminders were silently dropped after a restart. The bot starts the
    cog explicitly via ``start()`` instead.
    """
    from rosemary.cogs.bump_reminder import BumpReminderCog

    storage = GuildStorage(tmp_path)
    bot = FakeBot(storage)
    bot.guilds = [type("G", (), {"id": 1})(), type("G", (), {"id": 2})()]

    cog = BumpReminderCog(bot)
    cog._check_pending_reminders = AsyncMock()
    cog._check_and_recover_channel = AsyncMock()

    await cog.start()
    await asyncio.sleep(0)  # let the scheduled tasks run

    assert cog._started is True
    assert cog._check_pending_reminders.await_count == 2
    assert cog._check_and_recover_channel.await_count == 2
    cog._check_pending_reminders.assert_any_await(1)
    cog._check_pending_reminders.assert_any_await(2)
    cog._check_and_recover_channel.assert_any_await(1)
    cog._check_and_recover_channel.assert_any_await(2)


async def test_start_is_idempotent(tmp_path):
    """Calling ``start()`` twice must not double-schedule recovery."""
    from rosemary.cogs.bump_reminder import BumpReminderCog

    storage = GuildStorage(tmp_path)
    bot = FakeBot(storage)
    bot.guilds = [type("G", (), {"id": 1})()]

    cog = BumpReminderCog(bot)
    cog._check_pending_reminders = AsyncMock()
    cog._check_and_recover_channel = AsyncMock()

    await cog.start()
    await cog.start()
    await asyncio.sleep(0)

    assert cog._check_pending_reminders.await_count == 1
    assert cog._check_and_recover_channel.await_count == 1
