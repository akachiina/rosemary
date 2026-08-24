"""Tests for ``rosemary.cogs.moderation`` (TimeParser, WarningsStore, helpers)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import discord

from rosemary.cogs.moderation import (
    ModerationCog,
    TimeParser,
    WarningsStore,
    _has_permissions,
)
from rosemary.core.storage import GuildStorage
from rosemary.core.time_parser import DEFAULT_ALIASES, localized_aliases


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
