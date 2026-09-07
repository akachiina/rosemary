"""Fixed open/close schedule for the bump channel.

Covers the pure time-range helper (including overnight ranges) and the
transition-only enforcement in ``BumpReminderCog.check_schedule``, plus the
rule that the schedule wins over anti-camping unlocks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import discord

from rosemary.core.bump import LOCK_CAMPING, LOCK_SCHEDULE, schedule_open
from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 5, hour, minute, tzinfo=UTC)


def test_schedule_open_same_day_range():
    assert schedule_open("06:00", "23:00", 6, 0) is True
    assert schedule_open("06:00", "23:00", 12, 30) is True
    assert schedule_open("06:00", "23:00", 22, 59) is True
    assert schedule_open("06:00", "23:00", 23, 0) is False
    assert schedule_open("06:00", "23:00", 5, 59) is False
    assert schedule_open("06:00", "23:00", 0, 0) is False


def test_schedule_open_overnight_range():
    assert schedule_open("22:00", "08:00", 22, 0) is True
    assert schedule_open("22:00", "08:00", 3, 0) is True
    assert schedule_open("22:00", "08:00", 7, 59) is True
    assert schedule_open("22:00", "08:00", 8, 0) is False
    assert schedule_open("22:00", "08:00", 12, 0) is False
    assert schedule_open("22:00", "08:00", 21, 59) is False


def test_schedule_open_invalid_or_equal_means_open():
    assert schedule_open("bogus", "23:00", 3, 0) is True
    assert schedule_open("06:00", None, 3, 0) is True
    assert schedule_open("06:00", "06:00", 3, 0) is True
    assert schedule_open("25:00", "23:00", 3, 0) is True


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


def _bot(tmp_path):
    class Bot:
        theme = load_theme()
        translator = FakeTranslator()

        def __init__(self):
            self.storage = GuildStorage(tmp_path)

    return Bot()


def _guild(bot, tmp_path, **settings):
    channel = AsyncMock(spec=discord.TextChannel)
    channel.mention = "<#111>"
    guild = MagicMock()
    guild.id = 1
    guild.get_channel = MagicMock(return_value=channel)
    bot.get_guild = MagicMock(return_value=guild)
    return guild, channel


async def _cog(bot, settings):
    import rosemary.cogs.bump_reminder as mod
    from rosemary.cogs.bump_reminder import BumpReminderCog
    from rosemary.core.bump import BumpStore

    for key, value in settings.items():
        await bot.storage.set(1, key, value)
    cog = BumpReminderCog.__new__(BumpReminderCog)
    cog.bot = bot
    cog.store = BumpStore(bot.storage.data_dir)
    cog._reminder_tasks = {}
    cog._unlock_tasks = {}
    mod.send_channel_log = AsyncMock()
    return cog, mod


async def test_close_transition_locks_and_posts(tmp_path):
    bot = _bot(tmp_path)
    guild, channel = _guild(bot, tmp_path)
    cog, mod = await _cog(
        bot,
        {
            "bump.enabled": True,
            "bump.channel": 111,
            "bump.schedule.enabled": True,
            "bump.schedule.open_time": "06:00",
            "bump.schedule.close_time": "23:00",
            "bump.schedule.open_message": "open!",
            "bump.schedule.close_message": "closed!",
        },
    )
    try:
        await cog.check_schedule(guild, now=at(23, 30))
    finally:
        pass
    assert await cog.store.is_channel_locked(1) is True
    assert await cog.store.get_lock_source(1) == LOCK_SCHEDULE
    assert channel.edit.await_count == 1
    assert channel.send.await_count == 1
    assert mod.send_channel_log.await_count == 1
    assert mod.send_channel_log.call_args.kwargs.get("card_key") == (
        "bump.logs.schedule_closed.description"
    )


async def test_no_repeat_message_while_closed(tmp_path):
    bot = _bot(tmp_path)
    guild, channel = _guild(bot, tmp_path)
    cog, mod = await _cog(
        bot,
        {
            "bump.enabled": True,
            "bump.channel": 111,
            "bump.schedule.enabled": True,
            "bump.schedule.open_time": "06:00",
            "bump.schedule.close_time": "23:00",
            "bump.schedule.open_message": "open!",
            "bump.schedule.close_message": "closed!",
        },
    )
    await cog.check_schedule(guild, now=at(23, 30))
    channel.edit.reset_mock()
    channel.send.reset_mock()
    await cog.check_schedule(guild, now=at(23, 31))
    assert channel.edit.await_count == 0
    assert channel.send.await_count == 0


async def test_open_transition_unlocks_schedule_lock(tmp_path):
    bot = _bot(tmp_path)
    guild, channel = _guild(bot, tmp_path)
    cog, mod = await _cog(
        bot,
        {
            "bump.enabled": True,
            "bump.channel": 111,
            "bump.schedule.enabled": True,
            "bump.schedule.open_time": "06:00",
            "bump.schedule.close_time": "23:00",
            "bump.schedule.open_message": "open!",
            "bump.schedule.close_message": "closed!",
        },
    )
    await cog.store.mark_channel_locked(1, LOCK_SCHEDULE)
    await cog.check_schedule(guild, now=at(7, 0))
    assert await cog.store.is_channel_locked(1) is False
    assert channel.send.await_count == 1
    assert mod.send_channel_log.call_args.kwargs.get("card_key") == (
        "bump.logs.schedule_opened.description"
    )


async def test_camping_lock_untouched_during_open_hours(tmp_path):
    bot = _bot(tmp_path)
    guild, channel = _guild(bot, tmp_path)
    cog, _mod = await _cog(
        bot,
        {
            "bump.enabled": True,
            "bump.channel": 111,
            "bump.schedule.enabled": True,
            "bump.schedule.open_time": "06:00",
            "bump.schedule.close_time": "23:00",
            "bump.schedule.open_message": "open!",
            "bump.schedule.close_message": "closed!",
        },
    )
    await cog.store.mark_channel_locked(1, LOCK_CAMPING)
    await cog.check_schedule(guild, now=at(12, 0))
    assert await cog.store.is_channel_locked(1) is True
    assert await cog.store.get_lock_source(1) == LOCK_CAMPING
    assert channel.edit.await_count == 0


async def test_disabled_schedule_is_noop(tmp_path):
    bot = _bot(tmp_path)
    guild, channel = _guild(bot, tmp_path)
    cog, _mod = await _cog(
        bot,
        {"bump.enabled": True, "bump.channel": 111, "bump.schedule.enabled": False},
    )
    await cog.check_schedule(guild, now=at(3, 0))
    assert await cog.store.is_channel_locked(1) is False
    assert channel.edit.await_count == 0


async def test_camping_unlock_yields_to_closed_schedule(tmp_path):
    """Anti-camping must not open the channel during scheduled closed hours."""
    bot = _bot(tmp_path)
    guild, channel = _guild(bot, tmp_path)
    cog, _mod = await _cog(
        bot,
        {
            "bump.enabled": True,
            "bump.channel": 111,
            "bump.schedule.enabled": True,
            "bump.schedule.open_time": "06:00",
            "bump.schedule.close_time": "23:00",
            "bump.schedule.open_message": "open!",
            "bump.schedule.close_message": "closed!",
        },
    )
    await cog.store.mark_channel_locked(1, LOCK_CAMPING)
    real_datetime = datetime

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 1, 5, 23, 30, tzinfo=UTC)

    import rosemary.cogs.bump_reminder as mod

    orig_datetime = mod.datetime
    mod.datetime = _FrozenDatetime
    try:
        await cog.unlock_channel(1)
    finally:
        mod.datetime = orig_datetime
    assert await cog.store.is_channel_locked(1) is True
    assert await cog.store.get_lock_source(1) == LOCK_SCHEDULE
    assert channel.edit.await_count == 0


def _lang_bot(tmp_path, lang):
    """Bot wired to the real catalogs so defaults resolve per language."""
    from pathlib import Path

    from rosemary.core.i18n import Translator

    theme = load_theme()

    async def resolve(guild_id):
        return lang

    class Bot:
        translator = Translator(
            Path.cwd() / "language",
            resolver=resolve,
            default_placeholders=theme.emojis,
        )

        def __init__(self):
            self.storage = GuildStorage(tmp_path)

    Bot.theme = theme
    return Bot()


async def test_open_default_localized_pt_with_ping_role(tmp_path):
    """pt-BR guilds without customization get the PT default, not English."""
    import rosemary.core.card_specs  # noqa: F401  (fills the card registry)

    bot = _lang_bot(tmp_path, "pt-BR")
    cog, _mod = await _cog(bot, {"bump.ping_role": 222})
    ping_role, ping_role_id = await cog._ping_role_mention(1)
    assert (ping_role, ping_role_id) == ("<@&222>", 222)
    message = await cog._localized_message(
        1,
        "bump.schedule.open_message",
        "bump.schedule.open_message_default",
        {"ping_role": ping_role},
    )
    assert "O canal de bump está aberto!" in message
    assert "<@&222>" in message
    assert "The bump channel is open" not in message


async def test_open_default_localized_en(tmp_path):
    bot = _lang_bot(tmp_path, "en-US")
    cog, _mod = await _cog(bot, {})
    ping_role, ping_role_id = await cog._ping_role_mention(1)
    assert (ping_role, ping_role_id) == ("", None)
    message = await cog._localized_message(
        1,
        "bump.schedule.open_message",
        "bump.schedule.open_message_default",
        {"ping_role": ping_role},
    )
    assert "The bump channel is open!" in message
    assert "<@&" not in message


async def test_custom_message_is_preserved(tmp_path):
    """A guild that customized the text keeps it verbatim (zero migration)."""
    bot = _lang_bot(tmp_path, "pt-BR")
    cog, _mod = await _cog(bot, {"bump.schedule.open_message": "custom {ping_role}!"})
    message = await cog._localized_message(
        1,
        "bump.schedule.open_message",
        "bump.schedule.open_message_default",
        {"ping_role": "<@&222>"},
    )
    assert message == "custom <@&222>!"


async def test_close_and_anti_camping_defaults_localized(tmp_path):
    bot = _lang_bot(tmp_path, "pt-BR")
    cog, _mod = await _cog(bot, {})
    close = await cog._localized_message(
        1, "bump.schedule.close_message", "bump.schedule.close_message_default"
    )
    assert "fechado" in close
    anti = await cog._localized_message(
        1, "bump.anti_camping.message", "bump.anti_camping.message_default"
    )
    assert "bloqueado temporariamente" in anti


async def test_open_send_pings_role_by_default(tmp_path):
    """The open post actually pings the ping role (policy 'role')."""
    import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
    from rosemary.core.mentions import spec_default

    assert spec_default("bump.schedule.open") == "role"
    bot = _lang_bot(tmp_path, "en-US")
    guild, channel = _guild(bot, tmp_path)
    cog, _mod = await _cog(
        bot,
        {
            "bump.enabled": True,
            "bump.channel": 111,
            "bump.schedule.enabled": True,
            "bump.schedule.open_time": "06:00",
            "bump.schedule.close_time": "23:00",
            "bump.ping_role": 222,
        },
    )
    await cog.store.mark_channel_locked(1, LOCK_SCHEDULE)
    await cog.check_schedule(guild, now=at(7, 0))
    assert channel.send.await_count == 1
    sent_text = channel.send.call_args.args[0]
    assert "<@&222>" in sent_text
    allowed = channel.send.call_args.kwargs.get("allowed_mentions")
    assert [r.id for r in (allowed.roles or [])] == [222]
