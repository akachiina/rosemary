"""Outage resilience for the bump reminder.

An outage at the exact moment the reminder task fires used to swallow the send
broadly, leaving the pending reminder unrecoverable until a restart. These
tests pin the recovery layers: transient retry in ``send_reminder``, camping
unlock re-booking on success, reconnect recovery, and the minute-loop sweep.
"""

from __future__ import annotations

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

    def __init__(self, storage) -> None:
        self.storage = storage
        self.guilds = []
        self._ready = False

    def get_guild(self, guild_id):
        return None

    async def wait_until_ready(self) -> None:
        self._ready = True


def _make_cog(tmp_path):
    import rosemary.cogs.bump_reminder as mod
    from rosemary.cogs.bump_reminder import BumpReminderCog

    bot = FakeBot(GuildStorage(tmp_path))
    cog = BumpReminderCog.__new__(BumpReminderCog)
    cog.bot = bot
    cog.store = MagicMock()
    cog._reminder_tasks = {}
    cog._unlock_tasks = {}
    cog._recovery_task = None
    cog._schedule_reminder = MagicMock()
    cog._schedule_unlock = MagicMock()
    return cog, mod


def _settings_map(**overrides):
    settings = {
        "bump.enabled": True,
        "bump.cooldown": 7200,
        "bump.channel": 111,
        "bump.anti_camping.enabled": False,
        "bump.anti_camping.unlock_delay": 300,
        "bump.detection_text": "Bump done",
    }
    settings.update(overrides)
    return settings


def _patch_settings(monkeypatch, settings):
    async def fake_get_setting(storage, guild_id, key):
        return settings.get(key)

    monkeypatch.setattr(
        "rosemary.cogs.bump_reminder.get_setting", fake_get_setting
    )


def _ok_channel():
    channel = AsyncMock(spec=discord.TextChannel)
    channel.mention = "<#111>"
    return channel


def _guild_with(channel):
    guild = MagicMock()
    guild.id = 1
    guild.get_channel = MagicMock(return_value=channel)
    return guild


@pytest.fixture
def quiet_logs(monkeypatch):
    send_channel_log = AsyncMock()
    monkeypatch.setattr(
        "rosemary.cogs.bump_reminder.send_channel_log", send_channel_log
    )
    return send_channel_log


@pytest.fixture
def real_cog_factory(tmp_path, monkeypatch, quiet_logs):
    """A cog wired with a real BumpStore and the channel from ``bot.get_guild``."""

    def factory(settings, channel):
        import rosemary.cogs.bump_reminder as mod
        from rosemary.cogs.bump_reminder import BumpReminderCog
        from rosemary.core.bump import BumpStore

        bot = FakeBot(GuildStorage(tmp_path))
        guild = _guild_with(channel)
        bot.get_guild = MagicMock(return_value=guild)
        _patch_settings(monkeypatch, settings)
        cog = BumpReminderCog.__new__(BumpReminderCog)
        cog.bot = bot
        cog.store = BumpStore(bot.storage.data_dir)
        cog._reminder_tasks = {}
        cog._unlock_tasks = {}
        cog._recovery_task = None
        cog._schedule_reminder = MagicMock()
        cog._schedule_unlock = MagicMock()
        cog._build_reminder_view = AsyncMock(return_value=MagicMock())
        return cog, mod

    return factory


# ---------------------------------------------------------------------------
# Transient retry in send_reminder
# ---------------------------------------------------------------------------


async def test_send_reminder_retries_on_oserror_and_succeeds(
    real_cog_factory, monkeypatch
):
    """A transient DNS failure on the first attempt must not lose the reminder."""
    channel = _ok_channel()
    channel.send = AsyncMock(side_effect=[OSError(-2, "Name or service not known"), None])
    cog, mod = real_cog_factory(_settings_map(), channel)
    monkeypatch.setattr(
        mod, "SEND_RETRY_DELAY", 0.0
    )  # no real sleep in tests
    await cog.store.record_bump(1, 123)

    await cog.send_reminder(1)

    assert channel.send.await_count == 2
    assert await cog.store.get_reminder(1) is not None
    state = await cog.store.get_reminder(1)
    assert state["reminder_sent"] is True


async def test_send_reminder_gives_up_after_attempts(real_cog_factory, monkeypatch):
    """All attempts failing keeps the pending state for reconnect recovery."""
    channel = _ok_channel()
    channel.send = AsyncMock(side_effect=OSError(-2, "Name or service not known"))
    cog, mod = real_cog_factory(_settings_map(), channel)
    monkeypatch.setattr(mod, "SEND_RETRY_DELAY", 0.0)
    await cog.store.record_bump(1, 123)

    await cog.send_reminder(1)

    assert channel.send.await_count == 3  # SEND_RETRY_ATTEMPTS
    state = await cog.store.get_reminder(1)
    assert state["reminder_sent"] is False


