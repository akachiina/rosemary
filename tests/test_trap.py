"""Trap channel: silent deletion plus the configured kick/softban/ban.

Locks the honeypot contract: non-staff members posting in the configured
channel lose the message and receive ``trap.action`` (native Discord purge
window for ban/softban, hierarchy checked for kick/softban), staff and bots
are exempt, the DM is best-effort and toggleable, and the audit card fires
only when ``audit.trap_enabled`` is on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from rosemary.cogs.trap import DELETE_WINDOWS, TrapCog
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore
from rosemary.ui.theme import load_theme


def _bot(tmp_path):
    bot = MagicMock(spec=[
        "storage", "get_cog", "get_guild", "translator", "theme", "_theme_store",
    ])
    bot.storage = GuildStorage(tmp_path)
    bot.get_cog = MagicMock(return_value=None)
    bot.get_guild = MagicMock(return_value=None)
    bot.translator = MagicMock()
    bot.translator.t = AsyncMock(side_effect=lambda gid, key, **kw: key)
    # The DM override path resolves the guild theme (catalog fallback here).
    bot.theme = load_theme()
    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    return bot


def _guild(tmp_path, *, manage_roles_hierarchy=True):
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.name = "Girassol"
    me = MagicMock(spec=discord.Member)
    me.top_role = 100
    me.guild_permissions = discord.Permissions(
        ban_members=True, kick_members=True, manage_messages=True,
    )
    guild.me = me
    guild.get_channel = MagicMock(return_value=None)
    return guild


def _member(*, staff=False, top_role=10):
    member = MagicMock(spec=discord.Member)
    member.id = 100
    member.name = "caiu"
    member.bot = False
    member.top_role = top_role
    member.guild_permissions = discord.Permissions(manage_messages=staff)
    member.send = AsyncMock()
    return member


def _message(guild, member, channel_id=55):
    message = MagicMock(spec=discord.Message)
    message.guild = guild
    message.author = member
    message.channel = MagicMock(spec=discord.TextChannel)
    message.channel.id = channel_id
    message.delete = AsyncMock()
    return message


async def test_trap_message_is_deleted_and_victim_banned(tmp_path):
    """Ban action: native purge window rides the ban; the DM goes out."""
    bot = _bot(tmp_path)
    cog = TrapCog(bot)
    await bot.storage.set(1, "trap.enabled", True)
    await bot.storage.set(1, "trap.channel", 55)
    await bot.storage.set(1, "trap.action", "ban")
    await bot.storage.set(1, "trap.delete_window", "24h")
    guild = _guild(tmp_path)
    bot.get_guild.return_value = guild
    guild.ban = AsyncMock()
    member = _member()
    message = _message(guild, member)

    await cog.on_message(message)

    message.delete.assert_awaited_once()
    guild.ban.assert_awaited_once_with(
        member, reason="trap.reason", delete_message_seconds=86400
    )
    member.send.assert_awaited_once()


async def test_trap_softban_bans_then_unbans(tmp_path):
    """Softban: ban with purge followed by an immediate unban."""
    bot = _bot(tmp_path)
    cog = TrapCog(bot)
    await bot.storage.set(1, "trap.enabled", True)
    await bot.storage.set(1, "trap.channel", 55)
    await bot.storage.set(1, "trap.action", "softban")
    await bot.storage.set(1, "trap.delete_window", "7d")
    guild = _guild(tmp_path)
    bot.get_guild.return_value = guild
    guild.ban = AsyncMock()
    guild.unban = AsyncMock()
    member = _member()
    message = _message(guild, member)

    await cog.on_message(message)

    guild.ban.assert_awaited_once_with(
        member, reason="trap.reason", delete_message_seconds=604800
    )
    guild.unban.assert_awaited_once()
    member.send.assert_awaited_once()


async def test_trap_softban_keeps_victim_when_unban_fails(tmp_path):
    """A failed unban is honest reporting: no applied DM, no success."""
    bot = _bot(tmp_path)
    cog = TrapCog(bot)
    await bot.storage.set(1, "trap.enabled", True)
    await bot.storage.set(1, "trap.channel", 55)
    await bot.storage.set(1, "trap.action", "softban")
    guild = _guild(tmp_path)
    bot.get_guild.return_value = guild
    guild.ban = AsyncMock()
    guild.unban = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no"))
    member = _member()
    message = _message(guild, member)

    await cog.on_message(message)

    guild.ban.assert_awaited_once()
    member.send.assert_not_called()


async def test_trap_kick_checks_hierarchy_and_no_purge(tmp_path):
    """Kick: expelled without any message purge; hierarchy blocks safely."""
    bot = _bot(tmp_path)
    cog = TrapCog(bot)
    await bot.storage.set(1, "trap.enabled", True)
    await bot.storage.set(1, "trap.channel", 55)
    await bot.storage.set(1, "trap.action", "kick")
    guild = _guild(tmp_path)
    bot.get_guild.return_value = guild
    guild.ban = AsyncMock()
    member = _member(top_role=50)
    message = _message(guild, member)

    await cog.on_message(message)

    # py-cord kicks via Member.kick, not Guild.kick.
    member.kick.assert_awaited_once_with(reason="trap.reason")
    guild.ban.assert_not_called()
    member.send.assert_awaited_once()

    # Above the bot's top role: no kick at all (and no ban fallback).
    high = _member(top_role=200)
    high.kick = AsyncMock()
    high.send = AsyncMock()
    await cog.on_message(_message(guild, high))
    high.kick.assert_not_called()


async def test_trap_none_action_only_deletes(tmp_path):
    """'Only delete' mode: the message dies and nothing else happens."""
    bot = _bot(tmp_path)
    cog = TrapCog(bot)
    await bot.storage.set(1, "trap.enabled", True)
    await bot.storage.set(1, "trap.channel", 55)
    await bot.storage.set(1, "trap.action", "none")
    guild = _guild(tmp_path)
    bot.get_guild.return_value = guild
    guild.ban = AsyncMock()
    guild.kick = AsyncMock()
    member = _member()
    message = _message(guild, member)

    await cog.on_message(message)

    message.delete.assert_awaited_once()
    guild.ban.assert_not_called()
    guild.kick.assert_not_called()
    member.send.assert_not_called()


async def test_trap_skips_staff_and_bots_and_other_channels(tmp_path):
    """Staff with manage_messages, bots and other channels are all exempt."""
    bot = _bot(tmp_path)
    cog = TrapCog(bot)
    await bot.storage.set(1, "trap.enabled", True)
    await bot.storage.set(1, "trap.channel", 55)
    await bot.storage.set(1, "trap.action", "ban")
    guild = _guild(tmp_path)
    bot.get_guild.return_value = guild
    guild.ban = AsyncMock()

    staff = _member(staff=True)
    await cog.on_message(_message(guild, staff))
    staff.send.assert_not_called()
    guild.ban.assert_not_called()

    bot_author = _member()
    bot_author.bot = True
    await cog.on_message(_message(guild, bot_author))
    guild.ban.assert_not_called()

    outsider = _member()
    await cog.on_message(_message(guild, outsider, channel_id=99))
    outsider.send.assert_not_called()
    guild.ban.assert_not_called()

    # Disabled feature: nothing happens even in the trap channel.
    await bot.storage.set(1, "trap.enabled", False)
    victim = _member()
    await cog.on_message(_message(guild, victim))
    victim.send.assert_not_called()
    guild.ban.assert_not_called()


async def test_trap_dm_is_toggleable(tmp_path):
    """trap.dm_enabled off: the punishment runs without any DM."""
    bot = _bot(tmp_path)
    cog = TrapCog(bot)
    await bot.storage.set(1, "trap.enabled", True)
    await bot.storage.set(1, "trap.channel", 55)
    await bot.storage.set(1, "trap.action", "kick")
    await bot.storage.set(1, "trap.dm_enabled", False)
    guild = _guild(tmp_path)
    bot.get_guild.return_value = guild
    member = _member()
    member.kick = AsyncMock()

    await cog.on_message(_message(guild, member))

    member.kick.assert_awaited_once()
    member.send.assert_not_called()


async def test_trap_fires_the_audit_card_when_enabled(tmp_path):
    """audit.trap_enabled on: one audit card with the outcome."""
    bot = _bot(tmp_path)
    cog = TrapCog(bot)
    await bot.storage.set(1, "trap.enabled", True)
    await bot.storage.set(1, "trap.channel", 55)
    await bot.storage.set(1, "trap.action", "kick")
    guild = _guild(tmp_path)
    bot.get_guild.return_value = guild

    # A REAL AuditCog instance: the cog validates with isinstance (cleaner
    # precedent), so a bare MagicMock would be silently skipped.
    from rosemary.cogs.audit import AuditCog

    audit = AuditCog(bot)
    audit._channel = AsyncMock(return_value=MagicMock(spec=discord.TextChannel))
    audit._on = AsyncMock(return_value=True)
    audit._send = AsyncMock()
    bot.get_cog.return_value = audit

    member = _member()
    await cog.on_message(_message(guild, member))

    audit._send.assert_awaited_once()
    channel, guild_id, key, variables = audit._send.await_args.args
    assert key == "trap"
    assert variables["action"] == "trap.audit.action.kick"
    assert variables["outcome"] == "trap.audit.applied"

    # Toggle off: no card at all.
    audit._on = AsyncMock(return_value=False)
    await cog.on_message(_message(guild, _member()))
    assert audit._send.await_count == 1


def test_delete_windows_cover_every_choice():
    """The choice values and the native seconds map 1:1."""
    assert DELETE_WINDOWS == {"none": 0, "1h": 3600, "24h": 86400, "7d": 604800}
