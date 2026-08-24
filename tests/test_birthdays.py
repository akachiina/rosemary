"""Birthdays store, validation and announcement flow."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from rosemary.cogs.birthdays import BirthdayCog
from rosemary.core.birthdays import BirthdaysStore, validate_date
from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        suffix = "".join(f";{k}={v}" for k, v in sorted(variables.items()))
        return f"{key}{suffix}"


class FakeBot:
    theme = load_theme()
    translator = FakeTranslator()

    def __init__(self, tmp_path):
        self.storage = GuildStorage(tmp_path)
        self.guilds = []


def make_guild():
    channel = AsyncMock(spec=discord.TextChannel)
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.name = "Servidor"
    guild.get_channel.return_value = channel
    return guild, channel


def make_member(uid):
    member = MagicMock(spec=discord.Member)
    member.id = uid
    member.mention = f"<@{uid}>"
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


# -- store ----------------------------------------------------------------------


async def test_store_roundtrip_and_year_preserved(tmp_path):
    store = BirthdaysStore(tmp_path)
    await store.set(1, 10, day=5, month=3)
    entry = await store.get(1, 10)
    assert entry["day"] == 5 and entry["month"] == 3

    await store.mark_announced(1, 10, 2026)
    await store.set(1, 10, day=6, month=3)  # change date later
    entry = await store.get(1, 10)
    assert entry["day"] == 6
    assert entry["last_announced_year"] == 2026

    entries = await store.all(1)
    assert list(entries) == [10]
    assert await store.clear(1, 10) is True
    assert await store.get(1, 10) is None


async def test_store_isolated_per_guild(tmp_path):
    store = BirthdaysStore(tmp_path)
    await store.set(1, 10, day=1, month=1)
    assert await store.get(2, 10) is None


# -- validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "month", "expected"),
    [(29, 2, None), (30, 2, "bad_day"), (31, 4, "bad_day"), (15, 13, "bad_month")],
)
def test_validate_date(day, month, expected):
    assert validate_date(day, month) == expected


# -- announcement flow ----------------------------------------------------------


async def seed_settings(bot, *, channel=77, role_id=None):
    from rosemary.core.settings import set_setting

    await set_setting(bot.storage, 1, "birthdays.enabled", True)
    if channel:
        await set_setting(bot.storage, 1, "birthdays.channel", channel)
    if role_id:
        await set_setting(bot.storage, 1, "birthdays.role", role_id)


async def test_check_guild_time_gate(tmp_path):
    bot = FakeBot(tmp_path)
    guild, channel = make_guild()
    bot._guilds = [guild]
    bot.guilds = [guild]
    cog = BirthdayCog(bot)
    await seed_settings(bot)

    due = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    not_due = datetime(2026, 8, 23, 11, 59, tzinfo=UTC)

    # No birthdays yet: runs but sends nothing.
    await cog.check_guild(guild, now=due)
    channel.send.assert_not_awaited()

    await cog.store.set(1, 10, day=23, month=8)
    await cog.check_guild(guild, now=not_due)
    channel.send.assert_not_awaited()

    await cog.check_guild(guild, now=due)
    channel.send.assert_awaited_once()


async def test_announce_posts_card_marks_year_and_grants_role(tmp_path):
    bot = FakeBot(tmp_path)
    guild, channel = make_guild()
    member = make_member(10)
    guild.get_member.return_value = member
    cog = BirthdayCog(bot)
    await seed_settings(bot, role_id=55)

    role = MagicMock()
    role.id = 55
    other = make_member(99)
    role.members = [other]  # stale holder whose birthday isn't today
    guild.get_role.return_value = role

    today = datetime(2026, 8, 23).date()
    await cog.store.set(1, 10, day=23, month=8)
    await cog.announce_today(guild, channel, today)

    channel.send.assert_awaited_once()
    _, kwargs = channel.send.call_args
    assert kwargs["view"] is not None
    member.add_roles.assert_awaited_once()
    other.remove_roles.assert_awaited_once()

    entry = await cog.store.get(1, 10)
    assert entry["last_announced_year"] == 2026

    # Same year again: no duplicate announcement.
    channel.send.reset_mock()
    await cog.announce_today(guild, channel, today)
    channel.send.assert_not_awaited()


async def test_announce_honors_override_document(tmp_path):
    bot = FakeBot(tmp_path)
    guild, channel = make_guild()
    cog = BirthdayCog(bot)
    await seed_settings(bot)

    from rosemary.core.cards import card_store

    await card_store(bot).save_document(
        1,
        "birthdays.announce",
        {"v": 1, "blocks": [{"type": "text", "body": "PARABÉNS {user}!"}]},
    )
    await cog.store.set(1, 10, day=23, month=8)
    await cog.announce_today(guild, channel, datetime(2026, 8, 23).date())

    _, kwargs = channel.send.call_args
    found = []

    def walk(node):
        content = getattr(node, "content", None)
        if isinstance(content, str):
            found.append(content)
        for child in getattr(node, "items", []) or []:
            walk(child)

    for child in kwargs["view"].children:
        walk(child)
    assert any("PARABÉNS <@10>!" in text for text in found)


# -- disabled / missing channel ---------------------------------------------------


async def test_disabled_or_missing_channel_is_silent(tmp_path):
    bot = FakeBot(tmp_path)
    guild, channel = make_guild()
    cog = BirthdayCog(bot)
    await cog.store.set(1, 10, day=23, month=8)

    await cog.check_guild(
        guild, now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    )
    channel.send.assert_not_awaited()

    await seed_settings(bot, channel=None)
    from rosemary.core.settings import set_setting

    await set_setting(bot.storage, 1, "birthdays.enabled", True)
    await set_setting(bot.storage, 1, "birthdays.channel", None)
    await cog.check_guild(guild, now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC))
    channel.send.assert_not_awaited()