async def test_send_reminder_no_retry_on_http_exception(real_cog_factory):
    """Discord API errors (400/403/429) are real failures: fail immediately."""
    channel = _ok_channel()
    channel.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "bad request")
    )
    cog, _mod = real_cog_factory(_settings_map(), channel)
    await cog.store.record_bump(1, 123)

    await cog.send_reminder(1)

    assert channel.send.await_count == 1
    state = await cog.store.get_reminder(1)
    assert state["reminder_sent"] is False


async def test_send_reminder_pings_once_when_card_send_retries(
    real_cog_factory, monkeypatch
):
    """A retry must not re-ping the role: the ping message is sent only once."""
    channel = _ok_channel()
    # First call (role ping) succeeds; second (card) fails once, then succeeds.
    channel.send = AsyncMock(
        side_effect=[None, OSError(-2, "boom"), None]
    )
    cog, mod = real_cog_factory(_settings_map(**{"bump.ping_role": 42}), channel)
    monkeypatch.setattr(mod, "SEND_RETRY_DELAY", 0.0)
    await cog.store.record_bump(1, 123)

    await cog.send_reminder(1)

    assert channel.send.await_count == 3
    ping_texts = [
        c.args[0] for c in channel.send.await_args_list if c.args and c.args[0]
    ]
    assert ping_texts == ["<@&42>"]


# ---------------------------------------------------------------------------
# Camping unlock re-booking
# ---------------------------------------------------------------------------


async def test_successful_send_rebooks_lost_camping_unlock(real_cog_factory):
    """Reminder delivered while a camping lock has no live unlock task ->
    unlock is re-booked instead of leaving the channel locked forever."""
    from rosemary.core.bump import LOCK_CAMPING

    channel = _ok_channel()
    cog, _mod = real_cog_factory(_settings_map(), channel)
    await cog.store.record_bump(1, 123)
    await cog.store.mark_channel_locked(1, LOCK_CAMPING)
    # No _unlock_tasks entry for guild 1: the task was lost.
    cog._reminder_tasks.clear()
    cog._reminder_tasks[1] = MagicMock(done=MagicMock(return_value=True))

    await cog.send_reminder(1)

    cog._schedule_unlock.assert_called_once_with(1, 300)


async def test_successful_send_does_not_double_book_unlock(real_cog_factory):
    """A live unlock task is never replaced: no double unlock messages."""
    from rosemary.core.bump import LOCK_CAMPING

    channel = _ok_channel()
    cog, _mod = real_cog_factory(_settings_map(), channel)
    await cog.store.record_bump(1, 123)
    await cog.store.mark_channel_locked(1, LOCK_CAMPING)
    cog._unlock_tasks[1] = MagicMock(done=MagicMock(return_value=False))

    await cog.send_reminder(1)

    cog._schedule_unlock.assert_not_called()


# ---------------------------------------------------------------------------
# Reconnect recovery
# ---------------------------------------------------------------------------


async def test_on_connect_recovers_overdue_reminder(real_cog_factory, monkeypatch):
    """Outage swallowed the send; reconnect must resend the pending reminder."""

    channel = _ok_channel()
    cog, mod2 = real_cog_factory(_settings_map(), channel)
    monkeypatch.setattr(mod2, "RECONNECT_RECOVERY_DELAY", 0.0)
    await cog.store.record_bump(1, 123)
    # Simulate: reminder task died, pending state remains.
    past = datetime.now(UTC) - timedelta(seconds=8000)
    await cog.store._set_reminder(
        1,
        {
            "last_bump_timestamp": past.isoformat(),
            "reminder_sent": False,
        },
    )
    cog._check_pending_reminders = AsyncMock(wraps=cog._check_pending_reminders)
    send_spy = AsyncMock(wraps=cog.send_reminder)
    cog.send_reminder = send_spy
    cog.bot.guilds = [type("G", (), {"id": 1})()]

    await cog.on_connect()

    # Recovery is a delayed task: run it to completion.
    assert cog._recovery_task is not None
    await cog._recovery_task
    cog._check_pending_reminders.assert_any_await(1)
    send_spy.assert_awaited_once_with(1)


