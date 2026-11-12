"""Code-built cards promoted to CardSpecs + themed menu headings.

The user mandate: everything the bot sends is theme-customizable. These tests
pin the render path of every newly promoted card (seed == the code default)
and the heading-splice helper used by interactive menus.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
from card_forms import (
    payload_accent,
    payload_author_icon,
    payload_image,
    payload_strip,
    payload_texts,
    payload_thumbnail,
)
from catalog import load_catalog

import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.core.card_service import (
    render_card_message,
)
from rosemary.core.cards import all_cards, get_card
from rosemary.ui.heading import menu_heading_items
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key

    async def raw(self, guild_id, key):
        return key


def make_bot():
    import tempfile
    from pathlib import Path

    from rosemary.core.storage import GuildStorage
    from rosemary.core.themes import ThemeStore

    bot = MagicMock()
    bot.theme = load_theme()
    bot.translator = FakeTranslator()
    # The card pipeline reads the guild's theme and settings through real
    # objects (a mock would break the await).
    root = Path(tempfile.mkdtemp())
    bot.storage = GuildStorage(root)
    bot._theme_store = ThemeStore(root, themes_dir=root / "themes")
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


#: every promoted spec exists with a catalog title ---------------------------


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
        "debug.panel",
        "boost.home",
    ):
        assert get_card(key) is not None, f"spec missing: {key}"


def test_promoted_spec_titles_in_both_catalogs():
    missing = []
    for spec in all_cards():
        for lang in ("en-US", "pt-BR"):
            data = load_catalog(lang)
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


#: the panel documents render for every panel card ----------------------------


async def test_panel_documents_render():
    """The panel documents render end-to-end with the real catalog.

    The panels point at the catalog for their copy (the heading and the
    body), so the fake translator (which knows no copy) would leave them
    silent: the real catalogs are the point of the test.
    """
    from pathlib import Path

    from rosemary.core.i18n import Translator
    from rosemary.core.theme_package import default_package

    bot = make_bot()
    bot.translator = Translator(Path("language"))

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
        "settings.title",
        "themes.title",
        "boost.home",
    ]
    package = default_package()
    for key in keys:
        assert key in package.documents, f"{key} missing from the package"
        # Must be renderable end-to-end (whatever form the card ships in).
        payload, _allowed = await render_card_message(
            bot,
            1,
            key,
            {**variables, "ping": "", "category": "", "category_label": ""},
        )
        assert payload is not None, f"{key}: no rendered payload"
        assert payload_texts(payload), f"{key}: rendered no text"


async def test_the_notices_are_documents():
    """The notices live in the default package: the document is the source of
    truth (the seed map is gone)."""
    from rosemary.core.theme_package import default_package

    package = default_package()
    notices = [
        key
        for key in package.card_keys()
        if package.metadata(key)["form"] == "text"
    ]
    # The plain-text layer shrank when the event DMs became embeds (the
    # boost invite, the bump winners and the partnership notices), and it
    # grew with the confession ban DM.
    assert len(notices) == 30
    for key in notices:
        assert package.document(key) is not None, key


async def test_the_listings_are_documents():
    """The listings live in the default package: the document is the source of
    truth (the seed map is gone)."""
    from rosemary.core.theme_package import default_package

    package = default_package()
    listings = [
        "about.card",
        "bump.reminder",
        "bump.no_bumps",
        "bump.stats",
        "bump.status",
        "invites.leaderboard",
        "invites.stats",
        "invites.personal",
        "invites.invited_list",
        "invites.server_stats",
        "invites.invited_by",
        "invites.codes",
        "invites.blacklist",
        "birthdays.list",
        "partnerships.list",
        "partnerships.audit",
        "colors.list",
        "reminders.list",
        "tickets.created",
        "trap.notice",
    ]
    for key in listings:
        assert key in package.documents, f"{key} missing from the package"
        # The listing keeps its frame: the title names the list, the body
        # carries the rows.
        document = package.document(key)
        assert document.get("title"), key
        assert document.get("description") or document.get("paragraphs"), key


async def test_bump_stats_carries_the_container_color():
    """/bump stats renders through the document in production: a colorless
    card produced floating text with no container while the code fallback
    showed one. The document color must match the fallback (style
    bump_leaderboard)."""
    bot = make_bot()
    from rosemary.core.theme_package import default_package

    document = default_package().document("bump.stats")
    assert document is not None
    assert document["color"] == bot.theme.style("bump_leaderboard").color
    payload, _allowed = await render_card_message(
        bot, 1, "bump.stats", {"title": "T", "body": "B"}
    )
    assert payload is not None
    assert payload_accent(payload) == bot.theme.color("warning").value


async def test_starboard_card_resolves_via_document():
    """The starboard default lives in the package: the document owns the
    frame (the avatar rides the title, the picture rides the gallery and
    the attribution closes the card)."""
    from pathlib import Path

    from rosemary.core.i18n import Translator
    from rosemary.core.theme_package import default_package

    bot = make_bot()
    # Real catalogs: the document names the attribution copy.
    bot.translator = Translator(Path("language"))
    assert "starboard.card" in default_package().documents
    payload, _allowed = await render_card_message(
        bot,
        1,
        "starboard.card",
        {
            "user": "<@5>",
            "user_name": "Ana",
            "user_avatar": "https://a.b/c.png",
            "stars": 3,
            "state_color": "#F7EFA8",
            "title": "⭐ 3 · #geral",
            "body": "mensagem estrelada",
            "image_url": "https://a.b/img.png",
            "timestamp": "<t:1:R>",
        },
    )
    texts = payload_texts(payload)
    assert any("mensagem estrelada" in text for text in texts)
    # The attribution is the author line: the drawn strip carries the
    # member's display name (and the picture).
    assert payload_strip(payload).startswith("attachment://author_")
    assert not any(text == "**Ana**" for text in texts), texts
    assert not any("<@" in text for text in texts)


#: serverinfo rich builder ---------------------------------------------------


async def test_serverinfo_card_resolves_via_document():
    """The serverinfo default lives in the default package (icon thumbnail,
    stat rows, banner image, ID footer): the document is the source of truth
    (same shadowing regression as the starboard)."""
    from pathlib import Path

    import rosemary.cogs.utility  # noqa: F401  (registers the command)
    from rosemary.core.i18n import Translator
    from rosemary.core.theme_package import default_package

    bot = make_bot()
    # Real catalogs: the document composes stat rows from serverinfo.* keys.
    bot.translator = Translator(Path("language"))
    assert "utility.serverinfo" in default_package().documents
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
    payload, _allowed = await render_card_message(
        bot, 1, "utility.serverinfo", variables
    )
    assert payload is not None
    texts = payload_texts(payload)
    # The name of the server lives in the drawn strip (the author line is
    # a picture, not a line of text).
    assert payload_strip(payload).startswith("attachment://author_")
    assert not any("Overdose Community" in text for text in texts), texts
    # The document carries the stat rows (channel breakdown, verification).
    assert any("34" in text and "12" in text for text in texts), texts
    assert any("120" in text and "10" in text for text in texts), texts
    assert any("Médio" in text for text in texts), texts
    # Exactly one icon: the server icon rides the author line (the drawn
    # strip), the banner rides the image.
    assert payload_strip(payload).startswith("attachment://author_")
    assert payload_author_icon(payload) == ""
    assert payload_thumbnail(payload) == ""
    assert payload_image(payload) == "https://a.b/banner.png"


async def test_serverinfo_card_degrades_without_banner_and_icon():
    """No banner -> the image slot drops itself; no icon -> no thumbnail
    instead of a 400ing the payload."""
    from pathlib import Path

    import rosemary.cogs.utility  # noqa: F401  (registers the command)
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
    payload, _allowed = await render_card_message(
        bot, 1, "utility.serverinfo", variables
    )
    assert payload is not None
    texts = payload_texts(payload)
    assert any("Serv" in text for text in texts)
    assert not any("servidor de testes" in text for text in texts)
    # No icon, no banner: no empty image rides the card (the strip draws
    # the initial of the server's name).
    assert payload_strip(payload).startswith("attachment://author_")
    assert payload_author_icon(payload) == ""
    assert payload_thumbnail(payload) == ""
    assert payload_image(payload) == ""


async def test_serverinfo_send_does_not_ping_owner(tmp_path):
    """The owner mention rendered in the card must not ping: the send uses
    the card's own AllowedMentions (no `{@owner}` in the default doc)."""
    from pathlib import Path
    from unittest.mock import AsyncMock

    import rosemary.cogs.info  # noqa: F401  (registers the builder)
    from rosemary.cogs.info import InfoCog
    from rosemary.core.i18n import Translator
    from rosemary.core.themes import ThemeStore

    bot = make_bot()
    bot.translator = Translator(Path("language"))
    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    from rosemary.core.storage import GuildStorage

    bot.storage = GuildStorage(tmp_path)
    cog = InfoCog(bot)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.name = "Serv"
    guild.owner_id = 42
    guild.member_count = 10
    guild.roles = []
    guild.text_channels = []
    guild.voice_channels = []
    guild.premium_subscription_count = 0
    guild.emojis = []
    guild.stickers = []
    guild.description = None
    guild.icon = None
    guild.banner = None
    guild.created_at = None
    guild.verification_level = MagicMock()
    guild.verification_level.name = "low"

    ctx = MagicMock()
    ctx.guild = guild
    ctx.guild_id = 1
    ctx.respond = AsyncMock()

    await cog.serverinfo.callback(cog, ctx)

    kwargs = ctx.respond.await_args.kwargs
    allowed = kwargs.get("allowed_mentions")
    assert allowed is not None, "serverinfo must pass explicit AllowedMentions"
    assert not (allowed.users or []), allowed


#: menu heading splice -------------------------------------------------------


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


#: url placeholder contract ---------------------------------------------------


async def test_document_url_placeholders_come_from_contract():
    """Regression (live 400 on confessions): a ``{placeholder}`` left literal
    in a thumbnail/gallery/button URL is rejected by Discord ("Not a well
    formed URL"). Every placeholder in a URL position of a document must
    belong to the card's variable contract, which the send site fills.
    """
    import re

    from rosemary.core.theme_package import default_package

    def urls_in(node, found):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("url", "thumbnail", "image") and isinstance(value, str):
                    found.append(value)
                elif key == "urls" and isinstance(value, list):
                    found.extend(u for u in value if isinstance(u, str))
                else:
                    urls_in(value, found)
        elif isinstance(node, list):
            for item in node:
                urls_in(item, found)
        return found

    package = default_package()
    problems = []
    for key in package.card_keys():
        doc = package.document(key) or {}
        contract = set(package.metadata(key)["variables"])
        for url in urls_in(doc, []):
            names = set(re.findall(r"\{(\w+)\}", url))
            outside = names - contract
            if outside:
                problems.append(f"{key}: {sorted(outside)} in {url!r}")
    assert problems == []
