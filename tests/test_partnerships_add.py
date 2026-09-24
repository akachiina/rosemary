"""Partnership add flow: member-only slash command opening the ad-text modal.

Locks the redesigned ``/fazer_parceria``: guards answer ephemeral before any
modal, the modal is a paragraph field with the catalog placeholder hint, and
the submit resolves ``{rep}``/``{ping_role}`` (plus theme emojis) into the
stored and posted text, then runs the same add pipeline as before (store,
role, DM, log).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import yaml

import rosemary.cogs.partnerships as partnerships_module
from rosemary.cogs.partnerships import PartnershipsCog
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore
from rosemary.ui.theme import load_theme

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "language" / "pt-BR.yaml"


def _catalog_text(key: str) -> str:
    with CATALOG.open(encoding="utf-8") as fh:
        node = yaml.safe_load(fh) or {}
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node if isinstance(node, str) else key


class FakeTranslator:
    """Formats like the real Translator: theme emojis as default vars."""

    def __init__(self):
        self.theme = load_theme()

    async def t(self, guild_id, key, **variables):
        template = _catalog_text(key)
        mapping = {**self.theme.emojis, **variables}
        try:
            return template.format(**mapping)
        except (KeyError, IndexError, ValueError):
            return template

    async def raw(self, guild_id, key):
        return _catalog_text(key)


def _bot(tmp_path):
    bot = MagicMock()
    bot.theme = load_theme()
    bot.translator = FakeTranslator()
    bot.storage = GuildStorage(tmp_path)
    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    bot.user.id = 42
    return bot


def _cog(tmp_path):
    return PartnershipsCog(_bot(tmp_path))


def _guild():
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.name = "Girassol"
    guild.get_member = MagicMock(return_value=None)
    return guild


def _member():
    member = MagicMock(spec=discord.Member)
    member.id = 100
    member.mention = "<@100>"
    return member


def _ctx(guild):
    ctx = MagicMock()
    ctx.guild = guild
    ctx.author = _member()
    ctx.send_modal = AsyncMock()
    ctx.respond = AsyncMock()
    return ctx


def _ad_channel():
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 55
    channel.mention = "<#55>"
    return channel


def _input_text(modal) -> discord.ui.InputText:
    for child in modal.children:
        item = getattr(child, "item", None)
        if isinstance(item, discord.ui.InputText):
            return item
    raise AssertionError("modal has no InputText field")


async def _prepare(tmp_path, *, enabled=True, channel=True):
    cog = _cog(tmp_path)
    guild = _guild()
    if enabled:
        await cog.bot.storage.set(1, "partnerships.enabled", True)
    if channel:
        await cog.bot.storage.set(1, "partnerships.channel", 55)
    if enabled and channel:
        await cog.bot.storage.set(1, "partnerships.ping_role", 777)
    cog._channel = AsyncMock(return_value=_ad_channel() if channel else None)
    ctx = _ctx(guild)
    return cog, guild, ctx


async def test_add_opens_paragraph_modal_with_catalog_hint(tmp_path):
    """The command opens a paragraph modal with the translated placeholder."""
    cog, guild, ctx = await _prepare(tmp_path)
    await PartnershipsCog.partnerships_add.callback(cog, ctx, member=_member())
    modal = ctx.send_modal.await_args.args[0]
    field = _input_text(modal)
    assert field.style == discord.InputTextStyle.paragraph
    assert field.max_length == 2000
    hint = _catalog_text("partnerships.modal.placeholder").replace("{{", "{").replace("}}", "}")
    assert field.placeholder == hint
    assert len(modal.title) <= 45


async def test_add_disabled_answers_instead_of_modal(tmp_path):
    """Disabled partnerships: ephemeral error, no modal ever opens."""
    cog, guild, ctx = await _prepare(tmp_path, enabled=False)
    await PartnershipsCog.partnerships_add.callback(cog, ctx, member=_member())
    ctx.send_modal.assert_not_called()
    assert ctx.respond.await_args.kwargs.get("ephemeral") is True
    assert "desativadas" in ctx.respond.await_args.args[0]


async def test_add_without_channel_answers_instead_of_modal(tmp_path):
    """No partnerships channel: ephemeral error, no modal ever opens."""
    cog, guild, ctx = await _prepare(tmp_path, channel=False)
    await PartnershipsCog.partnerships_add.callback(cog, ctx, member=_member())
    ctx.send_modal.assert_not_called()
    assert ctx.respond.await_args.kwargs.get("ephemeral") is True
    assert "canal" in ctx.respond.await_args.args[0]


async def _submit(cog, ctx, guild, raw: str):
    await PartnershipsCog.partnerships_add.callback(cog, ctx, member=_member())
    modal = ctx.send_modal.await_args.args[0]
    field = _input_text(modal)
    field.value = raw
    interaction = MagicMock()
    interaction.user = _member()
    interaction.response.send_message = AsyncMock()
    await modal.callback(interaction)
    return interaction


async def test_add_submit_resolves_placeholders_and_stores(tmp_path, monkeypatch):
    """{rep}/{channel}/{ping_role} and theme emojis resolve before saving."""
    cog, guild, ctx = await _prepare(tmp_path)
    posted = {}

    async def fake_post(g, content, attachments, channel=None):
        posted["content"] = content
        return MagicMock(id=999)

    monkeypatch.setattr(cog, "_post_ad", fake_post)
    monkeypatch.setattr(partnerships_module, "send_channel_log", AsyncMock())

    interaction = await _submit(cog, ctx, guild, "Fala {rep}! {ping_role} {plus}")
    text = posted["content"]
    assert text == "Fala <@100>! <@&777> ➕"
    entry = await cog.store.get(guild.id, 100)
    assert entry is not None and entry["content"] == text
    assert entry["message_id"] == 999
    assert interaction.response.send_message.await_args.kwargs.get("ephemeral") is True
    assert "<@100>" in interaction.response.send_message.await_args.args[0]


async def test_add_submit_logs_with_submitter_as_author(tmp_path, monkeypatch):
    """The audit log attributes the partnership to the modal submitter."""
    cog, guild, ctx = await _prepare(tmp_path)
    log_vars = {}
    monkeypatch.setattr(cog, "_post_ad", AsyncMock(return_value=MagicMock(id=999)))

    async def fake_log(guild_id, key, color, ids=(), **variables):
        log_vars.update(variables)

    monkeypatch.setattr(cog, "_log", fake_log)
    interaction = await _submit(cog, ctx, guild, "anúncio")
    assert log_vars["user"] == "<@100>"
    assert log_vars["author"] == interaction.user.mention
