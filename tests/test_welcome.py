"""Welcome/leave/ban announcements (cogs/welcome.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import rosemary.cogs.welcome as welcome_mod
import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.cogs.welcome import WelcomeCog
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore
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


#: join ----------------------------------------------------------------------


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
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir(exist_ok=True)
    (themes_dir / "t.yaml").write_text(
        "name: t\n"
        "cards:\n"
        "  events.welcome:\n"
        "    - type: 10\n"
        "      content: \"BEM-VINDA {user_name}!\"\n",
        encoding="utf-8",
    )
    bot._theme_store = ThemeStore(tmp_path, themes_dir=themes_dir)
    await bot._theme_store.set_active(1, "t")
    bot.guilds = [type("G", (), {"id": 1})()]
    from rosemary.core.themes import preload_themes

    await preload_themes(bot)
    cog = WelcomeCog(bot)
    await cog.on_member_join(member)
    _, kwargs = channel.send.call_args
    assert "BEM-VINDA Ana!" in texts(kwargs["view"])


#: leave / ban ---------------------------------------------------------------


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


#: alert roles (receptionist pings) -------------------------------------------


def make_role(role_id: int) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.mention = f"<@&{role_id}>"
    return role


async def test_join_pings_receptionist_role_before_card(tmp_path):
    bot, guild, member, channel, *_ = make_world(tmp_path)
    role = make_role(999)
    guild.get_role = MagicMock(side_effect=lambda rid: role if rid == 999 else None)
    await set_settings(bot, welcome_channel=111, welcome_ping_role=999)
    cog = WelcomeCog(bot)

    await cog.on_member_join(member)

    assert channel.send.await_count == 2
    ping_args, ping_kwargs = channel.send.call_args_list[0]
    assert ping_args[0] == "<@&999>"
    assert [r.id for r in ping_kwargs["allowed_mentions"].roles] == [999]
    _, card_kwargs = channel.send.call_args_list[1]
    assert "events.welcome.title" in texts(card_kwargs["view"])


async def test_join_without_alert_role_sends_only_card(tmp_path):
    bot, guild, member, channel, *_ = make_world(tmp_path)
    await set_settings(bot, welcome_channel=111)
    cog = WelcomeCog(bot)

    await cog.on_member_join(member)

    assert channel.send.await_count == 1


async def test_deleted_alert_role_is_skipped(tmp_path):
    bot, guild, member, channel, *_ = make_world(tmp_path)
    guild.get_role = MagicMock(return_value=None)
    await set_settings(bot, welcome_channel=111, welcome_ping_role=999)
    cog = WelcomeCog(bot)

    await cog.on_member_join(member)

    assert channel.send.await_count == 1  # card still goes out


async def test_card_pings_muted_in_theme_skips_alert_role(tmp_path):
    bot, guild, member, channel, *_ = make_world(tmp_path)
    role = make_role(999)
    guild.get_role = MagicMock(side_effect=lambda rid: role if rid == 999 else None)
    await set_settings(bot, welcome_channel=111, welcome_ping_role=999)
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir(exist_ok=True)
    (themes_dir / "m.yaml").write_text(
        "name: m\n"
        "pings:\n"
        "  events.welcome: false\n",
        encoding="utf-8",
    )
    bot._theme_store = ThemeStore(tmp_path, themes_dir=themes_dir)
    await bot._theme_store.set_active(1, "m")
    bot.guilds = [type("G", (), {"id": 1})()]
    from rosemary.core.themes import preload_themes

    await preload_themes(bot)
    cog = WelcomeCog(bot)

    await cog.on_member_join(member)

    assert channel.send.await_count == 1  # muted card mutes the role too


async def test_failed_ping_does_not_block_card(tmp_path, caplog):
    bot, guild, member, channel, *_ = make_world(tmp_path)
    role = make_role(999)
    guild.get_role = MagicMock(side_effect=lambda rid: role if rid == 999 else None)
    await set_settings(bot, welcome_channel=111, welcome_ping_role=999)

    real_send = channel.send

    async def flaky(*args, **kwargs):
        if real_send.await_count == 0:
            real_send.side_effect = discord.HTTPException(MagicMock(), "boom")
        return await real_send(*args, **kwargs)

    channel.send = AsyncMock(side_effect=flaky)
    cog = WelcomeCog(bot)

    with caplog.at_level("WARNING"):
        await cog.on_member_join(member)

    assert real_send.await_count == 2  # ping retried? no: ping failed, card sent
    assert any("Alert ping failed" in r.message for r in caplog.records)


async def test_leave_and_ban_ping_their_own_roles(tmp_path):
    bot, guild, member, _, leave_channel, ban_channel = make_world(
        tmp_path, leave=True, ban=True
    )
    leave_role = make_role(888)
    ban_role = make_role(777)
    guild.get_role = MagicMock(
        side_effect=lambda rid: {888: leave_role, 777: ban_role}.get(rid)
    )
    await set_settings(bot, leave_channel=222, ban_channel=333,
                       leave_ping_role=888, ban_ping_role=777)
    cog = WelcomeCog(bot)

    user = MagicMock(spec=discord.User)
    user.id = member.id
    user.name = "Ana"
    user.mention = "<@5>"
    await cog.on_member_ban(guild, user)
    cog._recently_banned.clear()  # ban suppression is pinned elsewhere
    await cog.on_member_remove(member)

    ban_ping, ban_card = ban_channel.send.call_args_list
    assert ban_ping.args[0] == "<@&777>"
    assert [r.id for r in ban_ping.kwargs["allowed_mentions"].roles] == [777]
    leave_ping, leave_card = leave_channel.send.call_args_list
    assert leave_ping.args[0] == "<@&888>"
    assert [r.id for r in leave_ping.kwargs["allowed_mentions"].roles] == [888]


#: raid protection ------------------------------------------------------------


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
