"""Failed application commands must answer the interaction, not time out.

py-cord's default handler only prints the traceback; without a response the
invoker sees "The application did not respond" after three seconds. Rosemary's
handler logs and sends a themed notice through ``context.respond`` (which uses
the followup webhook when the command already deferred).
"""

from __future__ import annotations

from types import SimpleNamespace

from discord.ext import commands

from rosemary.bot import RosemaryBot


class _Translator:
    def __init__(self) -> None:
        self.calls: list[tuple[int | None, str]] = []

    async def t(self, guild_id, key, **variables):
        self.calls.append((guild_id, key))
        return f"text:{key}"


def _context(responded: list[dict]) -> SimpleNamespace:
    async def respond(*args, **kwargs):
        responded.append({"args": args, **kwargs})

    return SimpleNamespace(
        command="boost",
        guild=SimpleNamespace(id=7),
        respond=respond,
    )


async def test_application_command_error_answers_the_invoker() -> None:
    bot = RosemaryBot.__new__(RosemaryBot)
    bot.translator = _Translator()
    responded: list[dict] = []
    context = _context(responded)

    await bot.on_application_command_error(context, RuntimeError("boom"))

    # The answer is one notice line (the theme prefix plus the copy).
    assert len(responded) == 1
    assert responded[0]["ephemeral"] is True
    (content,) = responded[0]["args"]
    assert content.endswith("text:general.unknown_error")
    assert bot.translator.calls == [(7, "general.unknown_error")]


async def test_check_failure_answers_with_no_permission() -> None:
    bot = RosemaryBot.__new__(RosemaryBot)
    bot.translator = _Translator()
    responded: list[dict] = []
    context = _context(responded)

    await bot.on_application_command_error(context, commands.CheckFailure("nope"))

    assert len(responded) == 1
    assert responded[0]["ephemeral"] is True
    (content,) = responded[0]["args"]
    assert content.endswith("text:general.no_permission")
    assert bot.translator.calls == [(7, "general.no_permission")]


async def test_error_handler_without_translator_only_logs() -> None:
    """Boot-time failures (cogs still loading) must not raise from the handler."""
    bot = RosemaryBot.__new__(RosemaryBot)
    responded: list[dict] = []
    context = _context(responded)

    await bot.on_application_command_error(context, RuntimeError("boom"))

    assert responded == []
