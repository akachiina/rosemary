"""Code-built cards promoted to CardSpecs + themed menu headings.

The user mandate: everything the bot sends is theme-customizable. These tests
pin the render path of every newly promoted card (seed == the code default)
and the heading-splice helper used by interactive menus.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import yaml

import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.core.card_service import default_document, menu_heading_items
from rosemary.core.cards import all_cards, get_card
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key

    async def raw(self, guild_id, key):
        return key


def make_bot():
    bot = MagicMock()
    bot.theme = load_theme()
    bot.translator = FakeTranslator()
    bot._theme_store = MagicMock()
    return bot


def container_texts(view) -> list[str]:
    texts = []

    def walk(node):
        if isinstance(node, discord.ui.TextDisplay):
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


# -- every promoted spec exists with a catalog title ---------------------------


def test_promoted_content_specs_registered():
    for key in (
        "starboard.card",
        "utility.ping",
        "utility.serverinfo",
        "birthdays.list",
        "partnerships.list",
        "partnerships.audit",
        "moderation.warnings",
        "bump.stats",
        "settings.title",
        "themes.title",
        "debug.title",
        "boost.home",
    ):
        assert get_card(key) is not None, f"spec missing: {key}"


def test_promoted_spec_titles_in_both_catalogs():
    missing = []
    for spec in all_cards():
        for lang in ("en-US", "pt-BR"):
            with open(f"language/{lang}.yaml", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            node = data["card"]
            try:
                for part in spec.key.split("."):
                    node = node[part]
            except (KeyError, TypeError):
                missing.append(f"{lang}:card.{spec.key}")
                continue
            if not isinstance(node.get("title"), str):
                missing.append(f"{lang}:card.{spec.key}.title")
    assert missing == []


# -- seed documents render for every content card ------------------------------


async def test_seed_documents_render():
    bot = make_bot()
    from rosemary.core.card_service import SEED_PARTS_BY_KEY

    variables = {
        "ms": "42",
        "body": "linha 1\nlinha 2",
        "title": "Título",
        "user": "<@5>",
        "server": "Serv",
        "owner": "@dono",
        "members": 10,
        "roles": 3,
        "channels": 7,
        "created_at": "<t:1111111111:D>",
    }
    keys = [
        "utility.ping",
        "birthdays.list",
        "partnerships.list",
        "partnerships.audit",
        "moderation.warnings",
        "bump.stats",
        "settings.title",
        "themes.title",
        "debug.title",
        "boost.home",
    ]
    for key in keys:
        assert key in SEED_PARTS_BY_KEY, f"{key} missing from seed map"
        doc = await default_document(bot, 1, key)
        assert doc is not None, f"{key}: no default document"
        assert doc["blocks"], f"{key}: empty document"
        # Must be renderable end-to-end.
        from rosemary.core.card_service import render_document

        view = await render_document(
            bot, doc, {**variables, "ping": "", "category": "", "category_label": ""},
            guild_id=1, card_key=key,
        )
        assert view.children, f"{key}: rendered no items"


async def test_starboard_card_resolves_via_builder_not_seed():
    """The builder owns the starboard default; a seed entry here would shadow
    it with bare {title}/{body} -- the exact bug that made live posts plain
    (regression for the seed-map shadowing)."""
    import rosemary.cogs.starboard  # noqa: F401  (registers the builder)
    from rosemary.core.card_service import SEED_PARTS_BY_KEY, render_document

    bot = make_bot()
    assert "starboard.card" not in SEED_PARTS_BY_KEY, (
        "a seed entry would shadow the registered rich builder"
    )
    doc = await default_document(bot, 1, "starboard.card")
    assert doc is not None
    view = await render_document(
        bot,
        doc,
        {
            "user": "<@5>",
            "user_name": "Ana",
            "user_avatar": "https://a.b/c.png",
            "stars": 3,
            "title": "⭐ 3 • #geral",
            "body": "mensagem estrelada",
            "image_url": "https://a.b/img.png",
            "timestamp": "<t:1:R>",
        },
        guild_id=1,
        card_key="starboard.card",
    )
    texts = container_texts(view)
    assert any("mensagem estrelada" in text for text in texts)
    # Builder signature: thumbnail section + author footer, not the bare seed.
    assert any("-# by <@5>" in text for text in texts), texts


# -- serverinfo rich builder ---------------------------------------------------


async def test_serverinfo_card_resolves_via_builder_not_seed():
    """The serverinfo default lives in a registered rich builder (icon section,
    stat columns, banner gallery, ID footer); a seed-map entry would shadow it
    with bare key/value lines -- same shadowing regression as the starboard."""
    from pathlib import Path

    import rosemary.cogs.utility  # noqa: F401  (registers the builder)
    from rosemary.core.card_service import SEED_PARTS_BY_KEY, render_document
    from rosemary.core.i18n import Translator

    bot = make_bot()
    # Real catalogs: the builder composes stat rows from serverinfo.* keys.
    bot.translator = Translator(Path("language"))
    assert "utility.serverinfo" not in SEED_PARTS_BY_KEY, (
        "a seed entry would shadow the registered rich builder"
    )
    variables = {
        "server": "Overdose Community",
        "server_icon": "https://a.b/icon.png",
        "banner_url": "https://a.b/banner.png",
        "owner": "<@42>",
        "members": 623,
        "channels": 46,
        "text_channels": 34,
        "voice_channels": 12,
        "roles": 103,
        "boosts": 3,
        "emojis": 120,
        "stickers": 10,
        "description": "servidor de testes",
        "verification": "Médio",
        "created_at": "<t:1111111111:D>",
        "created_rel": "<t:1111111111:R>",
        "server_id": "1388731936914280540",
    }
    doc = await default_document(bot, 1, "utility.serverinfo")
    assert doc is not None
    view = await render_document(
        bot, doc, variables, guild_id=1, card_key="utility.serverinfo"
    )
    texts = container_texts(view)
    assert any("Overdose Community" in text for text in texts)
    # Builder signature: stat lines (channel breakdown) + verification row.
    assert any("34" in text and "12" in text for text in texts), texts
    assert any("120" in text and "10" in text for text in texts), texts
    assert any("Médio" in text for text in texts), texts
    # Exactly one icon: the header accessory (V2 sections stack texts, so a
    # second section would duplicate the thumbnail).
    from rosemary.core.cards import build_items

    items = build_items(bot.theme, doc, variables)
    sections = [i for i in items[0].items if type(i).__name__ == "Section"]
    assert len(sections) == 1
    assert type(sections[0].accessory).__name__ == "Thumbnail"


async def test_serverinfo_builder_degrades_without_banner_and_icon():
    """No banner -> gallery resolves empty and is skipped; no icon -> the
    section accessory is omitted instead of 400ing the payload."""
    from pathlib import Path

    import rosemary.cogs.utility  # noqa: F401  (registers the builder)
    from rosemary.core.card_service import render_document
    from rosemary.core.i18n import Translator

    bot = make_bot()
    bot.translator = Translator(Path("language"))
    variables = {
        "server": "Serv",
        "server_icon": "",
        "banner_url": "",
        "owner": "<@42>",
        "members": 10,
        "channels": 7,
        "text_channels": 6,
        "voice_channels": 1,
        "roles": 3,
        "boosts": 0,
        "emojis": 0,
        "stickers": 0,
        "description": "",
        "verification": "Baixo",
        "created_at": "<t:1111111111:D>",
        "created_rel": "<t:1111111111:R>",
        "server_id": "123",
    }
    doc = await default_document(bot, 1, "utility.serverinfo")
    view = await render_document(
        bot, doc, variables, guild_id=1, card_key="utility.serverinfo"
    )
    assert view.children
    texts = container_texts(view)
    assert any("Serv" in text for text in texts)
    assert not any("servidor de testes" in text for text in texts)


# -- menu heading splice -------------------------------------------------------


async def test_menu_heading_empty_without_theme(tmp_path):
    bot = make_bot()
    from rosemary.core.themes import ThemeStore

    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    items = await menu_heading_items(bot, 1, "settings.title")
    assert items == []


async def test_menu_heading_splices_themed_text_and_rejects_container(tmp_path):
    from rosemary.core.themes import ThemeStore, preload_themes

    themes_dir = tmp_path / "themes"
    themes_dir.mkdir()
    (themes_dir / "t.yaml").write_text(
        "name: t\n"
        "cards:\n"
        "  settings.title:\n"
        "    - type: 10\n"
        "      content: '# Painel customizado'\n",
        encoding="utf-8",
    )
    bot = make_bot()
    bot.translator = FakeTranslator()
    bot._theme_store = ThemeStore(tmp_path, themes_dir=themes_dir)
    bot.guilds = [MagicMock(id=1)]

    async def setup():
        await bot._theme_store.set_active(1, "t")
        await preload_themes(bot)

    await setup()
    items = await menu_heading_items(bot, 1, "settings.title")
    assert len(items) == 1
    assert isinstance(items[0], discord.ui.TextDisplay)
    assert "Painel customizado" in items[0].content

    # A container heading is rejected (menus bring their own container).
    (themes_dir / "t.yaml").write_text(
        "name: t\n"
        "cards:\n"
        "  settings.title:\n"
        "    - type: 17\n"
        "      components:\n"
        "        - type: 10\n"
        "          content: 'nope'\n",
        encoding="utf-8",
    )
    bot._theme_store.invalidate_guild(1)
    await preload_themes(bot)
    await preload_themes(bot)
    items = await menu_heading_items(bot, 1, "settings.title")
    assert items == []
