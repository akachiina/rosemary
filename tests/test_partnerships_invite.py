"""Partnership invite command: template resolution and send target.

Locks the invite pipeline: a stored ``partnerships.invite_text`` setting wins
over the catalog default, guilds that never touched the setting get the
guild-language default (``partnerships.invite_text_default``), the template's
``{rep}``/``{ping_role}`` placeholders resolve (theme emojis included, unknown
ones stay literal), and the ad posts to the chosen channel (current channel
by default, explicit ``canal`` option otherwise).
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
    """Returns the real catalog text so tests exercise actual copy."""

    async def t(self, guild_id, key, **variables):
        text = _catalog_text(key)
        for name, value in variables.items():
            text = text.replace(f"{{{name}}}", str(value))
        return text

    async def raw(self, guild_id, key):
        return _catalog_text(key)


def _bot(tmp_path):
    bot = MagicMock()
    bot.theme = load_theme()
    bot.translator = FakeTranslator()
    bot.storage = GuildStorage(tmp_path)
    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    return bot


def _cog(tmp_path):
    from rosemary.cogs.partnerships import PartnershipsCog

    return PartnershipsCog(_bot(tmp_path))


def _guild():
    guild = MagicMock()
    guild.id = 1
    guild.name = "Girassol"
    return guild


def _channel(channel_id: int):
    chan = AsyncMock(spec=discord.TextChannel)
    chan.id = channel_id
    chan.send = AsyncMock(return_value=MagicMock())
    return chan


def _run_command(cog, ctx, **kwargs):
    """Invoke the raw callback: py-cord's SlashCommand is not a descriptor."""
    return PartnershipsCog.partnerships_invite.callback(cog, ctx, **kwargs)


async def test_invite_default_uses_catalog_and_rep_placeholder(tmp_path):
    """No setting touched: the guild-language default resolves {rep}, no ping."""
    cog = _cog(tmp_path)
    text = await cog._invite_text(_guild(), "<@100>")
    assert text == "Faça parceria com a gente! Fale com <@100> para participar."


async def test_invite_setting_wins_over_catalog(tmp_path):
    """A customized setting keeps its text verbatim (zero migration)."""
    cog = _cog(tmp_path)
    await cog.bot.storage.set(1, "partnerships.invite_text", "Parceria! Contato: {rep}")
    text = await cog._invite_text(_guild(), "<@100>")
    assert text == "Parceria! Contato: <@100>"


async def test_invite_setting_resolves_ping_role(tmp_path):
    """{ping_role} resolves from the configured role and is a real mention."""
    cog = _cog(tmp_path)
    await cog.bot.storage.set(1, "partnerships.ping_role", 777)
    await cog.bot.storage.set(
        1, "partnerships.invite_text", "{ping_role} novidade! Rep: {rep}"
    )
    text = await cog._invite_text(_guild(), "<@100>")
    assert text == "<@&777> novidade! Rep: <@100>"


async def test_invite_unknown_placeholders_stay_literal(tmp_path):
    """An unknown placeholder (or theme-emoji collision) never crashes the send."""
    cog = _cog(tmp_path)
    await cog.bot.storage.set(1, "partnerships.invite_text", "oi {naoexiste}")
    text = await cog._invite_text(_guild(), "<@100>")
    assert text == "oi {naoexiste}"


async def test_invite_posts_to_current_channel_by_default(tmp_path, monkeypatch):
    """Without the canal option the ad goes to ctx.channel, not partnerships.channel."""
    cog = _cog(tmp_path)
    current = _channel(42)
    ctx = MagicMock()
    ctx.guild = _guild()
    ctx.channel = current
    ctx.author = MagicMock()
    ctx.author.id = 100
    ctx.author.mention = "<@100>"
    ctx.response.is_done.return_value = False
    ctx.response.defer = AsyncMock()
    ctx.respond = AsyncMock()

    async def patched_enabled(storage, guild_id, key):
        return key == "partnerships.enabled"

    monkeypatch.setattr(partnerships_module, "get_setting", patched_enabled)

    posted = {}

    async def fake_post(guild, content, attachments, channel=None):
        posted["channel"] = channel
        posted["content"] = content
        return MagicMock()

    monkeypatch.setattr(cog, "_post_ad", fake_post)
    await _run_command(cog, ctx)
    assert posted["channel"] is current
    assert posted["content"].startswith("Faça parceria com a gente!")


async def test_invite_posts_to_explicit_channel(tmp_path, monkeypatch):
    """The canal option redirects the ad away from the current channel."""
    cog = _cog(tmp_path)
    partnerships_channel = _channel(99)
    ctx = MagicMock()
    ctx.guild = _guild()
    ctx.channel = _channel(42)
    ctx.author = MagicMock()
    ctx.author.id = 100
    ctx.author.mention = "<@100>"
    ctx.response.is_done.return_value = False
    ctx.response.defer = AsyncMock()
    ctx.respond = AsyncMock()

    async def patched_enabled(storage, guild_id, key):
        return key == "partnerships.enabled"

    monkeypatch.setattr(partnerships_module, "get_setting", patched_enabled)

    posted = {}

    async def fake_post(guild, content, attachments, channel=None):
        posted["channel"] = channel
        return MagicMock()

    monkeypatch.setattr(cog, "_post_ad", fake_post)
    await _run_command(cog, ctx, channel=partnerships_channel)
    assert posted["channel"] is partnerships_channel


async def test_invite_rep_defaults_to_command_author(tmp_path, monkeypatch):
    """Without the representante option, {rep} resolves to the command user."""
    cog = _cog(tmp_path)
    ctx = MagicMock()
    ctx.guild = _guild()
    ctx.channel = _channel(42)
    ctx.author = MagicMock()
    ctx.author.id = 100
    ctx.author.mention = "<@100>"
    ctx.response.is_done.return_value = False
    ctx.response.defer = AsyncMock()
    ctx.respond = AsyncMock()

    async def patched_enabled(storage, guild_id, key):
        return key == "partnerships.enabled"

    monkeypatch.setattr(partnerships_module, "get_setting", patched_enabled)

    posted = {}

    async def fake_post(guild, content, attachments, channel=None):
        posted["content"] = content
        return MagicMock()

    monkeypatch.setattr(cog, "_post_ad", fake_post)
    await _run_command(cog, ctx)
    assert posted["content"] == (
        "Faça parceria com a gente! Fale com <@100> para participar."
    )


async def test_invite_post_uses_allowed_mentions_from_text(tmp_path):
    """The send resolves AllowedMentions from the text, never wide-open."""
    cog = _cog(tmp_path)
    await cog.bot.storage.set(1, "partnerships.invite_text", "fale com {rep}")
    chan = _channel(42)
    guild = _guild()

    async def fake_channel(g):
        return None

    monkey_target = chan
    await cog._post_ad(guild, "fale com <@100>", [], channel=monkey_target)
    _, kwargs = chan.send.await_args
    allowed = kwargs["allowed_mentions"]
    # partnerships.invite has no pings override and the default policy for it
    # resolves off here, so the send carries none(): never wide-open.
    assert isinstance(allowed, discord.AllowedMentions)
    assert not allowed.users and not allowed.roles and not allowed.everyone
