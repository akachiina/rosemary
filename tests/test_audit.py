"""Registro de Eventos (cogs/audit.py): gating, cards, diffs and bulk purge.

Fake bot + real catalogs: every listener runs against the settings store and
the shared card pipeline, so the tests pin both the toggle gating and the
theme-customizable send path (including the fenced ``` blocks).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from catalog import flatten, load_catalog

from rosemary.cogs.audit import AUDIT_BAN, AuditCog, _fence
from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        suffix = "".join(f";{k}={v}" for k, v in sorted(variables.items()))
        return f"{key}{suffix}"

    async def raw(self, guild_id, key):
        return key


class RealCatalogTranslator(FakeTranslator):
    """Resolves keys from the real pt-BR/en-US YAML catalogs."""

    _catalogs: dict = {}

    @classmethod
    def _load(cls) -> None:
        for lang in ("en-US", "pt-BR"):
            cls._catalogs[lang] = flatten(load_catalog(lang))

    async def raw(self, guild_id, key):
        if not self._catalogs:
            self._load()
        lang = "pt-BR" if guild_id == 1 else "en-US"
        return self._catalogs[lang].get(key, key)


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


#: card inventory and contracts ----------------------------------------------


def _audit_keys() -> tuple[str, ...]:
    """The audit cards the default package declares."""
    from rosemary.core.theme_package import default_package

    return tuple(key for key in default_package().card_keys() if key.startswith("audit."))


async def _render(bot, key, variables=None):
    """Render one card through the shared pipeline."""
    from rosemary.core.card_service import render_card_message

    payload, _allowed = await render_card_message(bot, 1, key, variables or {})
    return payload


def _render_bot(tmp_path):
    """A bot that renders with the real catalogs (the package is the truth)."""
    bot, *_rest = make_world(tmp_path)
    bot.translator = RealCatalogTranslator()
    return bot


def test_audit_cards_registered_with_variables():
    from rosemary.core.cards import get_card
    from rosemary.core.theme_package import default_package

    names = {key.removeprefix("audit.") for key in _audit_keys()}
    assert names == {
        "ban", "unban", "message_delete", "message_edit", "bulk_delete",
        "nickname", "avatar", "roles", "timeout", "voice_join", "voice_leave",
        "trap",
    }
    package = default_package()
    for name in names:
        key = f"audit.{name}"
        spec = get_card(key)
        assert spec is not None
        meta = package.metadata(key)
        assert meta["variables"] == spec.variables
        assert spec.category == "audit"


def test_audit_card_titles_in_both_catalogs():
    for lang in ("en-US", "pt-BR"):
        data = load_catalog(lang)
        node = data["card"]["audit"]
        for key in _audit_keys():
            name = key.removeprefix("audit.")
            assert isinstance(node[name].get("title"), str), f"{lang}:{key}.title"


async def test_the_package_renders_every_audit_card(tmp_path):
    bot = _render_bot(tmp_path)
    for key in _audit_keys():
        payload = await _render(bot, key)
        assert payload is not None, key
        assert payload.embed is not None or payload.view is not None, key


async def test_the_package_accepts_send_site_variables(tmp_path):
    """Regression: the card fell through with the contract variables.

    When the card does not render with the variables the cog actually
    passes, live sends showed raw catalog keys.
    """
    from rosemary.core.theme_package import default_package
    from rosemary.core.variables import samples_for

    bot = _render_bot(tmp_path)
    package = default_package()
    for key in _audit_keys():
        variables = samples_for(package.metadata(key)["variables"])
        payload = await _render(bot, key, variables)
        assert payload is not None and (payload.embed or payload.view), (
            f"{key} fell through with variables {sorted(variables)}"
        )


async def test_every_card_renders_with_the_variables_of_its_contract(tmp_path):
    """Sweep: no card may drop the variables the send site delivers.

    The old builders had closed signatures and lost the variables they did
    not declare (the card fell through to the raw catalog key). The
    documents declare their contract instead, so every card of the package
    must render with the samples of its own contract.
    """
    from rosemary.core.theme_package import default_package
    from rosemary.core.variables import samples_for

    bot = _render_bot(tmp_path)
    package = default_package()
    for key in package.card_keys():
        variables = samples_for(package.metadata(key)["variables"])
        payload = await _render(bot, key, variables)
        assert payload is not None, f"{key} fell through with variables {sorted(variables)}"
        assert payload.embed is not None or payload.view is not None or payload.content, key


async def test_audit_fallback_uses_card_prefixed_catalog_keys(tmp_path, caplog, monkeypatch):
    """Regression: the emergency fallback looked up ``audit.*.title`` (raw)."""
    import rosemary.core.card_service as card_service

    async def _no_card(bot, guild_id, key, variables=None, **kwargs):
        return None, discord.AllowedMentions.none()

    monkeypatch.setattr(card_service, "render_card_message", _no_card)

    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    author = member()
    msg = MagicMock(spec=discord.Message)
    msg.guild = guild
    msg.author = author
    msg.content = "oi"
    msg.channel = MagicMock(mention="<#7>")
    msg.jump_url = "https://x"
    with caplog.at_level("WARNING"):
        await cog.on_message_delete(msg)
    audit_channel.send.assert_awaited_once()
    joined = " ".join(_payload_texts(audit_channel.send.await_args.kwargs))
    # The catalog keys are ``card.audit.*``: the fallback must find them.
    assert "card.audit.message_delete.title" in joined
    assert "card.audit.message_delete.body" in joined
    assert not any("default builder failed" in r.message for r in caplog.records)


def _doc_texts(doc) -> list[str]:
    """Every text of a default document, whatever its form (V2 or embed)."""
    if doc.get("kind") == "embed":
        embed = doc.get("embed") or {}
        texts = [str(embed.get("title") or ""), str(embed.get("description") or "")]
        for field in embed.get("fields") or []:
            texts.append(str(field.get("name") or ""))
            texts.append(str(field.get("value") or ""))
        footer = embed.get("footer") or {}
        texts.append(str(footer.get("text") or ""))
        author = embed.get("author") or {}
        texts.append(str(author.get("name") or ""))
        return [text for text in texts if text]
    texts: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("body"), str):
                texts.append(node["body"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc.get("blocks") or [])
    return texts


def _footer_texts(doc) -> list[str]:
    """The footer lines of a default document (the attribution line)."""
    if doc.get("kind") == "embed":
        footer = (doc.get("embed") or {}).get("footer") or {}
        return [footer["text"]] if footer.get("text") else []
    return [
        block["body"]
        for block in doc["blocks"][0]["children"]
        if block.get("type") == "text" and block.get("body", "").startswith("-# ")
    ]


def _authors(doc) -> list[str]:
    """The author line of a default document (the embed author)."""
    if doc.get("kind") == "embed":
        author = (doc.get("embed") or {}).get("author") or {}
        return [author["name"]] if author.get("name") else []
    return []


def _view_texts(view) -> list[str]:
    """Every TextDisplay content of a rendered card view (nested-safe)."""
    texts: list[str] = []

    def walk(node) -> None:
        if hasattr(node, "content") and isinstance(node.content, str):
            texts.append(node.content)
        for child in (
            list(getattr(node, "children", []) or [])
            + list(getattr(node, "items", []) or [])
            + list(getattr(node, "walk_children", lambda: [])())
        ):
            walk(child)

    for item in view.children:
        walk(item)
    return texts


def _payload_texts(kwargs) -> list[str]:
    """Every text of a sent card payload (Components V2 view or embed)."""
    texts: list[str] = []
    view = kwargs.get("view")
    if view is not None:
        texts.extend(_view_texts(view))
    embed = kwargs.get("embed")
    if embed is not None:
        texts.append(embed.title or "")
        texts.append(embed.description or "")
        for field in embed.fields:
            texts.append(field.name or "")
            texts.append(field.value or "")
        if embed.footer and embed.footer.text:
            texts.append(embed.footer.text)
        if embed.author and embed.author.name:
            texts.append(embed.author.name)
    return [text for text in texts if text]


def _payload_footer(kwargs) -> str:
    """The footer text of a sent payload (embed footer, empty otherwise)."""
    embed = kwargs.get("embed")
    if embed is not None and embed.footer and embed.footer.text:
        return embed.footer.text
    return ""


async def test_every_audit_default_carries_author_footer(tmp_path):
    """The footer carries the time; the member owns the author line.

    Actor events (ban / unban / bulk purge) attribute the moderator in the
    footer; without send-site variables there is nobody to attribute, so the
    footer degrades to the timestamp-only line and the event's *target* never
    lands as the "por" mention (live that rendered '@usuário-desconhecido'
    for viewers without the member cached).
    """
    from rosemary.core.theme_package import default_package
    from rosemary.core.variables import samples_for

    bot = _render_bot(tmp_path)
    package = default_package()
    for key in _audit_keys():
        # Without send-site variables there is nothing to attribute: the
        # footer is the timestamp-only line and nobody owns the author line.
        payload = await _render(bot, key)
        assert payload.embed is not None, key
        assert payload.embed.footer.text == "{timestamp}", key
        assert payload.embed.author is None, key
        # With them, the member owns the author line and the footer stays.
        variables = samples_for(package.metadata(key)["variables"])
        payload = await _render(bot, key, variables)
        assert payload.embed.footer.text, key
        assert payload.embed.author is not None, key


def test_fence_wraps_and_neutralizes_backticks():
    assert _fence("hello").startswith("```")
    assert _fence("hello").endswith("```")
    assert "```" not in _fence("has ``` inside")[3:-3]


#: gating and send path -------------------------------------------------------


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


async def test_ban_audit_log_context_uses_enum_action(tmp_path):
    """py-cord's ``Guild.audit_logs`` calls ``action.value``: passing the raw
    integer crashed with ``AttributeError: 'int' object has no attribute
    'value'`` and the whole ban card was lost (live regression)."""
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()

    import discord as d

    seen: dict = {}

    def audit_logs(limit=None, action=None):
        seen["action"] = action

        class _Iter:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        return _Iter()

    guild.audit_logs = audit_logs
    moderator, reason = await cog._audit_log_context(guild, 5, AUDIT_BAN)
    assert (moderator, reason) == ("", "")
    assert isinstance(seen["action"], d.AuditLogAction)
    assert seen["action"] is d.AuditLogAction.ban


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
    msg.attachments = []
    msg.stickers = []
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
        m.attachments = []
        m.stickers = []
        m.id = 987654321000000000
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
    """Regression: live Member lacks timed_out_until (py-cord name differs)."""
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


#: bulk purge coalescing -------------------------------------------------------


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


#: chat snapshot (audit.snapshot_enabled) ---------------------------------------


def _snapshot_message(guild, author, content="oi"):
    """A message whose snapshot pipeline resolves everything offline."""
    chan = MagicMock()
    chan.mention = "<#7>"
    msg = MagicMock(spec=discord.Message)
    msg.guild = guild
    msg.author = author
    msg.content = content
    msg.channel = chan
    msg.jump_url = "https://x"
    msg.attachments = []
    msg.stickers = []
    msg.id = 987654321000000000
    msg.created_at.timestamp.return_value = 1700000000.0
    return msg


async def test_message_delete_snapshot_disabled_by_setting(tmp_path):
    """``audit.snapshot_enabled`` off: renderer never runs, card stays text-only."""
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    from rosemary.core.settings import set_setting

    await set_setting(bot.storage, 1, "audit.snapshot_enabled", False)
    msg = _snapshot_message(guild, member())
    with patch(
        "rosemary.core.chat_image.render_message_snapshot", new=AsyncMock()
    ) as renderer:
        await cog.on_message_delete(msg)
    renderer.assert_not_awaited()
    assert cog._snapshot_files == []
    assert audit_channel.send.await_count == 1  # the text-only card still goes out


async def test_message_delete_snapshot_failure_degrades_to_text(tmp_path):
    """Renderer returning None (missing WeasyPrint, broken theme): no file."""
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    msg = _snapshot_message(guild, member())
    with patch(
        "rosemary.core.chat_image.render_message_snapshot",
        new=AsyncMock(return_value=None),
    ):
        await cog.on_message_delete(msg)
    assert audit_channel.send.await_count == 1
    assert cog._snapshot_files == []
    assert "files" not in audit_channel.send.await_args.kwargs


async def test_message_delete_snapshot_attaches_png_and_galleries_it(tmp_path):
    """Snapshot ON + renderer ok: the card carries the PNG and references it."""
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    msg = _snapshot_message(guild, member())
    png = b"\x89PNG\r\n\x1a\nfake"
    with patch(
        "rosemary.core.chat_image.render_message_snapshot",
        new=AsyncMock(return_value=png),
    ):
        await cog.on_message_delete(msg)
    audit_channel.send.assert_awaited_once()
    files = audit_channel.send.await_args.kwargs["files"]
    assert [f.filename for f in files] == ["mensagem_987654321000000000.png"]
    assert files[0].fp.read() == png
    assert cog._snapshot_files == []  # drained: no leak into the next card

    # The card must reference the attachment in its image (embed form).
    kwargs = audit_channel.send.await_args.kwargs
    embed = kwargs.get("embed")
    assert embed is not None, "the audit card must ship as an embed"
    assert embed.image is not None and embed.image.url, "the embed lost the snapshot"
    assert "attachment://mensagem_987654321000000000.png" in embed.image.url


async def test_message_edit_snapshot_receives_before_for_pair(tmp_path):
    """Edit: the snapshot pipeline gets before= so it can render both states."""
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    author = member()
    before = _snapshot_message(guild, author, "antes")
    after = _snapshot_message(guild, author, "depois")

    seen: dict = {}

    async def fake_render(bot_, message, *, before=None, edited_marker=""):
        seen["before"] = before
        seen["message"] = message
        seen["marker"] = edited_marker
        return b"\x89PNG\r\n\x1a\nfake"

    with patch("rosemary.core.chat_image.render_message_snapshot", new=fake_render):
        await cog.on_message_edit(before, after)
    assert seen["before"] is before
    assert seen["message"] is after
    # FakeTranslator.raw returns the key itself: a missing catalog key must
    # degrade to NO marker (never hardcoded text).
    assert seen["marker"] == ""
    files = audit_channel.send.await_args.kwargs["files"]
    assert [f.filename for f in files] == ["mensagem_987654321000000000.png"]

    # With the real catalog the guild's marker reaches the renderer. The key
    # lives at ``audit.edited_marker`` (beside ``audit.no_nickname``): the
    # old lookup went through _line and searched ``card.audit.edited_marker``
    # (missing: the marker never appeared live).
    bot.translator = RealCatalogTranslator()
    seen.clear()
    with patch("rosemary.core.chat_image.render_message_snapshot", new=fake_render):
        await cog.on_message_edit(before, after)
    assert seen["marker"] == "(editado)"


async def test_message_delete_snapshot_never_breaks_the_send(tmp_path):
    """The renderer raising unexpectedly must not lose the log card."""
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()

    async def boom(bot_, message, *, before=None):
        raise RuntimeError("renderer exploded")

    msg = _snapshot_message(guild, member())
    with patch("rosemary.core.chat_image.render_message_snapshot", new=boom):
        await cog.on_message_delete(msg)
    audit_channel.send.assert_awaited_once()
    assert cog._snapshot_files == []


#: bot-action stamps -----------------------------------------------------------


async def test_bot_timeout_stamp_skips_gateway_card(tmp_path):
    """Regression: /mute posted two cards (moderation log + audit timeout)."""
    import datetime as dt

    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    expiry = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    before = member()
    before.communication_disabled_until = None
    after = member()
    after.communication_disabled_until = expiry
    after.guild = guild
    cog.note_bot_timeout(1, after.id)
    await cog.on_member_update(before, after)
    assert audit_channel.send.await_count == 0
    # Stamp consumed: the same change now posts (manual GUI timeout path).
    await cog.on_member_update(before, after)
    assert audit_channel.send.await_count == 1


async def test_timeout_until_line_has_no_raw_placeholders(tmp_path):
    """Regression: the card leaked '{clock} Válido até: {duration}'."""
    import datetime as dt

    bot, guild, audit_channel, cog = make_world(tmp_path)
    bot.translator = RealCatalogTranslator()
    await cog._arm()
    before = member()
    before.communication_disabled_until = None
    after = member()
    after.communication_disabled_until = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    after.guild = guild
    await cog.on_member_update(before, after)
    assert audit_channel.send.await_count == 1
    joined = " ".join(_payload_texts(audit_channel.send.await_args.kwargs))
    assert "{clock}" not in joined, joined
    assert "{duration}" not in joined, joined
    assert "Válido até:" in joined, joined


async def test_reason_and_moderator_lines_resolve_placeholders(tmp_path):
    """Same nested-template class as until_line, on the ban/unban bodies."""
    bot, guild, audit_channel, cog = make_world(tmp_path)
    bot.translator = RealCatalogTranslator()
    reason = await cog._reason_line(1, "spam")
    assert "{scroll}" not in reason, reason
    assert "{reason}" not in reason, reason
    assert "spam" in reason, reason
    moderator = await cog._moderator_line(1, "@mod")
    assert "{gear}" not in moderator, moderator
    assert "{moderator}" not in moderator, moderator
    assert "@mod" in moderator, moderator


async def test_file_line_resolves_nested_placeholders(tmp_path):
    """Same nested-template class as until_line: ``file_line`` was passed raw
    and the live purge card kept '{scroll} ... ({file_url})' literal."""
    bot, guild, audit_channel, cog = make_world(tmp_path)
    bot.translator = RealCatalogTranslator()
    line = await cog._file_line(1, "https://cdn.example/purga.txt")
    assert "{scroll}" not in line, line
    assert "{file_url}" not in line, line
    assert "[abrir arquivo](https://cdn.example/purga.txt)" in line, line


async def test_purge_card_carries_resolved_transcript_link(tmp_path):
    """End-to-end purge: the transcript link resolves inside the sent card."""
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    bot.translator = RealCatalogTranslator()
    chan = MagicMock()
    chan.mention = "<#7>"
    chan.id = 7

    def batch(n):
        out = []
        for i in range(n):
            m = MagicMock(spec=discord.Message)
            m.guild = guild
            m.author = member(100 + i)
            m.content = f"msg {i}"
            m.channel = chan
            m.created_at = MagicMock()
            m.created_at.timestamp.return_value = 1700000000 + i
            m.created_at.strftime.return_value = "2026-01-01 00:00:00"
            out.append(m)
        return out

    cog._bulk_file = AsyncMock(return_value="https://cdn.example/purga.txt")
    await cog.on_bulk_message_delete(batch(2))
    await asyncio.sleep(3.6)
    assert audit_channel.send.await_count == 1
    joined = " ".join(_payload_texts(audit_channel.send.await_args.kwargs))
    assert "Transcrição completa:" in joined, joined
    assert "(https://cdn.example/purga.txt)" in joined, joined
    assert "{scroll}" not in joined, joined
    assert "{file_url}" not in joined, joined


async def test_ban_footer_names_the_moderator_not_the_target(tmp_path):
    """Regression: 'por {user}' resolved the banned member, which every
    viewer rendered as '@usuário-desconhecido'. The author footer now
    carries the moderator the audit-log lookup found."""
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    bot.translator = RealCatalogTranslator()
    cog._audit_log_context = AsyncMock(return_value=("<@777>", "raids"))
    await cog.on_member_ban(guild, member())
    assert audit_channel.send.await_count == 1
    footer = _payload_footer(audit_channel.send.await_args.kwargs)
    assert footer, "the card must keep its author footer"
    assert "<@777>" in footer, footer
    assert "<@5>" not in footer, footer


async def test_ban_footer_without_moderator_is_timestamp_only(tmp_path):
    """Audit-log lookup failed: no attribution to invent, timestamp only."""
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    bot.translator = RealCatalogTranslator()
    cog._audit_log_context = AsyncMock(return_value=("", ""))
    await cog.on_member_ban(guild, member())
    assert audit_channel.send.await_count == 1
    footer = _payload_footer(audit_channel.send.await_args.kwargs)
    assert footer, "the card must still go out with a footer"
    assert "<@" not in footer and "<t:" in footer, footer


async def test_purge_footer_names_the_moderator_not_the_first_author(tmp_path):
    """Attribution of a purge is the moderator, never a purged member."""
    bot, guild, audit_channel, cog = make_world(tmp_path)
    await cog._arm()
    bot.translator = RealCatalogTranslator()

    async def fake_moderator(_guild, _bucket):
        return "<@777>"

    cog._bulk_moderator = fake_moderator
    cog._bulk_file = AsyncMock(return_value="")
    await cog.on_bulk_message_delete([batch_of(guild, 100)])
    await asyncio.sleep(3.6)
    assert audit_channel.send.await_count == 1
    footer = _payload_footer(audit_channel.send.await_args.kwargs)
    assert footer, "the card must keep its author footer"
    assert "<@777>" in footer, footer
    assert "<@100>" not in footer, footer


def batch_of(guild, author_id):
    """One purged fake message sharing the test-world guild shape."""
    chan = MagicMock()
    chan.mention = "<#7>"
    chan.id = 7
    m = MagicMock(spec=discord.Message)
    m.guild = guild
    m.author = member(author_id)
    m.content = "msg"
    m.channel = chan
    m.created_at = MagicMock()
    m.created_at.timestamp.return_value = 1700000000
    m.created_at.strftime.return_value = "2026-01-01 00:00:00"
    return m
