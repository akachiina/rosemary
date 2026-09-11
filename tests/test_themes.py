"""Theme system: raw Discord V2 conversion, ThemeStore, per-guild resolution.

These tests lock the pieces the /themes UI stands on: the raw-V2 converter
(:mod:`rosemary.core.v2_convert`) accepts exactly the Discord component shapes
theme authors copy from discord.dev and rejects everything else; the store
loads/imports/validates files and resolves per guild; the resolution helpers
(:func:`card_document`, :func:`card_origin`, pings) decide what sends render.
"""

from __future__ import annotations

import pytest

from rosemary.core.cards import CardIssue
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import (
    RosemaryTheme,
    ThemeStore,
    card_document,
    card_origin,
    load_theme_file,
    theme_for,
    theme_store,
)
from rosemary.core.v2_convert import ThemeError, convert_card_entry, convert_raw_document
from rosemary.ui.theme import load_theme

# -- v2_convert ---------------------------------------------------------------


def test_convert_full_document_preserves_order_and_mixing():
    """text -> container -> text -> lone image, exactly as authored."""
    raw = [
        {"type": 10, "content": "before"},
        {
            "type": 17,
            "accent_color": 0xFF8FC7,
            "components": [{"type": 10, "content": "inside"}],
        },
        {"type": "text", "content": "after"},  # name alias also accepted
        {"type": 12, "items": [{"media": {"url": "https://example.com/a.png"}}]},
    ]
    blocks = convert_raw_document(raw)
    assert [block["type"] for block in blocks] == [
        "text",
        "container",
        "text",
        "gallery",
    ]
    assert blocks[1]["color"] == "#ff8fc7"
    assert blocks[1]["children"] == [{"type": "text", "body": "inside"}]
    assert blocks[3]["urls"] == ["https://example.com/a.png"]


def test_convert_section_with_thumbnail_and_row():
    raw = [
        {
            "type": 9,
            "components": [{"type": 10, "content": "hello"}],
            "accessory": {"type": 11, "media": {"url": "{user_avatar}"}},
        },
        {
            "type": 1,
            "components": [
                {"type": 2, "style": 5, "label": "Site", "url": "https://x.y"},
            ],
        },
    ]
    blocks = convert_raw_document(raw)
    assert blocks[0]["type"] == "section"
    assert blocks[0]["accessory"] == {"type": "thumbnail", "url": "{user_avatar}"}
    assert blocks[1] == {
        "type": "row",
        "buttons": [{"label": "Site", "url": "https://x.y"}],
    }


def test_convert_separator_spacing_and_invisible():
    blocks = convert_raw_document(
        [
            {"type": 14, "spacing": 2, "divider": True},
            {"type": 14, "divider": False},
        ]
    )
    assert blocks[0] == {"type": "divider", "spacing": "large"}
    assert blocks[1]["visible"] is False


def test_convert_action_button_convention():
    raw = [
        {
            "type": 1,
            "components": [
                {"type": 2, "style": 1, "label": "Open", "custom_id": "cardact:open_ticket:report"},
                {"type": 2, "style": 2, "label": "Bye", "custom_id": "cardact:dismiss:x"},
            ],
        }
    ]
    buttons = convert_raw_document(raw)[0]["buttons"]
    assert buttons[0]["action"] == "open_ticket"
    assert buttons[0]["ticket_type"] == "report"
    assert buttons[1]["action"] == "dismiss"


def test_convert_rejects_unknown_and_nested_containers():
    with pytest.raises(ThemeError):
        convert_raw_document([{"type": 3, "options": []}])  # select unsupported
    with pytest.raises(ThemeError):
        convert_raw_document(
            [
                {
                    "type": 17,
                    "components": [
                        {"type": 17, "components": [{"type": 10, "content": "x"}]}
                    ],
                }
            ]
        )


def test_convert_string_shortcut_and_validation():
    doc = convert_card_entry("warn.dm", "You were warned in {guild}")
    assert doc["blocks"][0]["body"] == "You were warned in {guild}"
    with pytest.raises(ThemeError):
        convert_card_entry("bad.card", [{"type": 99}])


# -- ThemeStore ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir()
    (themes_dir / "cute.yaml").write_text(
        "name: cute\n"
        "colors:\n"
        '  brand: "#ff8fc7"\n'
        "emojis:\n"
        '  star: "🌟"\n'
        "cards:\n"
        "  about.card:\n"
        "    - type: 10\n"
        '      content: "## {star} Welcome!"\n'
        "  warn.dm: plain override\n"
        "pings:\n"
        "  bump.thank_you: false\n",
        encoding="utf-8",
    )
    return ThemeStore(tmp_path, themes_dir=themes_dir)


