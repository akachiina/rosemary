"""Build the bump leaderboard post flow with fakes to catch regressions.

Covers the manual "Postar Placar" button path: when the weekly leaderboard is
empty the button must still publish the "no data" card to the bump channel
(mirroring the weekly auto-post), instead of sending nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from rosemary.cogs.bump_leaderboard import BumpLeaderboardCog
from rosemary.core.storage import GuildStorage
from rosemary.ui.containers import DesignerView
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


class FakeBot:
    theme = load_theme()
    translator = FakeTranslator()

    def __init__(self, storage) -> None:
        self.storage = storage

    def get_guild(self, guild_id):
        return self._guild


def _make_cog(bot, leaderboard):
    cog = object.__new__(BumpLeaderboardCog)
    cog.bot = bot
    cog.bump_store = MagicMock(get_leaderboard=AsyncMock(return_value=leaderboard))
    return cog


async def test_post_current_leaderboard_empty_publishes_no_bumps_card(tmp_path):
    storage = GuildStorage(tmp_path)
    await storage.set(1, "bump.channel", 111)

    channel = AsyncMock(spec=discord.TextChannel)
    guild = MagicMock()
    guild.id = 1
    guild.get_channel = MagicMock(return_value=channel)

    bot = FakeBot(storage)
    bot._guild = guild

    cog = _make_cog(bot, {})

    posted = await cog.post_current_leaderboard(1)

    assert posted is False
    channel.send.assert_awaited_once()
    view = channel.send.await_args.kwargs["view"]
    assert isinstance(view, DesignerView)
    assert view.children


async def test_post_current_leaderboard_disabled_does_nothing(tmp_path):
    storage = GuildStorage(tmp_path)
    await storage.set(1, "bump.leaderboard.enabled", False)
    channel = AsyncMock(spec=discord.TextChannel)
    guild = MagicMock()
    guild.id = 1
    guild.get_channel = MagicMock(return_value=channel)
    bot = FakeBot(storage)
    bot._guild = guild

    cog = _make_cog(bot, {"123": 1})

    posted = await cog.post_current_leaderboard(1)

    assert posted is False
    channel.send.assert_not_awaited()


async def test_post_current_leaderboard_missing_channel_returns_false(tmp_path):
    storage = GuildStorage(tmp_path)  # bump.channel defaults to None
    guild = MagicMock()
    guild.id = 1
    bot = FakeBot(storage)
    bot._guild = guild

    cog = _make_cog(bot, {})

    posted = await cog.post_current_leaderboard(1)

    assert posted is False


async def test_weekly_post_mentions_winner(tmp_path):
    """The weekly post must visibly mention the winner AND allow the ping:
    the description resolves `{@winner_mention}` and allowed covers the id."""
    from datetime import UTC, datetime
    from pathlib import Path
    from unittest.mock import patch

    import rosemary.core.card_specs  # noqa: F401 (fills the card registry)
    from rosemary.core.i18n import Translator
    from rosemary.core.mentions import allowed_for_ids

    async def resolver(gid):
        return "pt-BR"

    theme = load_theme()
    translator = Translator(
        Path(__file__).resolve().parents[1] / "language",
        resolver=resolver,
        default_placeholders=theme.emojis,
    )
    storage = GuildStorage(tmp_path)
    bot = MagicMock()
    bot.translator = translator
    bot.theme = theme
    bot.storage = storage

    cog = object.__new__(BumpLeaderboardCog)
    cog.bot = bot
    cog.bump_store = MagicMock(
        get_week_start=AsyncMock(return_value=datetime.now(UTC))
    )
    channel = AsyncMock(spec=discord.TextChannel)
    guild = MagicMock()
    guild.id = 1
    guild.get_member = MagicMock(return_value=None)

    with patch("rosemary.core.themes.card_document", AsyncMock(return_value=None)):
        await cog._post_leaderboard(guild, channel, [(111, 5), (222, 3)], 111, 5, "MVP")

    kwargs = channel.send.await_args.kwargs
    texts = [
        item.content
        for item in kwargs["view"].walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]
    assert any("<@111>" in text for text in texts), texts
    allowed = await allowed_for_ids(
        bot, 1, "bump.leaderboard", user_ids=[111]
    )
    assert {obj.id for obj in (allowed.users or [])} == {111}, allowed
