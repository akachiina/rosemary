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
