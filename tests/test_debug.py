"""Tests for ``rosemary.core.debug`` (uptime, state summary, log helper)."""

from __future__ import annotations

import time

from rosemary.core.debug import bot_state_summary, format_uptime, send_channel_log


def test_format_uptime_none():
    assert format_uptime(None) == "-"


def test_format_uptime_units():
    assert format_uptime(time.monotonic() - 90_000) == "1d 1h 0m"
    assert format_uptime(time.monotonic() - 3_661) == "1h 1m"
    assert format_uptime(time.monotonic() - 65) == "1m 5s"
    assert format_uptime(time.monotonic() - 5) == "5s"


class _FakeGuild:
    def __init__(self, member_count: int) -> None:
        self.member_count = member_count


class _FakeBot:
    guilds = [_FakeGuild(10), _FakeGuild(5)]
    latency = 0.5
    start_time = None


def test_bot_state_summary():
    summary = dict(bot_state_summary(_FakeBot()))
    assert summary["debug.guilds"] == {"count": 2}
    assert summary["debug.members"] == {"count": 15}
    assert summary["debug.latency"] == {"ms": "500"}
    assert summary["debug.uptime"] == {"uptime": "-"}


class _FakeStorage:
    def __init__(self, data: dict) -> None:
        self.data = data

    async def get(self, guild_id: int) -> dict:
        return dict(self.data)


async def test_send_channel_log_disabled_by_default():
    class Bot:
        storage = _FakeStorage({})

    assert await send_channel_log(Bot(), 1, "T", "D") is False


async def test_send_channel_log_no_op_without_channel():
    class Bot:
        storage = _FakeStorage({"logging.enabled": True})

        def get_channel(self, channel_id: int):
            return None

    assert await send_channel_log(Bot(), 1, "T", "D") is False


async def test_send_channel_log_resolves_unusable_channel():
    class Bot:
        storage = _FakeStorage({"logging.enabled": True, "logging.channel": 999})

        def get_channel(self, channel_id: int):
            return None

    assert await send_channel_log(Bot(), 1, "T", "D") is False
