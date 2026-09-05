"""Tests for ``rosemary.cogs.moderation`` (TimeParser, WarningsStore, helpers)."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from rosemary.cogs.moderation import (
    ModerationCog,
    TimeParser,
    WarningsStore,
    _has_permissions,
)
from rosemary.core.i18n import Translator
from rosemary.core.storage import GuildStorage
from rosemary.core.time_parser import DEFAULT_ALIASES, localized_aliases
from rosemary.ui.theme import load_theme

ROOT = Path(__file__).resolve().parents[2]
LANG_DIR = ROOT / "rosemary" / "language"


def test_time_parser_valid():
    assert TimeParser.parse("10s") == timedelta(seconds=10)
    assert TimeParser.parse("5m") == timedelta(minutes=5)
    assert TimeParser.parse("2h") == timedelta(hours=2)
    assert TimeParser.parse("3d") == timedelta(days=3)


def test_time_parser_localized_words():
    """Full-word units work only when the matching aliases are provided."""
    pt_words = {
        "s": {"segundo", "segundos"},
        "m": {"minuto", "minutos"},
        "h": {"hora", "horas"},
        "d": {"dia", "dias"},
    }
    pt = {unit: set(words) | pt_words[unit] for unit, words in DEFAULT_ALIASES.items()}
    assert TimeParser.parse("2 horas", aliases=pt) == timedelta(hours=2)
    assert TimeParser.parse("45 segundos", aliases=pt) == timedelta(seconds=45)
    assert TimeParser.parse("3 dias", aliases=pt) == timedelta(days=3)
    assert TimeParser.parse("5 minutos", aliases=pt) == timedelta(minutes=5)
    assert TimeParser.parse("90m", aliases=pt) == timedelta(minutes=90)


def test_time_parser_full_words_require_aliases():
    """Without localized aliases, full words no longer parse (no hardcoded locale)."""
    assert TimeParser.parse("2 horas") is None
    assert TimeParser.parse("2 hours") is None


class _FakeCatalogTranslator:
    def __init__(self, words):
        self.words = words

    async def t(self, guild_id, key, **variables):
        return self.words[key]


async def test_localized_aliases_ptbr():
    translator = _FakeCatalogTranslator(
        {
            "time_parser.seconds": ["segundo", "segundos"],
            "time_parser.minutes": ["minuto", "minutos"],
            "time_parser.hours": ["hora", "horas"],
            "time_parser.days": ["dia", "dias"],
        }
    )
    aliases = await localized_aliases(translator, 1)
    assert TimeParser.parse("2 horas", aliases=aliases) == timedelta(hours=2)
    assert TimeParser.parse("45 segundos", aliases=aliases) == timedelta(seconds=45)
    assert TimeParser.parse("3 dias", aliases=aliases) == timedelta(days=3)
    assert TimeParser.parse("90m", aliases=aliases) == timedelta(minutes=90)


async def test_localized_aliases_enus():
    translator = _FakeCatalogTranslator(
        {
            "time_parser.seconds": ["second", "seconds"],
            "time_parser.minutes": ["minute", "minutes"],
            "time_parser.hours": ["hour", "hours"],
            "time_parser.days": ["day", "days"],
        }
    )
    aliases = await localized_aliases(translator, 1)
    assert TimeParser.parse("2 hours", aliases=aliases) == timedelta(hours=2)
    assert TimeParser.parse("45 seconds", aliases=aliases) == timedelta(seconds=45)
    assert TimeParser.parse("3 days", aliases=aliases) == timedelta(days=3)


def test_time_parser_invalid():
    assert TimeParser.parse("banana") is None
    assert TimeParser.parse("-5m") is None
    assert TimeParser.parse("") is None
    assert TimeParser.parse("5") is None


def test_format_duration():
    assert TimeParser.format_duration(timedelta(days=1, hours=1)) == "1d 1h"
    assert TimeParser.format_duration(timedelta(hours=1, minutes=30)) == "1h 30m"
    assert TimeParser.format_duration(timedelta(minutes=1, seconds=5)) == "1m"
    assert TimeParser.format_duration(timedelta(seconds=5)) == "5s"
    assert TimeParser.format_duration(timedelta(seconds=0)) == "0s"


async def test_warnings_store_roundtrip(tmp_path):
    store = WarningsStore(GuildStorage(tmp_path))
    assert await store.get_warnings(1, 100) == []

    assert await store.add_warning(1, 100, "Spam", 200, "Mod") == 1
    assert await store.add_warning(1, 100, "Flood", 200, "Mod") == 2

    warnings = await store.get_warnings(1, 100)
    assert warnings[0]["reason"] == "Spam"
    assert warnings[1]["moderator_id"] == 200
    assert warnings[1]["moderator_tag"] == "Mod"
    assert "timestamp" in warnings[0]

    assert await store.get_warnings(2, 100) == []
    assert await store.get_warnings(1, 999) == []


async def test_warnings_store_remove(tmp_path):
    store = WarningsStore(GuildStorage(tmp_path))
    await store.add_warning(1, 100, "A", 200, "Mod")
    await store.add_warning(1, 100, "B", 200, "Mod")

    assert await store.remove_warning(1, 100, 0) is True
    assert [w["reason"] for w in await store.get_warnings(1, 100)] == ["B"]
    assert await store.remove_warning(1, 100, 5) is False
    assert await store.remove_warning(1, 100, -1) is False


async def test_warnings_store_clear(tmp_path):
    store = WarningsStore(GuildStorage(tmp_path))
    await store.add_warning(1, 100, "A", 200, "Mod")
    await store.add_warning(1, 100, "B", 200, "Mod")

    assert await store.clear_warnings(1, 100) == 2
    assert await store.get_warnings(1, 100) == []
    assert await store.clear_warnings(1, 100) == 0


def test_has_permissions():
    perms = discord.Permissions(ban_members=True, kick_members=True)
    assert _has_permissions(perms, ("ban_members",))
    assert _has_permissions(perms, ("ban_members", "kick_members"))
    assert not _has_permissions(perms, ("moderate_members",))
    assert _has_permissions(perms, ())


class _Role:
    def __init__(self, position: int) -> None:
        self.position = position

    def __ge__(self, other: _Role) -> bool:
        return self.position >= other.position


class _Member:
    def __init__(self, member_id: int, role: _Role, permissions=None) -> None:
        self.id = member_id
        self.top_role = role
        self.guild_permissions = permissions or discord.Permissions(0)


class _Guild:
    def __init__(self, owner_id: int, me: _Member) -> None:
        self.owner_id = owner_id
        self.me = me


class _Ctx:
    def __init__(self, guild_id: int, guild: _Guild, author: _Member) -> None:
        self.guild_id = guild_id
        self.guild = guild
        self.author = author


def _make_cog(tmp_path) -> ModerationCog:
    return ModerationCog(SimpleNamespace(storage=GuildStorage(tmp_path)))


async def test_can_moderate_owner_bypasses_role_hierarchy(tmp_path):
    cog = _make_cog(tmp_path)
    bot_member = _Member(3, _Role(5), discord.Permissions(ban_members=True))
    owner = _Member(1, _Role(0))  # owner with no roles -> @everyone position 0
    ctx = _Ctx(1, _Guild(owner_id=1, me=bot_member), owner)
    target = _Member(2, _Role(1))  # common member with a base role
    assert await cog._can_moderate(ctx, target, "ban") == (True, "")


async def test_can_moderate_blocks_higher_role_for_non_owner(tmp_path):
    cog = _make_cog(tmp_path)
    bot_member = _Member(3, _Role(10), discord.Permissions(ban_members=True))
    author = _Member(1, _Role(2), discord.Permissions(ban_members=True))
    ctx = _Ctx(1, _Guild(owner_id=9, me=bot_member), author)
    target = _Member(2, _Role(5))
    assert await cog._can_moderate(ctx, target, "ban") == (
        False,
        "moderation.error_hierarchy",
    )


async def test_can_moderate_blocks_bot_hierarchy(tmp_path):
    cog = _make_cog(tmp_path)
    bot_member = _Member(3, _Role(1), discord.Permissions(ban_members=True))
    author = _Member(1, _Role(10), discord.Permissions(ban_members=True))
    ctx = _Ctx(1, _Guild(owner_id=9, me=bot_member), author)
    target = _Member(2, _Role(5))
    assert await cog._can_moderate(ctx, target, "ban") == (
        False,
        "moderation.error_bot_hierarchy",
    )


# -- _execute_action regression: duration=None + format placeholders ---------


class _ActionMember:
    """Fake member supporting every moderation side effect."""

    def __init__(self, member_id: int) -> None:
        self.id = member_id
        self.mention = f"<@{member_id}>"
        self.sent: list[str] = []
        self.banned = False
        self.kicked = False
        self.timed_out = False

    def __str__(self) -> str:
        return f"User{self.id}"

    async def send(self, content: str) -> None:
        self.sent.append(content)

    async def ban(self, reason: str | None = None) -> None:
        self.banned = True

    async def kick(self, reason: str | None = None) -> None:
        self.kicked = True

    async def timeout_for(self, duration, reason: str | None = None) -> None:
        assert duration is not None
        self.timed_out = True


def _resolver(language: str):
    async def resolve(guild_id):
        return language

    return resolve


def _real_cog(tmp_path, language: str) -> ModerationCog:
    """Cog wired with the real theme + catalogs, like the running bot."""
    theme = load_theme()
    translator = Translator(
        LANG_DIR, resolver=_resolver(language), default_placeholders=theme.emojis
    )
    bot = SimpleNamespace(
        storage=GuildStorage(tmp_path),
        translator=translator,
        theme=theme,
    )
    return ModerationCog(bot)


def _no_format_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if "Failed to format translation" in record.getMessage()
    ]


@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
@pytest.mark.parametrize("action", ["ban", "kick", "mute", "warn"])
@pytest.mark.parametrize("notify", [True, False])
async def test_execute_action_never_crashes_and_formats(
    tmp_path, caplog, language: str, action: str, notify: bool
) -> None:
    """Every action x notify combo must run without errors or raw placeholders.

    Regression for: ban/kick/warn with notify crashing on
    ``format_duration(None)``, and ``mute.success`` missing ``{duration}``.
    """
    cog = _real_cog(tmp_path, language)
    moderator = _ActionMember(100)
    target = _ActionMember(200)
    duration = timedelta(minutes=10) if action == "mute" else None

    with caplog.at_level(logging.WARNING, logger="rosemary.core.i18n"):
        result = await cog._execute_action(
            1, "TestGuild", moderator, target, action, "Spam", duration, notify
        )

    assert _no_format_warnings(caplog) == []
    assert "{" not in result and "}" not in result
    assert target.mention in result
    if notify:
        assert len(target.sent) == 1
        assert "{" not in target.sent[0] and "}" not in target.sent[0]
    else:
        assert target.sent == []

    assert target.banned is (action == "ban")
    assert target.kicked is (action == "kick")
    assert target.timed_out is (action == "mute")


@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
async def test_mute_success_includes_duration(tmp_path, language: str) -> None:
    """The mute result must render the duration instead of a raw placeholder."""
    cog = _real_cog(tmp_path, language)
    result = await cog._execute_action(
        1,
        "TestGuild",
        _ActionMember(100),
        _ActionMember(200),
        "mute",
        "Spam",
        timedelta(minutes=10),
        False,
    )
    assert "10m" in result


@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
@pytest.mark.parametrize("auto_ban", [True, False])
async def test_warn_limit_auto_ban(tmp_path, language: str, auto_ban: bool) -> None:
    """Reaching the warn limit bans only when auto-ban is enabled."""
    cog = _real_cog(tmp_path, language)
    await cog.bot.storage.set(1, "moderation.warn_limit", 2)
    await cog.bot.storage.set(1, "moderation.auto_ban_enabled", auto_ban)
    target = _ActionMember(200)
    await cog._execute_action(1, "G", _ActionMember(100), target, "warn", "Spam", None, False)
    assert target.banned is False
    result = await cog._execute_action(
        1, "G", _ActionMember(100), target, "warn", "Spam", None, False
    )
    assert target.banned is auto_ban
    assert "{" not in result
