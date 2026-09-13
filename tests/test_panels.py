"""Panel repaint registry + the bugs found by the ridiculous test theme.

Covers: active-theme emoji resolution (render used the built-in theme), the
in-place panel repaint on theme change, and stable themed button ids across
loads (posted message vs. boot-registered dispatch view).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import discord

from rosemary.core.panels import (
    on_setting_changed,
    register,
    repaint_guild,
    unregister,
)
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore, preload_themes, theme_for
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


class FakeBot:
    def __init__(self, tmp_path):
        self.storage = GuildStorage(tmp_path)
        self.theme = load_theme()
        self.translator = FakeTranslator()
        self._theme_store = None
        self._theme_cache = {}
        self.guild = None
        self.guilds = []
        self.views = []
        self._card_action_views = {}

    def get_guild(self, guild_id):
        return self.guild

    def add_view(self, view):
        self.views.append(view)


async def _store_with_clown(tmp_path: Path, name: str = "cute"):
    """A ThemeStore whose theme defines the ``clown`` emoji token."""
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir(exist_ok=True)
    (themes_dir / f"{name}.yaml").write_text(
        f"name: {name}\nemojis:\n  clown: '\U0001f921'\n", encoding="utf-8"
    )
    return ThemeStore(tmp_path, themes_dir=themes_dir)


async def test_render_uses_active_theme_emojis(tmp_path):
    """Regression: themed cards resolved emoji tokens against the BUILT-IN
    theme, so theme-only tokens like {clown} stayed literal (live bug)."""
    from rosemary.core.cards import build_items

    bot = FakeBot(tmp_path)
    bot._theme_store = await _store_with_clown(tmp_path)
    bot.guild = MagicMock()
    bot.guild.id = 1
    bot.guilds = [bot.guild]
    await bot._theme_store.set_active(1, "cute")
    await preload_themes(bot)

    doc = {"v": 1, "blocks": [{"type": "text", "body": "hello {clown}"}]}
    items = build_items(theme_for(bot, 1), doc, {})
    texts = [
        item.content
        for item in _walk(items)
        if isinstance(item, discord.ui.TextDisplay)
    ]
    assert any("\U0001f921" in t for t in texts), texts
    assert not any("{clown}" in t for t in texts), texts


async def test_render_card_message_uses_active_theme_emojis(tmp_path):
    """The full send pipeline (render_card_message) resolves the active theme."""
    from rosemary.core.card_service import render_card_message

    bot = FakeBot(tmp_path)
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir(exist_ok=True)
    (themes_dir / "cute.yaml").write_text(
        "name: cute\nemojis:\n  clown: '\U0001f921'\n"
        "cards:\n  about.card:\n    - type: 10\n      content: 'hi {clown}'\n",
        encoding="utf-8",
    )
    bot._theme_store = ThemeStore(tmp_path, themes_dir=themes_dir)
    bot.guild = MagicMock()
    bot.guild.id = 1
    bot.guilds = [bot.guild]
    await bot._theme_store.set_active(1, "cute")
    await preload_themes(bot)

    view, _allowed = await render_card_message(bot, 1, "about.card", {})
    assert view is not None
    texts = [
        item.content
        for item in _walk(view.children)
        if isinstance(item, discord.ui.TextDisplay)
    ]
    assert any("\U0001f921" in t for t in texts), texts
    assert not any("{clown}" in t for t in texts), texts


async def test_container_nested_action_buttons_match_dispatch(tmp_path):
    """Regression: _build_block only passed card_key to TOP-LEVEL rows, so
    buttons inside a container (type 17) got cardact::<id> while the boot
    dispatch views expected cardact:tickets.panel::<id> — clicks died with
    "the application did not respond" (live bug report)."""
    from rosemary.core.card_actions import sync_guild
    from rosemary.core.cards import maybe_view

    bot = FakeBot(tmp_path)
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir(exist_ok=True)
    (themes_dir / "cute.yaml").write_text(
        "name: cute\n"
        "cards:\n"
        "  tickets.panel:\n"
        "    - type: 17\n"
        "      components:\n"
        "        - type: 10\n"
        "          content: 'Painel'\n"
        "        - type: 1\n"
        "          components:\n"
        "            - type: 2\n"
        "              style: 1\n"
        "              label: 'Abrir'\n"
        "              custom_id: 'cardact:open_ticket:report'\n",
        encoding="utf-8",
    )
    bot._theme_store = ThemeStore(tmp_path, themes_dir=themes_dir)
    bot.guilds = [MagicMock(id=1)]
    await bot._theme_store.set_active(1, "cute")
    await preload_themes(bot)

    view = await maybe_view(bot, 1, "tickets.panel")
    assert view is not None

    def buttons(items):
        found = []
        for item in items:
            if isinstance(item, discord.ui.Button):
                found.append(item.custom_id)
            for attr in ("children", "items"):
                found.extend(buttons(getattr(item, attr, None) or []))
        return found

    message_ids = set(buttons(view.children))
    await sync_guild(bot, 1)
    dispatch_ids = set()
    for registered in bot._card_action_views.values():
        dispatch_ids.update(buttons(registered.children))
    assert message_ids, "no action buttons rendered"
    assert message_ids == dispatch_ids


async def test_invalidate_guild_reloads_edited_files(tmp_path):
    """"/themes" Reload files: cache drop makes file edits visible live."""
    from rosemary.core.themes import card_document

    bot = FakeBot(tmp_path)
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir(exist_ok=True)
    yaml_path = themes_dir / "cute.yaml"
    yaml_path.write_text(
        "name: cute\ncards:\n  about.card:\n    - type: 10\n      content: 'v1'\n",
        encoding="utf-8",
    )
    bot._theme_store = ThemeStore(tmp_path, themes_dir=themes_dir)
    bot.guilds = [MagicMock(id=1)]
    await bot._theme_store.set_active(1, "cute")
    await preload_themes(bot)

    doc = await card_document(bot, 1, "about.card")
    assert doc["blocks"][0]["body"] == "v1"

    yaml_path.write_text(
        "name: cute\ncards:\n  about.card:\n    - type: 10\n      content: 'v2'\n",
        encoding="utf-8",
    )
    bot._theme_store.invalidate_guild(1)
    await preload_themes(bot)
    doc = await card_document(bot, 1, "about.card")
    assert doc["blocks"][0]["body"] == "v2"


async def test_themed_button_ids_are_stable_across_loads(tmp_path):
    """Regression: random per-load ids meant the posted panel's buttons and
    the boot-registered dispatch views never matched (silent dead clicks)."""
    from rosemary.core.themes import load_theme_file

    themes_dir = tmp_path / "themes"
    themes_dir.mkdir(exist_ok=True)
    body = (
        "name: cute\n"
        "cards:\n"
        "  tickets.panel:\n"
        "    - type: 1\n"
        "      components:\n"
        "        - type: 2\n"
        "          style: 1\n"
        "          label: 'Open'\n"
        "          custom_id: 'cardact:open_ticket:report'\n"
    )
    (themes_dir / "cute.yaml").write_text(body, encoding="utf-8")

    def button_ids():
        theme = load_theme_file(themes_dir / "cute.yaml")
        doc = theme.cards["tickets.panel"]
        return [
            b["id"]
            for block in doc["blocks"]
            for b in block.get("buttons", [])
        ]

    first = button_ids()
    second = button_ids()
    assert first and first == second


async def test_theme_change_repaints_registered_panels(tmp_path):
    """on_theme_changed repaints every registered panel for the guild."""
    bot = FakeBot(tmp_path)
    calls = []

    async def painter(bot_, guild_id):
        calls.append(guild_id)

    register("fake", painter)
    try:
        from rosemary.core.panels import on_theme_changed

        await on_theme_changed(bot, 7)
        assert calls == [7]
    finally:
        unregister("fake")


async def test_setting_change_repaints_only_interested_panels(tmp_path):
    bot = FakeBot(tmp_path)
    calls = []

    async def interested(bot_, guild_id):
        calls.append(("interested", guild_id))

    async def bystander(bot_, guild_id):
        calls.append(("bystander", guild_id))

    register("a", interested, setting_keys=("tickets.panel_channel",))
    register("b", bystander)
    try:
        await on_setting_changed(bot, 7, "tickets.panel_channel")
        assert calls == [("interested", 7)]
    finally:
        unregister("a")
        unregister("b")


async def test_repaint_failure_never_blocks_others(tmp_path):
    bot = FakeBot(tmp_path)
    calls = []

    async def broken(bot_, guild_id):
        raise RuntimeError("boom")

    async def healthy(bot_, guild_id):
        calls.append(guild_id)

    register("broken", broken)
    register("healthy", healthy)
    try:
        await repaint_guild(bot, 7)
        assert calls == [7]
    finally:
        unregister("broken")
        unregister("healthy")


def _walk(items):
    for item in items:
        yield item
        for attr in ("children", "items"):
            yield from _walk(getattr(item, attr, None) or [])
