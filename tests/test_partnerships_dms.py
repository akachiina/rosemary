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
