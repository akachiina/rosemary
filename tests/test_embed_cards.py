"""Embed-form cards: the classic rich message as a theme alternative.

A theme ``cards:`` entry may be written as an ``embed:`` instead of raw
Components V2. These tests pin the whole path: load-time conversion +
validation (:mod:`rosemary.core.embed_convert`), render-time resolution
(:func:`rosemary.core.cards.resolve_embed`), the payload indirection that
makes both shapes interchangeable at send sites
(:class:`rosemary.core.card_service.CardPayload`) and the fallback rule
(an invalid embed document never breaks a send — the caller's default wins).
"""

from __future__ import annotations

import discord
import pytest

import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.core.cards import is_embed_document, resolve_embed


class _Translator:
    async def t(self, guild_id, key, **kwargs):
        if kwargs:
            return f"{key}:{kwargs}"
        return key

    async def raw(self, guild_id, key):
        return key


def _make_bot(tmp_path, themes_yaml: str):
    from rosemary.core.storage import GuildStorage
    from rosemary.core.themes import ThemeStore
    from rosemary.ui.theme import load_theme

    class Bot:
        pass

    bot = Bot()
    bot.storage = GuildStorage(tmp_path)
    bot.theme = load_theme()
    bot.translator = _Translator()
    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    (tmp_path / "themes").mkdir(exist_ok=True)
    (tmp_path / "themes" / "t.yaml").write_text(themes_yaml, encoding="utf-8")
    bot.guilds = [type("G", (), {"id": 1})()]
    return bot


async def _activate(bot) -> None:
        await bot._theme_store.set_active(1, "t")
        from rosemary.core.themes import preload_themes

        await preload_themes(bot)


EMBED_THEME = """\
name: t
colors:
  brand: "#ff8800"
  info: "#3399ff"
emojis:
  clown: "🤡"
cards:
  about.card:
    embed:
      title: "Hi {clown}!"
      description: "Body with {@user} ping"
      color: brand
      footer: "from {server}"
    buttons:
      - label: "Docs"
        url: "https://example.com"
      - label: "Act"
        action: dismiss
"""


def test_embed_entry_converts_and_tags_kind(tmp_path):
    """An ``embed:`` entry loads into a ``kind: embed`` document with buttons."""
    from rosemary.core.embed_convert import convert_embed_entry

    doc = convert_embed_entry(
        "about.card",
        {
            "embed": {"title": "T", "description": "D", "color": "brand"},
            "buttons": [{"label": "L", "url": "https://x"}],
        },
    )
    assert doc["kind"] == "embed"
    assert doc["embed"]["title"] == "T"
    assert doc["embed"]["color"] == "brand"
    assert doc["buttons"] and doc["buttons"][0]["id"].startswith("b_")


def test_embed_entry_validation_rejects_bad_shape(tmp_path):
    from rosemary.core.embed_convert import ThemeError, convert_embed_entry

    with pytest.raises(ThemeError) as exc:
        convert_embed_entry(
            "about.card",
            {
                "embed": {
                    "title": "x" * 300,
                    "fields": [{"name": "", "value": 1}],
                    "image": "notaurl",
                },
                "buttons": [{"label": "L", "url": "ftp://bad"}],
            },
        )
    codes = [issue.code for issue in exc.value.issues]
    assert "embed_too_long" in codes
    assert "embed_field_shape" in codes
    assert codes.count("embed_bad_url") == 1  # image; button url uses v2 code


def test_resolve_embed_fills_placeholders_and_mentions():
    from rosemary.ui.theme import load_theme

    doc = {
        "v": 1,
        "kind": "embed",
        "embed": {
            "title": "Hi {clown}!",
            "description": "Ping {@user} here",
            "footer": {"text": "by {server}"},
        },
        "buttons": [],
    }
    mapping = {"clown": "🤡", "user": "<@42>", "server": "Serv"}
    embed, buttons = resolve_embed(doc, load_theme(), mapping)
    assert embed.title == "Hi 🤡!"
    assert "<@42>" in embed.description
    assert embed.footer.text == "by Serv"
    assert buttons == []


def test_embed_document_mention_ids_walk_embed_fields():
    from rosemary.core.cards import document_mention_ids

    doc = {
        "v": 1,
        "kind": "embed",
        "embed": {
            "title": "T",
            "description": "hello {@user}",
            "fields": [{"name": "n", "value": "and {@inviter}"}],
        },
        "buttons": [],
    }
    mapping = {"user": "<@42>", "inviter": "<@7>"}
    users, roles = document_mention_ids(doc, mapping)
    ids = set(users) | set(roles)
    assert {42, 7} <= ids


async def test_embed_buttons_build_classic_view_with_dispatch_ids():
    from rosemary.core.card_actions import parse_custom_id
    from rosemary.core.cards import build_embed_view

    buttons = [
        {"label": "Docs", "url": "https://example.com", "id": "b_link"},
        {"label": "Act", "action": "dismiss", "id": "b_act"},
    ]
    view = build_embed_view(buttons, {}, "about.card")
    assert view is not None
    assert view.timeout is None  # persistent
    by_id = {b.custom_id: b for b in view.children if getattr(b, "custom_id", None)}
    parsed = parse_custom_id(by_id["cardact:about.card:b_act"].custom_id)
    assert parsed == ("cardact", "about.card", "b_act")
    link = [b for b in view.children if b.url == "https://example.com"]
    assert link and link[0].disabled is False


async def test_message_kwargs_dispatches_both_shapes():
    from rosemary.core.card_service import CardPayload

    v2 = CardPayload(view=discord.ui.DesignerView())
    assert list(v2.message_kwargs()) == ["view"]

    embed = discord.Embed(title="T")
    emb = CardPayload(embed=embed, view=discord.ui.View(timeout=None))
    kwargs = emb.message_kwargs()
    assert kwargs["embed"] is embed
    assert "view" in kwargs


async def test_render_card_message_returns_embed_payload(tmp_path):
    """Full pipeline: an embed-themed card renders as ``embed=`` kwargs."""
    from rosemary.core.card_service import render_card_message

    bot = _make_bot(tmp_path, EMBED_THEME)
    await _activate(bot)
    payload, _allowed = await render_card_message(bot, 1, "about.card", {"server": "S"})
    assert payload is not None and payload.embed is not None
    kwargs = payload.message_kwargs()
    assert "embed" in kwargs and kwargs["embed"].title == "Hi 🤡!"
    assert payload.view is not None  # classic row with the two buttons


async def test_invalid_embed_document_falls_back_to_default(tmp_path):
    """A broken embed doc (unknown color token) can never break a send.

    Load validation rejects the card up front, so the guild keeps the
    default catalog card instead of the themed one.
    """
    from rosemary.core.card_service import render_card_message

    bad = EMBED_THEME.replace("color: brand", "color: no_such_token")
    bot = _make_bot(tmp_path, bad)
    await _activate(bot)
    payload, allowed = await render_card_message(bot, 1, "about.card", {})
    assert payload is not None
    assert payload.embed is None  # default V2 card, not the broken embed
    assert allowed is not None


def test_is_embed_document():
    assert is_embed_document({"kind": "embed"})
    assert not is_embed_document({"kind": "v2"})
    assert not is_embed_document({})
