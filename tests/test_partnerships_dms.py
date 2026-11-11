"""Partnership DMs: the card pipeline, the split copy and the invite button.

The six ``partnerships.dm.*`` cards are embeds: the content line carries the
event (the partnership was established, ended, needs renewal), the embed
carries the details and ``expired_admin`` gets a button with the invite to
hand to the representative. ``_dm`` injects ``mention=<@id>`` for the
recipient and sends through the same pipeline every other DM uses.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
from catalog import catalog_text, load_catalog

import rosemary.core.card_specs  # noqa: F401  (fills the registry)
from rosemary.cogs.partnerships import PartnershipsCog
from rosemary.core.card_service import render_card_messages
from rosemary.core.cards import get_card
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore
from rosemary.core.variables import VARIABLES
from rosemary.ui.theme import load_theme

ROOT = Path(__file__).resolve().parents[1]

DM_KEYS = ("added", "removed", "rep_changed", "warning", "expired", "expired_admin")


def _catalog_text(key: str) -> str:
    return catalog_text("pt-BR", key)


class FakeTranslator:
    """Returns the real catalog text so tests exercise actual copy."""

    def __init__(self):
        self.theme = load_theme()

    async def t(self, guild_id, key, **variables):
        text = _catalog_text(key)
        mapping = {**self.theme.emojis, **variables}
        for name, value in mapping.items():
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
    bot._theme_cache = {}
    return bot


def _cog(tmp_path):
    return PartnershipsCog(_bot(tmp_path))


def _guild():
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.name = "Girassol"
    return guild


def _member(member_id: int):
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.send = AsyncMock()
    return member


def _embed_text(embed: discord.Embed) -> str:
    """Every line of an embed, so a raw key never hides in a field."""
    parts = [embed.title or "", embed.description or ""]
    if embed.footer is not None:
        parts.append(embed.footer.text or "")
    for field in embed.fields:
        parts.extend((field.name, field.value))
    return "\n".join(parts)


async def _render(cog, key: str, **variables):
    values = {
        "mention": "<@100>",
        "server": "Girassol",
        "days": 3,
        "rep": "<@100>",
        "invite": "https://discord.gg/abc",
        **variables,
    }
    return await render_card_messages(
        cog.bot, 1, f"partnerships.dm.{key}", values
    )


async def test_dm_cards_are_embeds_with_a_content_line(tmp_path):
    """The DM shape is the card's: a line plus an embed, never raw text."""
    cog = _cog(tmp_path)
    for key in DM_KEYS:
        messages = await _render(cog, key)
        assert len(messages) == 1, key
        payload = messages[0].payload
        assert payload.embed is not None, key
        assert payload.content, f"partnerships.dm.{key} lost its content line"
        assert "partnerships.dm." not in payload.content, key
        assert "partnerships.dm." not in _embed_text(payload.embed), key


async def test_dm_content_line_is_the_event_headline(tmp_path):
    """The line is the notice the member reads first, with the theme icon."""
    cog = _cog(tmp_path)
    for key in DM_KEYS:
        payload = (await _render(cog, key))[0].payload
        assert payload.content
        assert not payload.content.startswith("{"), key
        assert payload.embed.title or payload.embed.description, key


async def test_dm_variable_contract_includes_mention():
    """All six DM specs contract {mention}; the variable is registered."""
    for key in DM_KEYS:
        spec = get_card(f"partnerships.dm.{key}")
        assert spec is not None
        assert "mention" in spec.variables, f"partnerships.dm.{key} lacks mention"
        assert "server" in spec.variables
    assert "mention" in VARIABLES


async def test_dm_variable_contract_has_catalog_labels():
    """variables.mention carries label + description in both catalogs."""
    for code in ("pt-BR", "en-US"):
        data = load_catalog(code)
        node = (data.get("variables") or {}).get("mention") or {}
        assert node.get("label"), f"{code}: variables.mention.label missing"
        assert node.get("description"), f"{code}: variables.mention.description missing"


async def test_dm_send_injects_recipient_mention(tmp_path):
    """_dm passes mention=<@id> so the greeting names the right member."""
    cog = _cog(tmp_path)
    guild = _guild()
    guild.get_member = MagicMock(return_value=_member(100))
    await cog._dm(guild, 100, "added", server=guild.name)
    member = guild.get_member.return_value
    kwargs = member.send.await_args.kwargs
    embed = kwargs["embed"]
    assert "Olá, <@100>!" in embed.description
    assert "Girassol" in embed.description
    assert "view" not in kwargs


async def test_dm_warning_carries_days(tmp_path):
    """The warning DM resolves {days} from the renewal sweep."""
    cog = _cog(tmp_path)
    guild = _guild()
    guild.get_member = MagicMock(return_value=_member(100))
    await cog._dm(guild, 100, "warning", days=3, server=guild.name)
    embed = guild.get_member.return_value.send.await_args.kwargs["embed"]
    assert "3 dia(s)" in embed.description


