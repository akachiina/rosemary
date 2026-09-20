"""UI-consistency regressions: invite attribution race, event card layout,
and catalog templates that must carry real newlines (never literal ``\\n``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import yaml

from rosemary.cogs.invites import InvitesCog
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key

    async def raw(self, guild_id, key):
        return ""


def make_invite_bot(tmp_path):
    bot = MagicMock()
    bot.storage = GuildStorage(tmp_path)
    bot.theme = load_theme()
    bot.translator = FakeTranslator()
    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    return bot


#: invite attribution (the pre-join cache race) ------------------------------


async def test_join_attributes_via_prejoin_cache(tmp_path, monkeypatch):
    """Regression: on_member_join used to overwrite the uses cache with the
    post-join snapshot BEFORE diffing, so every join attributed to unknown."""
    from rosemary.cogs import invites as invites_mod
    from rosemary.core.invites import InviteStore

    bot = make_invite_bot(tmp_path)
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.invites = AsyncMock(
        return_value=[MagicMock(code="abc", uses=1, inviter=MagicMock(id=999))]
    )
    member = MagicMock(spec=discord.Member)
    member.id = 5
    member.guild = guild

    from rosemary.core.settings import set_setting

    await set_setting(bot.storage, 1, "invites.enabled", True)

    cog = InvitesCog(bot)
    cog._cache[1] = {"abc": 0}  # pre-join snapshot

    sent: list[tuple] = []

    async def fake_log(*args, **kwargs):
        sent.append((args, kwargs))

    monkeypatch.setattr(invites_mod, "send_channel_log", fake_log)
    await cog.on_member_join(member)

    record = await InviteStore(bot.storage.data_dir).previous_record(1, 5)
    assert record is not None and record.get("inviter_id") == 999
    assert sent, "join log should have been requested"


#: event document layout -----------------------------------------------------


async def test_event_document_has_avatar_section_and_footer(tmp_path):
    """Default event docs: avatar thumbnail section, emoji title, footer."""
    from rosemary.cogs.welcome import default_event_document
    from rosemary.core.cards import build_items

    bot = MagicMock()
    bot.theme = load_theme()
    bot.theme.emojis.setdefault("tada", "🎉")

    class T:
        async def raw(self, guild_id, key):
            return {
                "events.welcome.title": "{tada} Bem-vindo(a)!",
                "events.welcome.body": "{user} entrou!",
            }[key]

    bot.translator = T()

    doc = await default_event_document(
        bot, 1,
        title_key="events.welcome.title",
        body_key="events.welcome.body",
        color="brand",
        emoji_token="tada",
    )
    container = doc["blocks"][0]
    kinds = [child["type"] for child in container["children"]]
    assert kinds == ["section", "divider", "text"]
    section = container["children"][0]
    assert section["accessory"] == {"type": "thumbnail", "url": "{user_avatar}"}
    assert section["children"][0]["body"].startswith("# ")
    assert container["children"][2]["body"].startswith("-# ")

    items = build_items(
        load_theme(), doc,
        {"user": "<@5>", "user_avatar": "https://a.b/ana.png",
         "server": "Serv", "count": 3, "tada": "🎉"},
    )
    found_thumb = False

    def walk(node):
        nonlocal found_thumb
        if isinstance(node, discord.ui.Thumbnail):
            found_thumb = "https://a.b/ana.png" in node.media.url
        for child in list(getattr(node, "items", []) or []) + list(
            getattr(node, "children", []) or []
        ) + [getattr(node, "accessory", None)]:
            if child is not None:
                walk(child)

    for item in items:
        walk(item)
    assert found_thumb, "avatar thumbnail must render with the resolved url"


#: catalog templates: real newlines, never literal "\n" ----------------------


def test_every_log_title_has_theme_emoji():
    """Every log title starts with a theme emoji token ({ban}, {gear}...)."""
    import re

    pattern = re.compile(r"^\{[a-z_]+\}")
    for path in ("language/en-US.yaml", "language/pt-BR.yaml"):
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        def walk(node, prefix=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield from walk(value, f"{prefix}.{key}" if prefix else str(key))
            else:
                yield prefix, node

        for key, value in walk(data):
            if (
                key.endswith(".title")
                and (".logs." in key or ".log." in key)
                and not key.startswith("card.")
            ):
                assert pattern.match(str(value)), f"{path}:{key} lacks emoji token: {value!r}"


def test_boost_log_fields_are_bold_labeled():
    """Boost logs use the compact field format: emoji + bold label on data lines."""
    for lang, first_label in (("pt-BR", "**{tag} Cargo:**"), ("en-US", "**{tag} Role:**")):
        with open(f"language/{lang}.yaml", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        for name, entry in data["boost"]["logs"].items():
            desc = entry["description"]
            assert desc.startswith(first_label), f"{lang}:boost.logs.{name}: {desc!r}"


def test_birthday_catalog_keys_match_spec():
    """birthdays.announce is the customizable card key: catalogs follow."""
    for path in ("language/en-US.yaml", "language/pt-BR.yaml"):
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        b = data["birthdays"]
        assert "announce" in b and "title" in b["announce"]
        assert "card" not in b, f"{path}: legacy birthdays.card still present"


def _flatten(node, out):
    if isinstance(node, dict):
        for value in node.values():
            _flatten(value, out)
    elif isinstance(node, list):
        for value in node:
            _flatten(value, out)
    elif isinstance(node, str):
        out.append(node)


def test_log_templates_use_real_newlines():
    """Single-quoted YAML kept '\\n' literal (screenshot bug); these templates
    must now parse to real newlines."""
    for path in ("language/en-US.yaml", "language/pt-BR.yaml"):
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        strings: list[str] = []
        _flatten(data["moderation"]["logs"], strings)
        _flatten(data["settings"]["log"], strings)
        _flatten(data["settings"]["logs"], strings)
        _flatten(data["invites"]["logs"]["join"], strings)
        _flatten(data["invites"]["logs"]["leave"], strings)
        _flatten(data["boost"]["logs"], strings)
        assert strings, f"{path}: templates missing"
        for text in strings:
            assert "\\n" not in text, f"{path}: literal backslash-n in {text!r}"
