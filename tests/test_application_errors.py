"""Failed application commands must answer the interaction, not time out.

py-cord's default handler only prints the traceback; without a response the
invoker sees "The application did not respond" after three seconds. Rosemary's
handler logs and sends a themed notice through ``context.respond`` (which uses
the followup webhook when the command already deferred).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from discord.ext import commands

from rosemary.bot import RosemaryBot


class _Translator:
    def __init__(self) -> None:
        self.calls: list[tuple[int | None, str]] = []

    async def t(self, guild_id, key, **variables):
        self.calls.append((guild_id, key))
        return f"text:{key}"


def _context(responded: list[dict]) -> SimpleNamespace:
    async def respond(**kwargs):
        responded.append(kwargs)

    return SimpleNamespace(
        command="boost",
        guild=SimpleNamespace(id=7),
        respond=respond,
    )


async def test_application_command_error_answers_the_invoker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rosemary.ui.responses as responses

    bot = RosemaryBot.__new__(RosemaryBot)
    bot.translator = _Translator()
    responded: list[dict] = []
    context = _context(responded)
    sentinel = object()

    async def fake_notice(bot_arg, guild_id, text, *, color):
        assert bot_arg is bot
        assert guild_id == 7
        assert text == "text:general.unknown_error"
        assert color == "danger"
        return sentinel

    monkeypatch.setattr(responses, "notice_view", fake_notice)
    await bot.on_application_command_error(context, RuntimeError("boom"))

    assert responded == [{"view": sentinel, "ephemeral": True}]
    assert bot.translator.calls == [(7, "general.unknown_error")]


async def test_check_failure_answers_with_no_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rosemary.ui.responses as responses

    bot = RosemaryBot.__new__(RosemaryBot)
    bot.translator = _Translator()
    responded: list[dict] = []
    context = _context(responded)
    sentinel = object()

    async def fake_notice(bot_arg, guild_id, text, *, color):
        assert text == "text:general.no_permission"
        assert color == "warning"
        return sentinel

    monkeypatch.setattr(responses, "notice_view", fake_notice)
    await bot.on_application_command_error(context, commands.CheckFailure("nope"))

    assert responded == [{"view": sentinel, "ephemeral": True}]
    assert bot.translator.calls == [(7, "general.no_permission")]


async def test_error_handler_without_translator_only_logs() -> None:
    """Boot-time failures (cogs still loading) must not raise from the handler."""
    bot = RosemaryBot.__new__(RosemaryBot)
    responded: list[dict] = []
    context = _context(responded)

    await bot.on_application_command_error(context, RuntimeError("boom"))

    assert responded == []
