"""Starboard counting, tiers and posting (core/starboard.py + cogs/starboard.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from rosemary.cogs.starboard import StarboardCog
from rosemary.core.starboard import StarboardStore
from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


class FakeBot:
    theme = load_theme()
    translator = FakeTranslator()

    def __init__(self, tmp_path):
        self.storage = GuildStorage(tmp_path)
        self.guilds = []

    def get_guild(self, guild_id):
        return self._guild

    def get_channel(self, channel_id):
        return self._channel_by_id.get(channel_id)


# -- store ----------------------------------------------------------------------


async def test_store_roundtrip(tmp_path):
    store = StarboardStore(tmp_path)
    assert await store.get_entry(1, 10) is None
    await store.upsert(1, 10, post_id=99, stars=3, channel_id=5, author_id=7)
    entry = await store.get_entry(1, 10)
    assert entry == {"post_id": 99, "stars": 3, "channel_id": 5, "author_id": 7}
    await store.update_stars(1, 10, 4)
    assert (await store.get_entry(1, 10))["stars"] == 4
    await store.remove(1, 10)
    assert await store.get_entry(1, 10) is None


async def test_store_isolated_per_guild(tmp_path):
    store = StarboardStore(tmp_path)
    await store.upsert(1, 10, post_id=1, stars=1, channel_id=2, author_id=3)
    assert await store.get_entry(2, 10) is None


# -- counting -------------------------------------------------------------------


def make_reaction(users, *, bots=()):
    entries = []
    for uid in users:
        user = MagicMock(spec=discord.User)
        user.bot = False
        user.id = uid
        entries.append(user)
    for uid in bots:
        bot_user = MagicMock()
        bot_user.bot = True
        bot_user.id = uid
        entries.append(bot_user)

    class Reaction:
        def users(self):
            async def gen():
                for entry in entries:
                    yield entry

            return gen()

    reaction = Reaction()
    return reaction


@pytest.mark.parametrize(
    ("users", "self_star", "author", "expected"),
    [
        ([1000, 2000], False, 1000, 1),
        ([1000], False, 1000, 0),
        ([1000, 2000], True, 1000, 2),
        ([2000], False, 1000, 1),
    ],
)
async def test_effective_stars_rules(users, self_star, author, expected):
    reaction = make_reaction(users)
    result = await StarboardCog._effective_stars(
        reaction, author_id=author, self_star_counts=self_star
    )
    assert result == expected


async def test_effective_stars_filters_bots():
    reaction = make_reaction([1000, 2000], bots=(3000,))
    result = await StarboardCog._effective_stars(
        reaction, author_id=1000, self_star_counts=False
    )
    assert result == 1


# -- tiers ------------------------------------------------------------------------


def test_tier_styles_exist_in_theme():
    theme = load_theme()
    for threshold in (1, 2, 3, 5, 8, 13):
        assert f"star_tier_{threshold}" in theme.styles


def test_tier_style_progression(tmp_path):
    class Bot(FakeBot):
        def __init__(self):
            super().__init__(tmp_path)

    cog = StarboardCog(Bot())
    assert cog._tier_style(1) == "star_tier_1"
    assert cog._tier_style(3) == "star_tier_3"
    assert cog._tier_style(20) == "star_tier_13"
    assert cog._tier_style(0) == "star_tier_1"


# -- posting flow ------------------------------------------------------------------


def make_flow(tmp_path, *, threshold=3):
    bot = FakeBot(tmp_path)
    board = AsyncMock(spec=discord.TextChannel)

    async def fetch_message(message_id):
        return board_post

    board.fetch_message.side_effect = fetch_message
    board_post = AsyncMock()
    board_post.id = 999
    board.send.return_value = board_post

    channel = AsyncMock(spec=discord.TextChannel)
    channel.id = 5
    channel.guild = MagicMock(spec=discord.Guild)
    channel.guild.id = 1
    channel.guild.get_channel.return_value = board

    message = MagicMock(spec=discord.Message)
    message.id = 10
    message.author.id = 7
    message.author.display_avatar.url = "https://a.b/x.png"
    message.content = "hello"
    message.attachments = []
    message.channel = channel
    message.jump_url = "https://discord.com/jump"

    bot._guild = channel.guild
    config_channel = 42
    bot._channel_by_id = {config_channel: board}

    from rosemary.core.settings import set_setting

    async def seed():
        await set_setting(bot.storage, 1, "starboard.enabled", True)
        await set_setting(bot.storage, 1, "starboard.channel", config_channel)
        await set_setting(bot.storage, 1, "starboard.threshold", threshold)

    cog = StarboardCog(bot)
    cog.bot = bot
    return cog, bot, channel, board, board_post, message, seed


async def test_threshold_reached_posts_once(tmp_path):
    cog, bot, channel, board, board_post, message, seed = make_flow(tmp_path)
    await seed()

    await cog._update_starboard(channel, message, 3)
    board.send.assert_awaited_once()
    entry = await cog.store.get_entry(1, 10)
    assert entry["post_id"] == 999 and entry["stars"] == 3

    await cog._update_starboard(channel, message, 4)
    board_post.edit.assert_awaited()


async def test_below_threshold_does_not_post(tmp_path):
    cog, bot, channel, board, _post, message, seed = make_flow(tmp_path)
    await seed()
    await cog._update_starboard(channel, message, 2)
    board.send.assert_not_awaited()
    assert await cog.store.get_entry(1, 10) is None


async def test_zero_stars_deletes_post(tmp_path):
    cog, bot, channel, board, board_post, message, seed = make_flow(tmp_path)
    await seed()
    await cog.store.upsert(1, 10, post_id=999, stars=3, channel_id=5, author_id=7)
    partial = AsyncMock()
    partial.delete = AsyncMock()
    board.get_partial_message.return_value = partial

    await cog._update_starboard(channel, message, 0)

    partial.delete.assert_awaited()
    assert await cog.store.get_entry(1, 10) is None