async def test_dm_mention_always_the_recipient(tmp_path):
    """{mention} is forced to the recipient: no caller can misroute it."""
    cog = _cog(tmp_path)
    guild = _guild()
    guild.get_member = MagicMock(return_value=_member(100))
    await cog._dm(guild, 100, "added", server=guild.name, mention="<@999>")
    embed = guild.get_member.return_value.send.await_args.kwargs["embed"]
    assert "Olá, <@100>!" in embed.description
    assert "<@999>" not in _embed_text(embed)


async def test_expired_admin_dm_reaches_the_adder(tmp_path):
    """Expiry DMs the admin who added the partnership, not the rep again."""
    cog = _cog(tmp_path)
    guild = _guild()
    sent: dict[int, dict] = {}

    def get_member(user_id):
        member = _member(user_id)
        member.send = AsyncMock(side_effect=lambda **kwargs: sent.setdefault(user_id, kwargs))
        return member

    guild.get_member = get_member
    await cog._dm(
        guild, 7, "expired_admin",
        rep="<@100>", server=guild.name, days=21, invite="https://discord.gg/abc",
    )
    assert 7 in sent and 100 not in sent
    kwargs = sent[7]
    embed = kwargs["embed"]
    assert "Olá, <@7>!" in embed.description
    assert [field.value for field in embed.fields] == ["<@100>", "há 21 dia(s)"]
    view = kwargs["view"]
    assert [button.url for button in view.children] == ["https://discord.gg/abc"]


async def test_expired_admin_dm_survives_missing_invite(tmp_path):
    """An empty {invite} (no channel rights) still renders the whole DM."""
    cog = _cog(tmp_path)
    guild = _guild()
    guild.get_member = MagicMock(return_value=_member(7))
    await cog._dm(
        guild, 7, "expired_admin",
        rep="<@100>", server=guild.name, days=21, invite="",
    )
    kwargs = guild.get_member.return_value.send.await_args.kwargs
    embed = kwargs["embed"]
    assert "<@100>" in _embed_text(embed)
    assert "há 21 dia(s)" in _embed_text(embed)
    # A link button whose URL never arrived does not exist.
    assert "view" not in kwargs


async def test_expired_admin_variable_contract():
    """The admin DM contracts its five placeholders; invite is registered."""
    spec = get_card("partnerships.dm.expired_admin")
    assert spec is not None
    for name in ("rep", "server", "days", "invite", "mention"):
        assert name in spec.variables
    assert "invite" in VARIABLES


async def test_expired_admin_labels_in_both_catalogs():
    """variables.invite carries label + description in both catalogs."""
    for code in ("pt-BR", "en-US"):
        data = load_catalog(code)
        node = (data.get("variables") or {}).get("invite") or {}
        assert node.get("label"), f"{code}: variables.invite.label missing"
        assert node.get("description"), f"{code}: variables.invite.description missing"


async def test_guild_invite_reuses_the_bots_own_permanent_invite(tmp_path):
    """A standing bot-made invite in the partnerships channel is reused."""
    cog = _cog(tmp_path)
    guild = _guild()
    standing = MagicMock()
    standing.inviter.id = 42
    standing.temporary = False
    standing.max_age = 0
    standing.url = "https://discord.gg/standing"
    other = MagicMock()
    other.inviter.id = 99
    channel = MagicMock(spec=discord.TextChannel)
    channel.invites = AsyncMock(return_value=[other, standing])
    channel.create_invite = AsyncMock(side_effect=AssertionError("must not create"))
    guild.text_channels = []
    cog._channel = AsyncMock(return_value=channel)
    cog.bot.user.id = 42
    assert await cog._guild_invite(guild) == "https://discord.gg/standing"


async def test_guild_invite_creates_when_none_standing(tmp_path):
    """Without a reusable invite the bot creates a permanent one."""
    cog = _cog(tmp_path)
    guild = _guild()
    created = MagicMock()
    created.url = "https://discord.gg/new"
    channel = MagicMock(spec=discord.TextChannel)
    channel.invites = AsyncMock(return_value=[])
    channel.create_invite = AsyncMock(return_value=created)
    guild.text_channels = []
    cog._channel = AsyncMock(return_value=channel)
    assert await cog._guild_invite(guild) == "https://discord.gg/new"


async def test_guild_invite_degrades_to_empty(tmp_path):
    """Forbidden/HTTP errors yield an empty link, never an exception."""
    cog = _cog(tmp_path)
    guild = _guild()
    channel = MagicMock(spec=discord.TextChannel)
    channel.invites = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no"))
    guild.text_channels = []
    cog._channel = AsyncMock(return_value=channel)
    assert await cog._guild_invite(guild) == ""
