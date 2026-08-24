"""Welcome/leave/ban announcements (cogs/welcome.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import rosemary.cogs.welcome as welcome_mod
from rosemary.cogs.welcome import WelcomeCog
from rosemary.core.cards import card_store
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


def make_world(tmp_path, *, welcome=True, leave=None, ban=None):
    bot = FakeBot(tmp_path)
    welcome_channel = AsyncMock(spec=discord.TextChannel)
    channels = {}
    if welcome:
        channels[111] = welcome_channel
    leave_channel = AsyncMock(spec=discord.TextChannel) if leave else None
    if leave_channel is not None:
        channels[222] = leave_channel
    ban_channel = AsyncMock(spec=discord.TextChannel) if ban else None
    if ban_channel is not None:
        channels[333] = ban_channel

    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.name = "Servidor"
    guild.member_count = 42
    guild.get_channel.side_effect = lambda cid: channels.get(cid)

    member = MagicMock(spec=discord.Member)
    member.id = 5
    member.display_name = "Ana"
    member.mention = "<@5>"
    member.bot = False
    member.guild = guild
    member.display_avatar.url = "https://a.b/ana.png"

    return bot, guild, member, welcome_channel, leave_channel, ban_channel


async def set_settings(bot, **values):
    from rosemary.core.settings import set_setting

    for key, value in values.items():
        await set_setting(bot.storage, 1, f"events.{key}", value)


def texts(view) -> str:
    found: list[str] = []

    def walk(node):
        content = getattr(node, "content", None)
        if isinstance(content, str):
            found.append(content)
        for child in getattr(node, "items", []) or []:
            walk(child)

    for child in getattr(view, "children", []):
        walk(child)
    return "\n".join(found)


# -- join ----------------------------------------------------------------------


@pytest.mark.parametrize("welcome", [True])
async def test_join_sends_default_card(tmp_path, welcome):
    bot, guild, member, channel, *_ = make_world(tmp_path)
    await set_settings(bot, welcome_channel=111)
    cog = WelcomeCog(bot)

    await cog.on_member_join(member)

    assert channel.send.await_count == 1
    _, kwargs = channel.send.call_args
    rendered = texts(kwargs["view"])
    assert "events.welcome.title" in rendered
    assert ";count=42" in rendered or "{user}" not in rendered


async def test_join_without_channel_is_silent(tmp_path):
    bot, guild, member, channel, *_ = make_world(tmp_path, welcome=False)
    await set_settings(bot, welcome_channel=None)
    cog = WelcomeCog(bot)
    await cog.on_member_join(member)
    assert channel.send.await_count == 0


async def test_disabled_events_do_nothing(tmp_path):
    bot, guild, member, channel, *_ = make_world(tmp_path)
    await set_settings(
        bot, welcome_channel=111, enabled=False
    )
    cog = WelcomeCog(bot)
    await cog.on_member_join(member)
    assert channel.send.await_count == 0


async def test_join_uses_plain_override(tmp_path):
    bot, guild, member, channel, *_ = make_world(tmp_path)
    await set_settings(bot, welcome_channel=111)
    await card_store(bot).save_document(
        1,
        "events.welcome",
        {"v": 1, "blocks": [{"type": "text", "body": "BEM-VINDA {user_name}!"}]},
    )
    cog = WelcomeCog(bot)
    await cog.on_member_join(member)
    _, kwargs = channel.send.call_args
    assert "BEM-VINDA Ana!" in texts(kwargs["view"])


# -- leave / ban ---------------------------------------------------------------


async def test_leave_sends_to_leave_channel(tmp_path):
    bot, guild, member, _, leave_channel, _ = make_world(tmp_path, leave=True)
    await set_settings(bot, leave_channel=222)
    cog = WelcomeCog(bot)
    await cog.on_member_remove(member)
    assert leave_channel.send.await_count == 1


async def test_ban_suppresses_following_leave(tmp_path):
    bot, guild, member, _, leave_channel, ban_channel = make_world(
        tmp_path, leave=True, ban=True
    )
    await set_settings(bot, leave_channel=222, ban_channel=333)
    cog = WelcomeCog(bot)

    user = MagicMock(spec=discord.User)
    user.id = member.id
    user.name = "Ana"
    user.mention = "<@5>"
    await cog.on_member_ban(guild, user)
    await cog.on_member_remove(member)

    assert ban_channel.send.await_count == 1
    assert leave_channel.send.await_count == 0


# -- raid protection ------------------------------------------------------------


async def test_raid_protection_delays_welcomes(tmp_path, monkeypatch):
    bot, guild, member, channel, *_ = make_world(tmp_path)
    await set_settings(bot, welcome_channel=111, raid_protection=True)
    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(welcome_mod.asyncio, "sleep", fake_sleep)
    cog = WelcomeCog(bot)
    for _ in range(WelcomeCog.__mro__ and 5):
        await cog.on_member_join(member)
    assert delays and delays[-1] == welcome_mod.RAID_WELCOME_DELAY