async def test_reconnect_recovery_coalesces(real_cog_factory, monkeypatch):
    """A second on_connect while recovery is running must not spawn a task."""
    cog, _mod = real_cog_factory(_settings_map(), _ok_channel())
    cog._reconnect_recovery = AsyncMock()
    await cog._schedule_reconnect_recovery()
    first = cog._recovery_task
    await cog._schedule_reconnect_recovery()
    assert cog._recovery_task is first
    await first


async def test_recover_lost_lock_reschedules_unlock(real_cog_factory, monkeypatch):
    """Camping lock with no live unlock task and reminder pending -> re-arm."""
    from rosemary.core.bump import LOCK_CAMPING

    cog, _mod = real_cog_factory(_settings_map(), _ok_channel())
    past = datetime.now(UTC) - timedelta(seconds=60)
    await cog.store._set_reminder(
        1,
        {
            "last_bump_timestamp": past.isoformat(),
            "last_bump_user_id": 123,
            "reminder_sent": False,
            "channel_locked": True,
            "lock_source": LOCK_CAMPING,
        },
    )

    await cog._recover_lost_lock(1)

    cog._schedule_unlock.assert_called_once()
    delay = cog._schedule_unlock.call_args.args[1]
    assert 0 < delay <= 7200


async def test_recover_lost_lock_unlocks_when_nothing_pending(
    real_cog_factory, monkeypatch
):
    """Camping lock, reminder already sent, no unlock task -> unlock now."""
    from rosemary.core.bump import LOCK_CAMPING

    cog, _mod = real_cog_factory(_settings_map(), _ok_channel())
    await cog.store._set_reminder(
        1,
        {
            "last_bump_timestamp": datetime.now(UTC).isoformat(),
            "last_bump_user_id": 123,
            "reminder_sent": True,
            "channel_locked": True,
            "lock_source": LOCK_CAMPING,
        },
    )
    cog.unlock_channel = AsyncMock()

    await cog._recover_lost_lock(1)

    cog.unlock_channel.assert_awaited_once_with(1)


async def test_recover_lost_lock_skips_schedule_and_live_tasks(
    real_cog_factory, monkeypatch
):
    """Schedule locks and live unlock tasks are not the sweep's business."""
    from rosemary.core.bump import LOCK_CAMPING, LOCK_SCHEDULE

    cog, _mod = real_cog_factory(_settings_map(), _ok_channel())
    await cog.store.mark_channel_locked(1, LOCK_SCHEDULE)
    cog.unlock_channel = AsyncMock()
    await cog._recover_lost_lock(1)
    cog.unlock_channel.assert_not_awaited()
    cog._schedule_unlock.assert_not_called()

    await cog.store.mark_channel_locked(1, LOCK_CAMPING)
    cog._unlock_tasks[1] = MagicMock(done=MagicMock(return_value=False))
    await cog._recover_lost_lock(1)
    cog.unlock_channel.assert_not_awaited()
    cog._schedule_unlock.assert_not_called()


# ---------------------------------------------------------------------------
# Minute-loop sweep
# ---------------------------------------------------------------------------


async def test_sweep_recovers_pending_reminder(real_cog_factory, monkeypatch):
    """Sweep re-checks a pending reminder whose task is gone."""
    cog, _mod = real_cog_factory(_settings_map(), _ok_channel())
    past = datetime.now(UTC) - timedelta(seconds=8000)
    await cog.store._set_reminder(
        1,
        {
            "last_bump_timestamp": past.isoformat(),
            "reminder_sent": False,
        },
    )
    cog._check_pending_reminders = AsyncMock()

    await cog._sweep_guild(1)

    cog._check_pending_reminders.assert_awaited_once_with(1)


async def test_sweep_skips_guild_with_live_reminder_task(real_cog_factory):
    """A live reminder task owns the reminder: the sweep must not race it."""
    cog, _mod = real_cog_factory(_settings_map(), _ok_channel())
    await cog.store._set_reminder(1, {"reminder_sent": False})
    cog._reminder_tasks[1] = MagicMock(done=MagicMock(return_value=False))
    cog._check_pending_reminders = AsyncMock()

    await cog._sweep_guild(1)

    cog._check_pending_reminders.assert_not_awaited()


async def test_sweep_skips_when_reminder_already_sent(real_cog_factory):
    cog, _mod = real_cog_factory(_settings_map(), _ok_channel())
    await cog.store._set_reminder(1, {"reminder_sent": True})
    cog._check_pending_reminders = AsyncMock()

    await cog._sweep_guild(1)

    cog._check_pending_reminders.assert_not_awaited()
