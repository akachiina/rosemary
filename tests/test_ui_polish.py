"""UI-consistency regressions: invite attribution race, event card layout,
and catalog templates that must carry real newlines (never literal ``\\n``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
from catalog import load_catalog

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

    monkeypatch.setattr(invites_mod, "send_log", fake_log)
    await cog.on_member_join(member)

    record = await InviteStore(bot.storage.data_dir).previous_record(1, 5)
    assert record is not None and record.get("inviter_id") == 999
    assert sent, "join log should have been requested"


#: event document layout -----------------------------------------------------


async def test_event_titles_are_not_double_decorated():
    """Catalog titles that already carry an emoji token must not receive a
    second one (live bug: '🚫🚫 Membro banido').

    The event documents point at the catalog copy, so the token of the copy
    is the only decoration. Dry titles (the audit cards) get the theme emoji
    from the document."""
    from card_forms import payload_texts
    from golden_cards import make_bot

    from rosemary.core.card_service import render_card_message

    bot = make_bot()
    ban_emoji = bot.theme.emojis["ban"]

    payload, _allowed = await render_card_message(
        bot,
        1,
        "events.ban",
        {
            "user": "<@2>",
            "user_name": "Ana",
            "user_id": "2",
            "server": "Serv",
            "count": 3,
            "user_avatar": "https://a.b/2.png",
            "inviter": "",
        },
    )
    assert payload is not None
    # The event card is a Components V2: the title is the bold line.
    heading = next(
        (text for text in payload_texts(payload) if text.startswith("**")), ""
    )
    assert heading.count(ban_emoji) == 1, heading

    # The audit card: the document carries the emoji token, the catalog title
    # is dry. The two never meet, so the title has one emoji.
    payload, _allowed = await render_card_message(
        bot, 1, "audit.ban", {"user": "<@2>", "user_avatar": "https://a.b/2.png"}
    )
    assert payload.embed.title.count(ban_emoji) == 1, payload.embed.title


async def test_event_document_has_avatar_author_and_footer():
    """The event documents: the member owns the author line and the footer
    names the server, the count and the id (the id in inline code)."""
    from card_forms import (
        payload_author_icon,
        payload_footer_lines,
        payload_strip,
        payload_texts,
    )
    from golden_cards import make_bot

    from rosemary.core.card_service import render_card_message
    from rosemary.core.theme_package import default_package

    document = default_package().document("events.welcome")
    assert document is not None
    assert document["author"] == "{user_name}"
    assert document["author_icon"] == "{user_avatar}"
    assert "thumbnail" not in document
    assert "`{user_id}`" in document["footer"]

    bot = make_bot()
    payload, _allowed = await render_card_message(
        bot,
        1,
        "events.welcome",
        {
            "user": "<@5>",
            "user_name": "Ana",
            "user_id": "5",
            "server": "Serv",
            "count": 3,
            "user_avatar": "https://a.b/ana.png",
            "inviter": "",
        },
    )
    assert payload is not None
    texts = payload_texts(payload)
    # The author line: the drawn strip carries the name and the picture.
    assert payload_strip(payload).startswith("attachment://author_")
    assert "**Ana**" not in texts, texts
    assert payload_author_icon(payload) == ""
    # The footer names the server, the count and the id (in inline code).
    assert "Serv · #3 · `5`" in payload_footer_lines(payload)


#: catalog templates: real newlines, never literal "\n" ----------------------


def test_every_log_title_has_theme_emoji():
    """Every log title starts with a theme emoji token ({ban}, {gear}...)."""
    import re

    pattern = re.compile(r"^\{[a-z_]+\}")
    for code in ("en-US", "pt-BR"):
        data = load_catalog(code)

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
                assert pattern.match(str(value)), f"{code}:{key} lacks emoji token: {value!r}"


def test_boost_log_fields_are_bold_labeled():
    """Boost logs use the compact field format: emoji + bold label on data lines."""
    for lang, first_label in (("pt-BR", "**{tag} Cargo:**"), ("en-US", "**{tag} Role:**")):
        data = load_catalog(lang)
        for name, entry in data["boost"]["logs"].items():
            desc = entry["description"]
            assert desc.startswith(first_label), f"{lang}:boost.logs.{name}: {desc!r}"


def test_birthday_catalog_keys_match_spec():
    """birthdays.announce is the customizable card key: catalogs follow."""
    for code in ("en-US", "pt-BR"):
        data = load_catalog(code)
        b = data["birthdays"]
        assert "announce" in b and "title" in b["announce"]
        assert "card" not in b, f"{code}: legacy birthdays.card still present"


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
    for code in ("en-US", "pt-BR"):
        data = load_catalog(code)
        strings: list[str] = []
        _flatten(data["moderation"]["logs"], strings)
        _flatten(data["settings"]["log"], strings)
        _flatten(data["settings"]["logs"], strings)
        _flatten(data["invites"]["logs"]["join"], strings)
        _flatten(data["invites"]["logs"]["leave"], strings)
        _flatten(data["boost"]["logs"], strings)
        assert strings, f"{code}: templates missing"
        for text in strings:
            assert "\\n" not in text, f"{code}: literal backslash-n in {text!r}"