async def test_store_lists_loads_and_selects(store, tmp_path):
    assert store.list_all(1) == ["cute"]
    theme = store.load("cute")
    assert theme.name == "cute"
    assert theme.emojis["star"] == "🌟"
    await store.set_active(1, "cute")
    assert await store.get_active(1) == "cute"
    # Selection file is its own JSON, separate from settings.
    assert (tmp_path / "1" / "themes.json").exists()


async def test_store_import_validates_before_writing(store, tmp_path):
    ok = await store.import_theme(
        1,
        "My Theme.YAML",
        b'name: my_theme\npings:\n  about.card: true\n',
    )
    assert ok == "my_theme"
    assert store.list_guild(1) == ["my_theme"]
    with pytest.raises(ThemeError):
        await store.import_theme(1, "broken.yaml", b"colors: [not, a, map\n")
    assert "broken" not in store.list_guild(1)  # nothing written on rejection


async def test_store_remove_clears_active_selection(store):
    await store.set_active(1, "cute")
    await store.import_theme(1, "local.yaml", b"name: local\n")
    removed = await store.remove_guild_theme(1, "local")
    assert removed is True
    assert await store.get_active(1) == "cute"  # untouched
    await store.remove_guild_theme(1, "cute")  # guild never owned it
    assert await store.get_active(1) == "cute"


async def test_effective_theme_falls_back_when_file_missing(store, tmp_path):
    class Bot:
        pass

    bot = Bot()
    bot.storage = GuildStorage(tmp_path)
    bot.theme = load_theme()
    await store.set_active(1, "cute")
    (tmp_path / "themes" / "cute.yaml").unlink()
    assert await store.effective_theme(bot, 1) is bot.theme  # never breaks sends


def test_theme_store_binds_once(tmp_path):
    class Bot:
        storage = GuildStorage(tmp_path)

    bot = Bot()
    bot._theme_store = ThemeStore(tmp_path)
    assert theme_store(bot) is theme_store(bot)  # bot-owned store is reused


# -- resolution ---------------------------------------------------------------


class ThemeBot:
    def __init__(self, tmp_path, store, *, with_cache=True):
        from rosemary.core import card_specs  # noqa: F401  (fills the registry)

        self.storage = GuildStorage(tmp_path)
        self.theme = load_theme()
        self.translator = type("T", (), {})()
        self._theme_store = store
        if with_cache:
            self._theme_cache = {}


@pytest.mark.parametrize("cache", [True, False])
async def test_card_document_resolves_theme_override(tmp_path, store, cache):
    bot = ThemeBot(tmp_path, store, with_cache=cache)
    bot.guilds = [type("G", (), {"id": 1})()]
    await store.set_active(1, "cute")
    from rosemary.core.themes import preload_themes

    await preload_themes(bot)
    doc = await card_document(bot, 1, "about.card")
    assert doc["blocks"][0]["body"] == "## {star} Welcome!"
    warn_doc = await card_document(bot, 1, "warn.dm")
    assert warn_doc["blocks"][0]["body"] == "plain override"
    assert await card_document(bot, 1, "no.override") is None
    assert await card_document(bot, None, "about.card") is None


async def test_card_origin_reports_theme_or_none(tmp_path, store):
    bot = ThemeBot(tmp_path, store)
    bot.guilds = [type("G", (), {"id": 1})()]
    await store.set_active(1, "cute")
    from rosemary.core.themes import preload_themes

    await preload_themes(bot)
    assert card_origin(bot, 1, "about.card") == ("cute", "about.card")
    assert card_origin(bot, 1, "other.card") == (None, None)
    assert card_origin(bot, None, "about.card") == (None, None)


async def test_theme_for_synchronous_snapshot(tmp_path, store):
    bot = ThemeBot(tmp_path, store)
    bot.guilds = [type("G", (), {"id": 1})()]
    # Cold snapshot: built-in theme, never a crash.
    assert theme_for(bot, 1) is bot.theme
    await store.set_active(1, "cute")
    from rosemary.core.themes import preload_themes

    await preload_themes(bot)
    assert isinstance(theme_for(bot, 1), RosemaryTheme)
    assert theme_for(bot, 1).name == "cute"


def test_load_theme_file_rejects_bad_names_and_colors(tmp_path):
    bad_name = tmp_path / "Bad Name.yaml"
    bad_name.write_text("name: Bad Name\n", encoding="utf-8")
    with pytest.raises(ThemeError):
        load_theme_file(bad_name)
    bad_color = tmp_path / "ugly.yaml"
    bad_color.write_text('colors:\n  brand: "nope"\n', encoding="utf-8")
    with pytest.raises(ThemeError):
        load_theme_file(bad_color)


def test_themeerror_carries_issue_codes():
    err = ThemeError([CardIssue("theme_bad_name", (("name", "X"),))])
    assert "theme_bad_name" in str(err)
