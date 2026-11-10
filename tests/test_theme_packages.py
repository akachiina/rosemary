"""Theme packages: the manifest, the chain, the deltas and the import.

These tests lock the promises of the package format (the plan's P3):

* a theme is a package (a folder with ``theme.yaml``) or the old single
  file;
* a theme may **extend** another one: the chain merges from the root to the
  child and a cycle is rejected with a clear error;
* a **delta** (a semantic document) customizes a card without rewriting it:
  the fields it does not declare are inherited from the packaged document;
* the old format still works: a raw Components V2 card replaces the whole
  card, exactly as before.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from rosemary.core.card_service import render_card_message
from rosemary.core.cards import CardIssue
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import (
    RosemaryTheme,
    ThemeStore,
    card_delta,
    load_theme_file,
    theme_for,
)
from rosemary.core.v2_convert import ThemeError
from rosemary.ui.theme import load_theme

#: the package manifest of a theme
MANIFEST = "version: 2\nname: {name}\nextends: {extends}\n"


def _package(root: Path, name: str, *, extends: str | None = None, **files: str) -> Path:
    """A theme package on the disk (the manifest plus its files)."""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "theme.yaml").write_text(
        MANIFEST.format(name=name, extends=extends or "null"), encoding="utf-8"
    )
    for filename, content in files.items():
        path = package / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return package


def _zip(package: Path) -> bytes:
    """The ZIP of a package folder."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.rglob("*")):
            if path.is_dir():
                continue
            archive.write(path, path.relative_to(package).as_posix())
    return buffer.getvalue()


class ThemeBot:
    """The minimum bot the theme pipeline needs."""

    def __init__(self, tmp_path: Path, store: ThemeStore) -> None:
        self.storage = GuildStorage(tmp_path)
        self.theme = load_theme()
        self.translator = type("T", (), {})()
        self._theme_store = store
        self._theme_cache: dict[int, object] = {}
        self.guilds: list[object] = []


#: the package ---------------------------------------------------------------


def test_the_store_lists_the_packages_and_the_files(tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "cute.yaml").write_text("name: cute\n", encoding="utf-8")
    _package(themes, "fancy")
    store = ThemeStore(tmp_path, themes_dir=themes)
    assert store.list_global() == ["cute", "fancy"]


