"""Partnership DM texts: rich catalog copy and the {mention} contract.

Locks the four ``partnerships.dm.*`` cards: the default copy is the rich
multi-line template (title emoji token, greeting with ``{mention}``,
markdown sections, footer), theme emojis resolve through ``t()``, the
variable contract carries ``mention`` on all four keys, and ``_dm``
injects ``mention=<@id>`` for the recipient without touching callers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import yaml

import rosemary.core.card_specs  # noqa: F401  (fills the registry)
from rosemary.cogs.partnerships import PartnershipsCog
from rosemary.core.cards import get_card
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore
from rosemary.core.variables import VARIABLES
from rosemary.ui.theme import load_theme

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "language" / "pt-BR.yaml"

DM_KEYS = ("added", "removed", "warning", "expired")


def _catalog_text(key: str) -> str:
    with CATALOG.open(encoding="utf-8") as fh:
        node = yaml.safe_load(fh) or {}
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node if isinstance(node, str) else key


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


async def test_dm_texts_are_rich_multiline_templates(tmp_path):
    """Every DM is a rich template: title, {mention} greeting and a footer."""
    cog = _cog(tmp_path)
    for key in DM_KEYS:
        text = await cog.bot.translator.t(
            1, f"partnerships.dm.{key}", mention="<@100>", server="Girassol", days=3
        )
        assert "\n" in text, f"partnerships.dm.{key} lost its multi-line body"
        assert "Olá, <@100>!" in text, f"partnerships.dm.{key} lost the greeting"
        assert "Girassol" in text, f"partnerships.dm.{key} lost the server name"
        assert text.lstrip().startswith("# "), f"partnerships.dm.{key} lost the title"


async def test_dm_texts_resolve_theme_emoji_tokens(tmp_path):
    """Emoji tokens come from the theme, never hardcoded in the catalogs."""
    cog = _cog(tmp_path)
    theme_emojis = load_theme().emojis
    text = await cog.bot.translator.t(
        1, "partnerships.dm.added", mention="<@100>", server="Girassol"
    )
    assert "{crown}" not in text and "{tada}" not in text
    assert theme_emojis["crown"] in text and theme_emojis["tada"] in text


async def test_dm_variable_contract_includes_mention():
    """All four DM specs contract {mention}; the variable is registered."""
    for key in DM_KEYS:
        spec = get_card(f"partnerships.dm.{key}")
        assert spec is not None
        assert "mention" in spec.variables, f"partnerships.dm.{key} lacks mention"
        assert "server" in spec.variables
    assert "mention" in VARIABLES


async def test_dm_variable_contract_has_catalog_labels():
    """variables.mention carries label + description in both catalogs."""
    for code in ("pt-BR", "en-US"):
        with open(ROOT / "language" / f"{code}.yaml", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        node = (data.get("variables") or {}).get("mention") or {}
        assert node.get("label"), f"{code}: variables.mention.label missing"
        assert node.get("description"), f"{code}: variables.mention.description missing"


async def test_dm_send_injects_recipient_mention(tmp_path):
    """_dm passes mention=<@id> so the greeting pings the right member."""
    cog = _cog(tmp_path)
    guild = _guild()
    guild.get_member = MagicMock(return_value=_member(100))
    await cog._dm(guild, 100, "added", server=guild.name)
    member = guild.get_member.return_value
    text = member.send.await_args.args[0]
    assert "Olá, <@100>!" in text
    assert "Girassol" in text


async def test_dm_warning_carries_days(tmp_path):
    """The warning DM resolves {days} from the renewal sweep."""
    cog = _cog(tmp_path)
    guild = _guild()
    guild.get_member = MagicMock(return_value=_member(100))
    await cog._dm(guild, 100, "warning", days=3, server=guild.name)
    text = guild.get_member.return_value.send.await_args.args[0]
    assert "3 dia(s)" in text


async def test_dm_mention_always_the_recipient(tmp_path):
    """{mention} is forced to the recipient: no caller can misroute it."""
    cog = _cog(tmp_path)
    guild = _guild()
    guild.get_member = MagicMock(return_value=_member(100))
    await cog._dm(guild, 100, "added", server=guild.name, mention="<@999>")
    text = guild.get_member.return_value.send.await_args.args[0]
    assert "Olá, <@100>!" in text
    assert "<@999>" not in text


async def test_expired_admin_dm_reaches_the_adder(tmp_path):
    """Expiry DMs the admin who added the partnership, not the rep again."""
    cog = _cog(tmp_path)
    guild = _guild()
    sent: dict[int, str] = {}

    def get_member(user_id):
        member = _member(user_id)
        member.send = AsyncMock(
            side_effect=lambda text: sent.setdefault(user_id, text)
        )
        return member

    guild.get_member = get_member
    await cog._dm(
        guild, 7, "expired_admin",
        rep="<@100>", server=guild.name, days=21, invite="https://discord.gg/abc",
    )
    assert 7 in sent and 100 not in sent
    text = sent[7]
    assert "Olá, <@7>!" in text
    assert "<@100>" in text
    assert "há 21 dia(s)" in text
    assert "https://discord.gg/abc" in text


async def test_expired_admin_dm_survives_missing_invite(tmp_path):
    """An empty {invite} (no channel rights) still renders the whole DM."""
    cog = _cog(tmp_path)
    guild = _guild()
    guild.get_member = MagicMock(return_value=_member(7))
    await cog._dm(
        guild, 7, "expired_admin",
        rep="<@100>", server=guild.name, days=21, invite="",
    )
    text = guild.get_member.return_value.send.await_args.args[0]
    assert "<@100>" in text and "há 21 dia(s)" in text


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
        with open(ROOT / "language" / f"{code}.yaml", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
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
