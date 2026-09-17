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


#: store ----------------------------------------------------------------------


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


#: counting -------------------------------------------------------------------


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


#: tiers ------------------------------------------------------------------------


def test_tier_styles_exist_in_theme():
    theme = load_theme()
    for threshold in (1, 2, 3, 5, 8, 13):
        assert f"star_tier_{threshold}" in theme.styles


def test_star_ramp_is_continuous_and_monotonic():
    """The legacy feel: every star nudges the tone toward the strong end."""
    theme = load_theme()
    ramp = theme.star_ramp_config()
    assert ramp is not None, "built-in theme must define the star ramp"
    from_color, to_color, limit = ramp
    below = [theme.star_ramp(n) for n in range(1, limit + 1)]
    assert below[0] == from_color, "one star starts at the weak end"
    assert below[-1] == to_color, "the limit reaches the strong end"
    assert len(set(below)) == len(below), "tone must change every star up to the limit"
    assert theme.star_ramp(limit + 5) == to_color, "the ramp holds past the limit"


def test_star_ramp_without_config_falls_back_to_tier_style(tmp_path):
    """Guild themes without a ramp keep the tier-style look working."""
    from rosemary.cogs.starboard import _ramp_color

    theme = load_theme()
    theme.styles.pop("star_ramp", None)
    assert theme.star_ramp_config() is None
    assert _ramp_color(theme, 1) == theme.color("warning")


def test_star_title_emoji_milestones():
    theme = load_theme()
    assert theme.star_title_emoji(1) == theme.emoji("star")
    assert theme.star_title_emoji(3) != theme.star_title_emoji(1)
    assert theme.star_title_emoji(12) == theme.star_title_emoji(8)
    assert theme.star_title_emoji(13) != theme.star_title_emoji(12)
    assert theme.star_title_emoji(99) == theme.star_title_emoji(13)


#: posting flow ------------------------------------------------------------------


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


#: default builder ---------------------------------------------------------------


async def test_default_document_is_full_rich_layout(tmp_path):
    """The seed {title}/{body} used to shadow the rich card; the builder wins now."""
    from rosemary.core.card_service import default_document, render_document

    bot = FakeBot(tmp_path)
    doc = await default_document(
        bot, 1, "starboard.card",
        {"stars": 3, "image_url": "https://a.b/cat.png", "timestamp": "<t:1:R>"},
    )
    assert doc is not None
    view = await render_document(
        bot,
        doc,
        {
            "user": "<@7>",
            "user_avatar": "https://a.b/x.png",
            "stars": 3,
            "title": "⭐ 3 • #geral",
            "body": "miau",
            "image_url": "https://a.b/cat.png",
            "timestamp": "<t:1:R>",
        },
        guild_id=1,
        card_key="starboard.card",
    )
    texts = [
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]
    assert any("⭐ 3 • #geral" in text for text in texts), texts
    assert any("-# by <@7>" in text for text in texts), texts
    galleries = [i for i in view.walk_children() if isinstance(i, discord.ui.MediaGallery)]
    assert len(galleries) == 1


async def test_default_document_skips_gallery_without_image(tmp_path):
    """No image attachment means no empty gallery skeleton in the message."""
    from rosemary.core.card_service import default_document, render_document

    bot = FakeBot(tmp_path)
    doc = await default_document(bot, 1, "starboard.card", {"stars": 2})
    view = await render_document(
        bot,
        doc,
        {
            "user": "<@7>",
            "user_avatar": "https://a.b/x.png",
            "stars": 2,
            "title": "⭐ 2 • #geral",
            "body": "só texto",
            "image_url": "",
            "timestamp": "<t:1:R>",
        },
        guild_id=1,
        card_key="starboard.card",
    )
    assert not any(isinstance(i, discord.ui.MediaGallery) for i in view.walk_children())


async def test_builder_reacts_to_star_tier(tmp_path):
    """The document carries hex strings (schema contract) and the ramp moves."""
    from rosemary.core.card_service import default_document

    bot = FakeBot(tmp_path)
    low = await default_document(bot, 1, "starboard.card", {"stars": 1})
    mid = await default_document(bot, 1, "starboard.card", {"stars": 5})
    high = await default_document(bot, 1, "starboard.card", {"stars": 13})
    low_c, mid_c, high_c = (
        doc["blocks"][0]["color"] for doc in (low, mid, high)
    )
    for value in (low_c, mid_c, high_c):
        assert isinstance(value, str) and value.startswith("#"), value
    assert low_c != mid_c != high_c, "ramp must move between 1/5/13 stars"
    # Monotonic toward the strong end: red channel only rises (yellow->orange).
    assert int(low_c[1:3], 16) <= int(mid_c[1:3], 16) <= int(high_c[1:3], 16)
