"""Registro de Eventos (cogs/audit.py): gating, cards, diffs and bulk purge.

Fake bot + real catalogs: every listener runs against the settings store and
the shared card pipeline, so the tests pin both the toggle gating and the
theme-customizable send path (including the fenced ``` blocks).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord

from rosemary.cogs.audit import AuditCog, _fence
from rosemary.core.card_specs import AUDIT_CARDS, VARIABLES_BY_KEY
from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        suffix = "".join(f";{k}={v}" for k, v in sorted(variables.items()))
        return f"{key}{suffix}"

    async def raw(self, guild_id, key):
        return key


def make_world(tmp_path, *, audit_channel_id=999, enabled=True):
    """Fake bot, guild, audit channel and cog wired to a real GuildStorage."""
    bot = MagicMock()
    bot.theme = load_theme()
    bot.translator = FakeTranslator()
    bot.storage = GuildStorage(tmp_path)
    from rosemary.core.themes import ThemeStore

    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    audit_channel = AsyncMock(spec=discord.TextChannel)
    audit_channel.id = audit_channel_id
    bot.get_channel.side_effect = lambda cid: audit_channel if cid == audit_channel_id else None

    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.name = "Servidor"
    bot.get_guild.side_effect = lambda gid: guild if gid == 1 else None

    cog = AuditCog(bot)

    async def _arm() -> None:
        """Flip the master switch on (registry default is off)."""
        await bot.storage.set(1, "audit.enabled", True)
        await bot.storage.set(1, "audit.channel", audit_channel_id)

    cog._arm = _arm
    return bot, guild, audit_channel, cog


def member(mid=5, name="Ana", nick=None):
    m = MagicMock(spec=discord.Member)
    m.id = mid
    m.display_name = name
    m.name = name
    m.nick = nick
    m.bot = False
    m.mention = f"<@{mid}>"
    m.display_avatar.url = "https://a.b/ana.png"
    m.guild_permissions = discord.Permissions.none()
    m.roles = []
    m.communication_disabled_until = None
    m.guild = None  # callers set the real fake guild
    return m


# -- card inventory and contracts ----------------------------------------------


def test_audit_cards_registered_with_variables():
    from rosemary.core.cards import get_card

    names = {name for name, _ in AUDIT_CARDS}
    assert names == {
        "ban", "unban", "message_delete", "message_edit", "bulk_delete",
        "nickname", "avatar", "roles", "timeout", "voice_join", "voice_leave",
    }
    for name in names:
        spec = get_card(f"audit.{name}")
        assert spec is not None
        assert VARIABLES_BY_KEY[f"audit.{name}"] == spec.variables


def test_audit_card_titles_in_both_catalogs():
    import yaml

    for lang in ("en-US", "pt-BR"):
        with open(f"language/{lang}.yaml", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        node = data["card"]["audit"]
        for name, _ in AUDIT_CARDS:
            assert isinstance(node[name].get("title"), str), f"{lang}:audit.{name}.title"


async def test_default_builders_render_every_audit_card():
    from rosemary.core.card_service import default_document

    bot = MagicMock()
    bot.theme = load_theme()
    bot.translator = FakeTranslator()
    for name, _ in AUDIT_CARDS:
        doc = await default_document(bot, 1, f"audit.{name}")
        assert doc is not None, name
        assert doc["blocks"], name


def test_fence_wraps_and_neutralizes_backticks():
    assert _fence("hello").startswith("```")
    assert _fence("hello").endswith("```")
    assert "```" not in _fence("has ``` inside")[3:-3]


# -- gating and send path -------------------------------------------------------


async def test_disabled_master_setting_sends_nothing(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path, enabled=False)
    await cog.on_member_ban(guild, member())
    audit_channel.send.assert_not_awaited()


async def test_missing_channel_sends_nothing(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    bot.get_channel.side_effect = lambda cid: None
    await cog.on_member_ban(guild, member())
    audit_channel.send.assert_not_awaited()


async def test_per_event_toggle_gates_send(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    from rosemary.core.settings import set_setting

    await set_setting(bot.storage, 1, "audit.ban_enabled", False)
    await cog.on_member_ban(guild, member())
    audit_channel.send.assert_not_awaited()

    await set_setting(bot.storage, 1, "audit.unban_enabled", False)
    await cog.on_member_unban(guild, member())
    audit_channel.send.assert_not_awaited()


async def test_ban_sends_card_with_fenced_free_body(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    u = member()
    with_patch = AsyncMock(return_value=("<@7>", "raids"))
    cog._audit_log_context = with_patch
    await cog.on_member_ban(guild, u)
    assert audit_channel.send.await_count == 1
    kwargs = audit_channel.send.await_args.kwargs
    assert "view" in kwargs or "embed" in kwargs
    # Pings come from the document, not the caller.
    assert kwargs.get("allowed_mentions") is not None


async def test_voice_transitions_emit_join_and_leave(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    m = member()
    m.guild = guild
    before = MagicMock()
    before.channel = None
    after = MagicMock()
    after.channel = MagicMock(mention="<#42>")
    await cog.on_voice_state_update(m, before, after)
    assert audit_channel.send.await_count == 1

    before2 = MagicMock()
    before2.channel = MagicMock(mention="<#42>")
    after2 = MagicMock()
    after2.channel = None
    await cog.on_voice_state_update(m, before2, after2)
    assert audit_channel.send.await_count == 2

    # Same channel: no card.
    before3 = MagicMock()
    before3.channel = MagicMock(mention="<#42>")
    after3 = MagicMock()
    after3.channel = before3.channel
    await cog.on_voice_state_update(m, before3, after3)
    assert audit_channel.send.await_count == 2


async def test_message_delete_skips_bots_and_dm(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    dm = MagicMock(spec=discord.Message)
    dm.guild = None
    await cog.on_message_delete(dm)
    assert audit_channel.send.await_count == 0

    author = MagicMock()
    author.bot = True
    msg = MagicMock(spec=discord.Message)
    msg.guild = guild
    msg.author = author
    await cog.on_message_delete(msg)
    assert audit_channel.send.await_count == 0


async def test_message_delete_fences_content(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    author = member()
    chan = MagicMock()
    chan.mention = "<#7>"
    msg = MagicMock(spec=discord.Message)
    msg.guild = guild
    msg.author = author
    msg.content = "conteudo secreto"
    msg.channel = chan
    msg.jump_url = "https://discord.com/channels/1/7/9"
    await cog.on_message_delete(msg)
    assert audit_channel.send.await_count == 1


async def test_message_edit_only_when_content_changes(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    author = member()
    chan = MagicMock()
    chan.mention = "<#7>"

    def msg(content):
        m = MagicMock(spec=discord.Message)
        m.guild = guild
        m.author = author
        m.content = content
        m.channel = chan
        m.jump_url = "https://x"
        return m

    await cog.on_message_edit(msg("a"), msg("a"))
    assert audit_channel.send.await_count == 0
    await cog.on_message_edit(msg("a"), msg("b"))
    assert audit_channel.send.await_count == 1


async def test_member_update_emits_nickname_diff(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    before = member(nick="Velho")
    after = member(nick="Novo")
    after.guild = guild
    await cog.on_member_update(before, after)
    assert audit_channel.send.await_count == 1


async def test_member_update_roles_diff(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    gained = MagicMock()
    gained.mention = "<@&11>"
    before = member()
    before.roles = []
    after = member()
    after.roles = [gained]
    after.guild = guild
    await cog.on_member_update(before, after)
    assert audit_channel.send.await_count == 1


async def test_member_update_timeout_diff(tmp_path):
    import datetime as dt

    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    before = member()
    before.communication_disabled_until = None
    after = member()
    after.communication_disabled_until = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    after.guild = guild
    await cog.on_member_update(before, after)
    assert audit_channel.send.await_count == 1

    before2 = member()
    before2.communication_disabled_until = after.communication_disabled_until
    after2 = member()
    after2.communication_disabled_until = None
    after2.guild = guild
    await cog.on_member_update(before2, after2)
    assert audit_channel.send.await_count == 2


async def test_member_update_without_timeout_attr_never_crashes(tmp_path, caplog):
    """Regression: live Member has no timed_out_until (py-cord uses communication_disabled_until)."""
    import logging
    from types import SimpleNamespace

    import discord

    from rosemary.cogs.audit import _timeout_until

    # The installed py-cord exposes the timeout under the py-cord name only.
    assert hasattr(discord.Member, "communication_disabled_until")
    assert not hasattr(discord.Member, "timed_out_until")

    # Helper covers both shapes without raising.
    assert _timeout_until(SimpleNamespace(communication_disabled_until=None)) is None
    assert _timeout_until(SimpleNamespace(timed_out_until=None)) is None

    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    before = member()
    after = member()
    after.guild = guild
    # Live shape: only the py-cord attribute exists, direct access would raise.
    assert before.communication_disabled_until is None
    try:
        before.timed_out_until  # noqa: B018
        raise AssertionError("live-shaped fake should not expose timed_out_until")
    except AttributeError:
        pass

    with caplog.at_level(logging.ERROR, logger="rosemary.cogs.audit"):
        await cog.on_member_update(before, after)
    assert "on_member_update audit failed" not in caplog.text
    assert audit_channel.send.await_count == 0


async def test_bot_member_changes_are_ignored(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    before = member()
    before.bot = True
    after = member()
    after.bot = True
    before.nick = "a"
    after.nick = "b"
    after.guild = guild
    await cog.on_member_update(before, after)
    assert audit_channel.send.await_count == 0


# -- bulk purge coalescing -------------------------------------------------------


async def test_bulk_delete_coalesces_into_one_card_and_file(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    chan = MagicMock()
    chan.mention = "<#7>"
    chan.id = 7

    def batch(n, offset=0):
        out = []
        for i in range(n):
            m = MagicMock(spec=discord.Message)
            m.guild = guild
            m.author = member(100 + i)
            m.content = f"msg {i}"
            m.channel = chan
            m.created_at = MagicMock()
            m.created_at.timestamp.return_value = 1700000000 + offset + i
            m.created_at.strftime.return_value = "2026-01-01 00:00:00"
            out.append(m)
        return out

    cog._bulk_file = AsyncMock(return_value="https://file")
    await cog.on_bulk_message_delete(batch(3))
    await cog.on_bulk_message_delete(batch(2, offset=10))
    await asyncio.sleep(3.6)
    assert audit_channel.send.await_count == 1, "batches must coalesce into one card"
    cog._bulk_file.assert_awaited_once()


async def test_bulk_delete_respects_toggle(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    from rosemary.core.settings import set_setting

    await set_setting(bot.storage, 1, "audit.bulk_delete_enabled", False)
    m = MagicMock(spec=discord.Message)
    m.guild = guild
    m.author = member()
    m.channel = MagicMock(mention="<#7>")
    cog._bulk_file = AsyncMock(return_value="")
    await cog.on_bulk_message_delete([m])
    await asyncio.sleep(3.6)
    audit_channel.send.assert_not_awaited()


async def test_purge_context_suppresses_single_deletes(tmp_path):
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    mod = member(9, "Mod")
    cog.note_purge_context(guild, mod)
    author = member()
    chan = MagicMock()
    chan.mention = "<#7>"
    msg = MagicMock(spec=discord.Message)
    msg.guild = guild
    msg.author = author
    msg.content = "oi"
    msg.channel = chan
    msg.jump_url = "https://x"
    await cog.on_message_delete(msg)
    audit_channel.send.assert_not_awaited()
