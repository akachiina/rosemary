"""The /themes panel: render, select, import and reset flows.

Fake bot + real catalogs: every button flips store state, snapshots the theme
cache and re-renders: the exact sequence a live click performs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import discord

from rosemary.cogs.themes import ThemesMenuView
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        if variables:
            return f"{key}:{variables}"
        return key


class FakeResponse:
    def __init__(self):
        self._done = False

    def is_done(self):
        return self._done

    async def defer(self, *, ephemeral=False):
        self._done = True

    async def edit(self, **kwargs):
        self.kwargs = kwargs


class FakeInteraction:
    def __init__(self, values=None):
        self.response = FakeResponse()
        self.data = {"values": values or []} if values else {}
        self.guild_id = 1
        self.followup = AsyncMock()

    async def edit(self, **kwargs):
        self.kwargs = kwargs


class FakeBot:
    def __init__(self, tmp_path, store):
        self.storage = GuildStorage(tmp_path)
        self.theme = load_theme()
        self.translator = FakeTranslator()
        self._theme_store = store
        self.guilds = [type("G", (), {"id": 1})()]

    def get_guild(self, guild_id):
        return next((g for g in self.guilds if g.id == guild_id), None)


async def make(tmp_path, yaml_body="name: cute\n"):
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir(exist_ok=True)
    (themes_dir / "cute.yaml").write_text(yaml_body, encoding="utf-8")
    store = ThemeStore(tmp_path, themes_dir=themes_dir)
    bot = FakeBot(tmp_path, store)
    from rosemary.core.themes import preload_themes

    await preload_themes(bot)
    view = ThemesMenuView(bot, 1, owner_id=9)
    await view.prepare()
    await view.prepare()  # build_items needs the timeout refresh from prepare
    return bot, store, view


async def test_panel_renders_items_and_select_options(tmp_path):
    yaml_body = (
        "name: cute\ncolors:\n  brand: '#ff8fc7'\n"
        "cards:\n  about.card:\n    - type: 10\n      content: 'hi'\n"
    )
    _bot, _store, view = await make(tmp_path, yaml_body)
    items = await view.build_items()
    assert items  # container + texts + selects + buttons
    texts = "\n".join(
        item.content
        for item in _walk(items)
        if isinstance(item, discord.ui.TextDisplay)
    )
    assert "themes.title" in texts
    assert "themes.active_none" in texts
    selects = [i for i in _walk(items) if isinstance(i, discord.ui.Select)]
    assert [s.custom_id for s in selects] == [
        "themes_select",
        "themes_export",
        "themes_inspect",
        "themes_preview_card",
        "themes_preview_form",
    ]
    assert any(
        isinstance(b, discord.ui.Button) and b.custom_id == "themes_reset"
        for b in _walk(items)
    )


async def test_select_switches_active_theme_and_snapshots(tmp_path):
    bot, store, view = await make(tmp_path)
    interaction = FakeInteraction(values=["cute"])
    await view._select(interaction)
    assert await store.get_active(1) == "cute"
    assert theme_name(bot, 1) == "cute"  # snapshot refreshed
    assert "themes.selected" in _texts(view)


async def test_reset_clears_active(tmp_path):
    bot, store, view = await make(tmp_path)
    await store.set_active(1, "cute")
    await view._reset(FakeInteraction())
    assert await store.get_active(1) is None
    assert theme_name(bot, 1) is None  # back to built-in


async def test_import_flash_shown_by_command(tmp_path):
    bot, store, view = await make(tmp_path)
    view.flash = "ok-imported"
    view.flash_color = "success"
    items = await view.build_items()
    texts = "\n".join(
        item.content
        for item in _walk(items)
        if isinstance(item, discord.ui.TextDisplay)
    )
    assert "ok-imported" in texts


async def test_every_rendered_component_has_a_callback(tmp_path):
    """Regression: raw Selects without callbacks made clicks die with
    "the application did not respond" (live bug report)."""
    yaml_body = (
        "name: cute\ncolors:\n  brand: '#ff8fc7'\n"
        "cards:\n  about.card:\n    - type: 10\n      content: 'hi'\n"
    )
    _bot, _store, view = await make(tmp_path, yaml_body)
    unbound = [
        comp.custom_id
        for comp in set(_walk(view.children))
        if isinstance(comp, (discord.ui.Select, discord.ui.Button))
        and comp.callback is None
    ]
    assert unbound == []


async def test_remove_only_lists_guild_imports(tmp_path):
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir(exist_ok=True)
    (themes_dir / "cute.yaml").write_text("name: cute\n", encoding="utf-8")
    store = ThemeStore(tmp_path, themes_dir=themes_dir)
    await store.import_theme(1, "local.yaml", b"name: local\n")
    bot = FakeBot(tmp_path, store)
    from rosemary.core.themes import preload_themes

    await preload_themes(bot)
    view = ThemesMenuView(bot, 1, owner_id=9)
    await view.prepare()
    items = await view.build_items()
    selects = [i for i in _walk(items) if isinstance(i, discord.ui.Select)]
    remove = next(s for s in selects if s.custom_id == "themes_remove")
    assert [o.value for o in remove.options] == ["local"]  # global not listed


#: the inspection -------------------------------------------------------------


async def test_validate_says_a_sound_theme_is_sound(tmp_path):
    _bot, _store, view = await make(tmp_path)
    view._inspect = "cute"
    interaction = FakeInteraction()
    await view._validate(interaction)
    assert "themes.validate_ok" in _texts(view)


async def test_validate_lists_the_problems_of_the_theme(tmp_path):
    yaml_body = (
        "name: cute\n"
        "cards:\n"
        "  audit.ban:\n"
        "    title: '{card.audit.ban.title} {not_a_variable}'\n"
    )
    _bot, _store, view = await make(tmp_path, yaml_body)
    view._inspect = "cute"
    interaction = FakeInteraction()
    await view._validate(interaction)
    assert "themes.validate_failed" in _texts(view)
    text = interaction.followup.send.await_args.args[0]
    assert "themes.validate_issue" in text


async def test_diff_says_what_the_theme_changes(tmp_path):
    yaml_body = "name: cute\ncolors:\n  brand: '#ff8fc7'\n"
    _bot, _store, view = await make(tmp_path, yaml_body)
    view._inspect = "cute"
    interaction = FakeInteraction()
    await view._diff(interaction)
    text = interaction.followup.send.await_args.args[0]
    assert "themes.diff_title" in text
    assert "themes.diff_colors" in text


async def test_the_preview_sends_the_card_in_the_chosen_shape(tmp_path):
    _bot, _store, view = await make(tmp_path)
    view._inspect = "cute"
    view._preview_key = "audit.ban"
    view._preview_form = "embed"
    interaction = FakeInteraction()
    await view._preview(interaction)
    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs.get("embed") is not None
    assert kwargs.get("ephemeral") is True


def _texts(view):
    """The rendered text of the current screen (the flash is one-shot)."""
    return "\n".join(
        item.content
        for item in _walk(view.children)
        if isinstance(item, discord.ui.TextDisplay)
    )


def _walk(items):
    """Depth-first walk; py-cord rows expose .items AND .children (same list)
   : visit each child once."""
    for item in items:
        yield item
        if isinstance(item, discord.ui.ActionRow):
            yield from _walk(getattr(item, "children", []) or [])
        else:
            yield from _walk(getattr(item, "items", []) or [])


def theme_name(bot, guild_id):
    from rosemary.core.themes import theme_for

    theme = theme_for(bot, guild_id)
    return getattr(theme, "name", None)
