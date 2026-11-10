"""The command surface: one policy for the ACK, the errors and the visibility.

``core/command_surface.py`` is what every command answers through: it ACKs
before the slow work, it turns every failure into a translated notice (never
a traceback for the member) and it decides whether the answer is ephemeral.

These tests pin that policy with fakes that mirror the live contexts (a slash
context, a prefix context and the prefix facade).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import discord
import pytest
from discord.ext import commands

from rosemary.core.command_surface import (
    BOT_NO_PERMISSION,
    NO_PERMISSION,
    Surface,
    ephemeral_for,
    is_application,
    run,
)
from rosemary.ui.responses import PrefixContext
from rosemary.ui.theme import load_theme


class FakeTranslator:
    def __init__(self) -> None:
        self.calls: list[tuple[int | None, str]] = []

    async def t(self, guild_id, key, **variables):
        self.calls.append((guild_id, key))
        return f"{key}!"


class FakeBot:
    def __init__(self) -> None:
        self.translator = FakeTranslator()
        self.theme = load_theme()


class FakeResponse:
    def __init__(self) -> None:
        self.done = False

    def is_done(self) -> bool:
        return self.done


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content, **kwargs):
        self.sent.append({"content": content, **kwargs})


class FakeSlashCtx:
    """The shape of an application command context (a duck-typed fake)."""

    def __init__(self, *, permissions=None, done: bool = False) -> None:
        self.guild_id = 42
        self.bot = FakeBot()
        self.author = SimpleNamespace(guild_permissions=permissions)
        self.response = FakeResponse()
        self.response.done = done
        self.followup = FakeFollowup()
        self.deferred: list[bool] = []
        self.responded: list[dict] = []

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred.append(ephemeral)
        self.response.done = True

    async def respond(self, content, **kwargs) -> None:
        self.responded.append({"content": content, **kwargs})


class FakePrefixCtx:
    """The shape of a prefix command context."""

    def __init__(self, *, permissions=None) -> None:
        self.guild_id = 42
        self.bot = FakeBot()
        self.author = SimpleNamespace(guild_permissions=permissions)
        self.replies: list[dict] = []
        self.sent: list[dict] = []

    async def reply(self, content, **kwargs) -> None:
        self.replies.append({"content": content, **kwargs})

    async def send(self, content, **kwargs) -> None:
        self.sent.append({"content": content, **kwargs})


def _permissions(**flags):
    return SimpleNamespace(**flags)


async def test_the_handler_runs_and_nothing_is_answered():
    ctx = FakeSlashCtx()
    called: list[str] = []

    async def handler() -> None:
        called.append("yes")

    await run(ctx, Surface(card_key="family.error"), handler)
    assert called == ["yes"]
    assert ctx.responded == []
    assert ctx.bot.translator.calls == []


async def test_the_surface_defers_before_the_slow_work():
    ctx = FakeSlashCtx()
    order: list[str] = []

    async def handler() -> None:
        order.append("handler")

    original_defer = ctx.defer

    async def defer(*, ephemeral: bool = False) -> None:
        order.append("defer")
        await original_defer(ephemeral=ephemeral)

    ctx.defer = defer
    await run(ctx, Surface(card_key="family.error"), handler)
    assert order == ["defer", "handler"]
    assert ctx.deferred == [True]


async def test_a_surface_that_answers_with_a_panel_never_defers():
    ctx = FakeSlashCtx()

    async def handler() -> None:
        return None

    await run(ctx, Surface(card_key="family.error", defer=False), handler)
    assert ctx.deferred == []


async def test_a_missing_permission_stops_the_command():
    ctx = FakeSlashCtx(permissions=_permissions(ban_members=False))
    called: list[str] = []

    async def handler() -> None:
        called.append("yes")

    await run(
        ctx,
        Surface(card_key="family.error", permission="ban_members"),
        handler,
    )
    assert called == []
    assert ctx.bot.translator.calls == [(42, NO_PERMISSION)]
    (content,) = [item["content"] for item in ctx.responded]
    assert content.endswith(f"{NO_PERMISSION}!")
    # The notice never notifies.
    mentions = ctx.responded[0]["allowed_mentions"]
    assert mentions.everyone is False
    assert not mentions.users
    assert not mentions.roles


async def test_the_permission_is_granted_when_the_invoker_has_it():
    ctx = FakeSlashCtx(permissions=_permissions(ban_members=True))
    called: list[str] = []

    async def handler() -> None:
        called.append("yes")

    await run(
        ctx,
        Surface(card_key="family.error", permission="ban_members"),
        handler,
    )
    assert called == ["yes"]
    assert ctx.bot.translator.calls == []


async def test_an_unexpected_failure_answers_the_family_copy(caplog):
    ctx = FakeSlashCtx()

    async def handler() -> None:
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="rosemary.core.command_surface"):
        await run(ctx, Surface(card_key="family.error"), handler)
    assert ctx.bot.translator.calls == [(42, "family.error")]
    # The command already deferred: the answer rides the follow-up.
    assert ctx.followup.sent and ctx.followup.sent[0]["content"].endswith("family.error!")
    assert any("boom" in caplog.text for record in caplog.records)


async def test_a_forbidden_call_answers_the_bot_copy():
    ctx = FakeSlashCtx()

    async def handler() -> None:
        raise discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"message": "Missing Permissions", "code": 50013},
        )

    await run(ctx, Surface(card_key="family.error"), handler)
    assert ctx.bot.translator.calls == [(42, BOT_NO_PERMISSION)]


async def test_a_check_failure_answers_the_permission_copy():
    ctx = FakeSlashCtx()

    async def handler() -> None:
        raise commands.CheckFailure("nope")

    await run(ctx, Surface(card_key="family.error"), handler)
    assert ctx.bot.translator.calls == [(42, NO_PERMISSION)]


async def test_the_answer_after_a_defer_goes_through_the_followup():
    ctx = FakeSlashCtx()

    async def handler() -> None:
        raise RuntimeError("boom")

    await run(ctx, Surface(card_key="family.error"), handler)
    assert ctx.responded == []
    assert ctx.followup.sent
    assert ctx.followup.sent[0]["content"].endswith("family.error!")


async def test_a_prefix_context_answers_publicly():
    ctx = FakePrefixCtx()

    async def handler() -> None:
        raise RuntimeError("boom")

    await run(ctx, Surface(card_key="family.error"), handler)
    assert ctx.sent
    assert ctx.sent[0]["content"].endswith("family.error!")
    assert ctx.sent[0]["allowed_mentions"].everyone is False


async def test_the_prefix_facade_is_not_an_interaction():
    ctx = PrefixContext(FakePrefixCtx())
    assert is_application(ctx) is False
    assert ephemeral_for(ctx, Surface(card_key="family.error")) is False

    called: list[str] = []

    async def handler() -> None:
        called.append("yes")

    await run(ctx, Surface(card_key="family.error"), handler)
    assert called == ["yes"]
    assert ctx._ctx.replies == []


async def test_a_slash_context_is_an_interaction():
    ctx = FakeSlashCtx()
    assert is_application(ctx) is True
    assert ephemeral_for(ctx, Surface(card_key="family.error")) is True


async def test_the_surface_can_force_the_visibility():
    ctx = FakeSlashCtx()
    assert ephemeral_for(ctx, Surface(card_key="x", ephemeral=False)) is False
    prefix = FakePrefixCtx()
    assert ephemeral_for(prefix, Surface(card_key="x", ephemeral=True)) is True


async def test_the_moderation_family_goes_through_the_surface(monkeypatch, tmp_path):
    """The moderation commands answer through the surface, not by hand."""
    import rosemary.cogs.moderation as moderation_mod
    from rosemary.core.storage import GuildStorage

    seen: list[str] = []

    async def spy(ctx, surface, handler):
        seen.append(surface.card_key)
        await handler()

    monkeypatch.setattr(moderation_mod, "run", spy)
    cog = moderation_mod.ModerationCog(SimpleNamespace(storage=GuildStorage(tmp_path)))
    calls: list[str] = []

    async def fake_moderate(ctx, action, targets, reason, duration=None):
        calls.append(action)

    cog._moderate = fake_moderate
    await moderation_mod.ModerationCog.ban.callback(
        cog, SimpleNamespace(), SimpleNamespace(), "spam"
    )
    assert seen == ["moderation.error_failed"]
    assert calls == ["ban"]


@pytest.mark.parametrize("name", ["ban", "kick", "warn"])
async def test_every_moderation_command_uses_the_surface(name):
    """The three commands share one body: the surface call and the flow."""
    import rosemary.cogs.moderation as moderation_mod

    bodies = {
        command: getattr(moderation_mod.ModerationCog, command).callback
        for command in ("ban", "kick", "warn")
    }
    codes = {command: body.__code__.co_code for command, body in bodies.items()}
    assert len(set(codes.values())) == 1
