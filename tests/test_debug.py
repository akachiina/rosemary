"""Tests for ``rosemary.core.debug`` (uptime, state summary, log helper)."""

from __future__ import annotations

import io
import time
from unittest.mock import MagicMock

import discord

from rosemary.core.debug import bot_state_summary, format_uptime, send_log


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


class _Translator:
    async def t(self, guild_id, key, **kwargs):
        return key


def _bot(data: dict, channel=None):
    class Bot:
        storage = _FakeStorage(data)
        translator = _Translator()

        def get_channel(self, channel_id):
            return channel

    return Bot()


async def test_send_log_disabled_by_default():
    assert await send_log(_bot({}), 1, "debug.logs.test") is False


async def test_send_log_no_op_without_channel():
    assert await send_log(
        _bot({"logging.enabled": True}), 1, "debug.logs.test"
    ) is False


async def test_send_log_resolves_unusable_channel():
    assert await send_log(
        _bot({"logging.enabled": True, "logging.channel": 999}),
        1,
        "debug.logs.test",
    ) is False


async def test_send_log_rejects_unknown_event():
    assert await send_log(
        _bot({"logging.enabled": True, "logging.channel": 999}),
        1,
        "no.such.event",
    ) is False


async def test_send_log_injects_actor_labels_and_body_variant(monkeypatch):
    import rosemary.core.debug as debug_mod

    channel = MagicMock(spec=discord.TextChannel)
    bot = _bot({"logging.enabled": True, "logging.channel": 999}, channel)
    captured: dict = {}

    async def fake_render(got_bot, guild_id, event, variables):
        captured.update({"event": event, **variables})
        return object(), discord.AllowedMentions.none()

    async def fake_send(got_channel, payload, allowed, **kwargs):
        captured["channel"] = got_channel
        captured["payload"] = payload
        captured["kwargs"] = kwargs

    monkeypatch.setattr(debug_mod, "render_card_message", fake_render)
    monkeypatch.setattr(debug_mod, "send_card", fake_send)

    sent = await send_log(
        bot,
        1,
        "moderation.logs.ban",
        member="<@2>",
        member_id="2",
        moderator="<@3>",
        reason="spam",
        body_key="moderation.logs.ban.description",
    )

    assert sent is True
    assert captured["event"] == "moderation.logs.ban"
    assert captured["actor"] == "<@3>"
    assert captured["member_label"] == "log.field.target"
    assert captured["body_key"] == "moderation.logs.ban.description"
    assert captured["channel"] is channel
    assert "timestamp" in captured
    assert captured["kwargs"] == {}


async def test_send_log_attaches_file(monkeypatch):
    import rosemary.core.debug as debug_mod

    channel = MagicMock(spec=discord.TextChannel)
    bot = _bot({"logging.enabled": True, "logging.channel": 999}, channel)
    file = discord.File(fp=io.BytesIO(b"x"), filename="transcript.txt")
    sent_kwargs: dict = {}

    async def fake_render(got_bot, guild_id, event, variables):
        return object(), discord.AllowedMentions.none()

    async def fake_send(got_channel, payload, allowed, **kwargs):
        sent_kwargs.update(kwargs)

    monkeypatch.setattr(debug_mod, "render_card_message", fake_render)
    monkeypatch.setattr(debug_mod, "send_card", fake_send)

    assert await send_log(bot, 1, "tickets.logs.transcript", file=file) is True
    assert sent_kwargs["file"] is file
    file.close()