def test_a_package_carries_its_tokens_and_its_documents(tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    _package(
        themes,
        "fancy",
        **{
            "tokens.yaml": 'colors:\n  brand: "#ff00ff"\n',
            "cards/audit.yaml": "audit.ban:\n  title: 'Custom ban'\n",
        },
    )
    store = ThemeStore(tmp_path, themes_dir=themes)
    theme = store.load("fancy")
    assert isinstance(theme, RosemaryTheme)
    assert theme.colors["brand"] == "#ff00ff"
    # The tokens the package does not define keep the built-in value.
    assert theme.colors["danger"] == load_theme().colors["danger"]
    # The document is a delta (the fields it does not declare are inherited).
    assert theme.deltas["audit.ban"] == {"title": "Custom ban"}


def test_a_package_with_a_broken_document_is_rejected(tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    _package(themes, "fancy", **{"cards/audit.yaml": "audit.ban:\n  nope: 1\n"})
    store = ThemeStore(tmp_path, themes_dir=themes)
    with pytest.raises(ThemeError) as excinfo:
        store.load("fancy")
    assert "theme_card_invalid" in str(excinfo.value)


#: the chain -----------------------------------------------------------------


def test_a_theme_extends_another_one(tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    _package(
        themes,
        "base",
        **{
            "tokens.yaml": 'colors:\n  brand: "#111111"\n  info: "#222222"\n',
            "cards/audit.yaml": "audit.ban:\n  title: 'Base ban'\n  color: info\n",
        },
    )
    _package(
        themes,
        "child",
        extends="base",
        **{
            "tokens.yaml": 'colors:\n  brand: "#333333"\n',
            "cards/audit.yaml": "audit.ban:\n  title: 'Child ban'\n",
        },
    )
    store = ThemeStore(tmp_path, themes_dir=themes)
    theme = store.load("child")
    # The child's tokens win, the parent's fill the gaps.
    assert theme.colors["brand"] == "#333333"
    assert theme.colors["info"] == "#222222"
    # The card document merges: the child overrides the title, the parent
    # keeps the color.
    assert theme.deltas["audit.ban"] == {"title": "Child ban", "color": "info"}
    assert theme.extends == "base"


def test_an_extends_cycle_is_rejected(tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    _package(themes, "a", extends="b")
    _package(themes, "b", extends="a")
    store = ThemeStore(tmp_path, themes_dir=themes)
    with pytest.raises(ThemeError) as excinfo:
        store.load("a")
    assert "theme_extends_cycle" in str(excinfo.value)


def test_an_extends_chain_that_is_too_deep_is_rejected(tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    _package(themes, "t0", extends="t1")
    _package(themes, "t1", extends="t2")
    _package(themes, "t2", extends="t3")
    _package(themes, "t3", extends="t4")
    _package(themes, "t4", extends="t5")
    _package(themes, "t5", extends="t6")
    _package(themes, "t6", extends="t7")
    _package(themes, "t7", extends="t8")
    _package(themes, "t8", extends="t9")
    store = ThemeStore(tmp_path, themes_dir=themes)
    with pytest.raises(ThemeError) as excinfo:
        store.load("t0")
    assert "theme_extends_depth" in str(excinfo.value)


def test_a_missing_parent_is_rejected(tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    _package(themes, "child", extends="ghost")
    store = ThemeStore(tmp_path, themes_dir=themes)
    with pytest.raises(ThemeError) as excinfo:
        store.load("child")
    assert "theme_not_found" in str(excinfo.value)


#: the deltas ----------------------------------------------------------------


async def test_a_delta_customizes_the_card_without_rewriting_it(tmp_path):
    """The delta changes the title; the frame (the color, the footer) stays."""
    themes = tmp_path / "themes"
    themes.mkdir()
    _package(
        themes,
        "fancy",
        **{"cards/audit.yaml": "audit.ban:\n  title: 'Custom ban'\n"},
    )
    store = ThemeStore(tmp_path, themes_dir=themes)
    bot = ThemeBot(tmp_path, store)
    bot.guilds = [type("G", (), {"id": 1})()]
    await store.set_active(1, "fancy")
    from rosemary.core.themes import preload_themes

    await preload_themes(bot)
    delta = await card_delta(bot, 1, "audit.ban")
    assert delta == {"title": "Custom ban"}

    payload, _allowed = await render_card_message(
        bot,
        1,
        "audit.ban",
        {"member": "<@7>", "reason": "spam", "moderator": "<@8>"},
    )
    assert payload is not None and payload.embed is not None
    assert payload.embed.title == "Custom ban"
    # The frame stays the packaged one: the accent is the built-in danger.
    assert payload.embed.colour.value == theme_for(bot, 1).color("danger").value


async def test_a_delta_may_choose_the_shape(tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    _package(
        themes,
        "fancy",
        **{"cards/audit.yaml": "audit.ban:\n  form: card\n  title: 'Custom ban'\n"},
    )
    store = ThemeStore(tmp_path, themes_dir=themes)
    bot = ThemeBot(tmp_path, store)
    bot.guilds = [type("G", (), {"id": 1})()]
    await store.set_active(1, "fancy")
    from rosemary.core.themes import preload_themes

    await preload_themes(bot)
    assert await card_delta(bot, 1, "audit.ban") == {"form": "card", "title": "Custom ban"}
    payload, _allowed = await render_card_message(
        bot,
        1,
        "audit.ban",
        {"member": "<@7>", "reason": "spam", "moderator": "<@8>"},
    )
    assert payload is not None and payload.view is not None
    assert payload.embed is None
    texts = [
        item.content
        for item in payload.view.walk_children()
        if hasattr(item, "content")
    ]
    assert any("Custom ban" in text for text in texts), texts


async def test_the_old_format_still_replaces_the_whole_card(tmp_path):
    """A raw Components V2 card keeps replacing the card, as before."""
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "old.yaml").write_text(
        "name: old\n"
        "cards:\n"
        "  audit.ban:\n"
        "    - type: 17\n"
        '      accent_color: "#ff00ff"\n'
        "      components:\n"
        "        - type: 10\n"
        '          content: "Raw {member}"\n',
        encoding="utf-8",
    )
    store = ThemeStore(tmp_path, themes_dir=themes)
    bot = ThemeBot(tmp_path, store)
    bot.guilds = [type("G", (), {"id": 1})()]
    await store.set_active(1, "old")
    from rosemary.core.themes import preload_themes

    await preload_themes(bot)
    assert await card_delta(bot, 1, "audit.ban") is None
    payload, _allowed = await render_card_message(
        bot,
        1,
        "audit.ban",
        {"member": "<@7>", "reason": "spam", "moderator": "<@8>"},
    )
    assert payload is not None and payload.view is not None
    texts = [
        item.content
        for item in payload.view.walk_children()
        if hasattr(item, "content")
    ]
    assert any("Raw <@7>" in text for text in texts), texts


async def test_a_themed_text_only_card_keeps_the_card_surface(tmp_path):
    """A themed document made only of text carries no layout: the card keeps
    its own surface (the embed)."""
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "old.yaml").write_text(
        "name: old\n"
        "cards:\n"
        "  audit.ban:\n"
        "    - type: 10\n"
        '      content: "Raw {member}"\n',
        encoding="utf-8",
    )
    store = ThemeStore(tmp_path, themes_dir=themes)
    bot = ThemeBot(tmp_path, store)
    bot.guilds = [type("G", (), {"id": 1})()]
    await store.set_active(1, "old")
    from rosemary.core.themes import preload_themes

    await preload_themes(bot)
    payload, _allowed = await render_card_message(
        bot,
        1,
        "audit.ban",
        {"member": "<@7>", "reason": "spam", "moderator": "<@8>"},
    )
    assert payload is not None and payload.embed is not None
    assert payload.embed.description == "Raw <@7>"


#: the import / export -------------------------------------------------------


async def test_a_package_is_imported_from_a_zip(tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    package = _package(
        themes,
        "fancy",
        **{
            "tokens.yaml": 'colors:\n  brand: "#ff00ff"\n',
            "cards/audit.yaml": "audit.ban:\n  title: 'Custom ban'\n",
        },
    )
    store = ThemeStore(tmp_path, themes_dir=themes)
    name = await store.import_theme(1, "fancy.zip", _zip(package))
    assert name == "fancy"
    # The package landed in the guild's own folder.
    assert (tmp_path / "1" / "themes" / "fancy" / "theme.yaml").is_file()
    theme = store.load("fancy", guild_id=1)
    assert theme.colors["brand"] == "#ff00ff"


async def test_a_broken_zip_is_rejected(tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    store = ThemeStore(tmp_path, themes_dir=themes)
    with pytest.raises(ThemeError) as excinfo:
        await store.import_theme(1, "broken.zip", b"not a zip")
    assert "theme_zip_invalid" in str(excinfo.value)


async def test_a_package_is_exported_as_a_zip(tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    _package(themes, "fancy", **{"tokens.yaml": 'colors:\n  brand: "#ff00ff"\n'})
    store = ThemeStore(tmp_path, themes_dir=themes)
    payload = store.export_theme(1, "fancy")
    assert payload is not None
    filename, data = payload
    assert filename == "fancy.zip"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert "theme.yaml" in archive.namelist()
        assert "tokens.yaml" in archive.namelist()


def test_the_issue_codes_are_stable():
    """The codes the ``/themes`` panel translates."""
    assert CardIssue("theme_extends_cycle").code == "theme_extends_cycle"


def test_a_theme_file_still_loads_standalone(tmp_path):
    """The old API (one file, no chain) keeps working."""
    path = tmp_path / "cute.yaml"
    path.write_text(
        'name: cute\ncolors:\n  brand: "#ff8fc7"\n', encoding="utf-8"
    )
    theme = load_theme_file(path)
    assert theme.name == "cute"
    assert theme.colors["brand"] == "#ff8fc7"
