"""The theme tools: validate, diff and preview (the plan's P3).

The ``/themes`` panel answers three questions about a theme before the guild
picks it: is it sound, what does it change against the built-in one and how
does one of its cards look in each shape. These tests pin the three.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rosemary.core.storage import GuildStorage
from rosemary.core.theme_tools import diff_theme, preview_card, validate_theme
from rosemary.core.themes import ThemeStore, preload_themes
from rosemary.ui.theme import load_theme

#: A theme whose delta names things that do not exist.
BROKEN_CARD = """
audit.ban:
  title: '{card.audit.ban.title} {not_a_variable}'
  description: '{missing.copy}'
"""

#: A theme whose delta names a color nobody defines.
BROKEN_COLOR_CARD = """
audit.ban:
  color: nonexistent
  title: '{card.audit.ban.title}'
"""

#: A theme whose stack of embeds carries a slot of the message.
BROKEN_STACK_CARD = """
audit.ban:
  form: embed
  embeds:
  - title: 'Uma caixa'
    actions:
    - label: 'Abrir'
      url: 'https://example.com'
"""

#: A theme whose delta is sound.
SOUND_CARD = """
audit.ban:
  title: '{card.audit.ban.title}'
  description: '{card.audit.ban.body}'
"""


class FakeTranslator:
    """Resolves the real catalog copy (the keys it does not have stay)."""

    def __init__(self) -> None:
        from catalog import load_catalog

        self.catalog = load_catalog("en-US")

    async def raw(self, guild_id, key):
        node = self.catalog
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                return key
        return node if isinstance(node, str) else key

    async def t(self, guild_id, key, **variables):
        found = await self.raw(guild_id, key)
        for name, value in variables.items():
            found = found.replace(f"{{{name}}}", str(value))
        return found


class FakeBot:
    """The minimum bot the theme tools need."""

    def __init__(self, tmp_path: Path, store: ThemeStore) -> None:
        self.storage = GuildStorage(tmp_path)
        self.theme = load_theme()
        self.translator = FakeTranslator()
        self._theme_store = store
        self._theme_cache: dict[int, object] = {}
        self.guilds = [type("G", (), {"id": 1})()]


def _package(root: Path, name: str, cards: str) -> Path:
    """A theme package on the disk (the manifest, the tokens and a card)."""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "theme.yaml").write_text(
        f"version: 2\nname: {name}\nextends: null\n", encoding="utf-8"
    )
    (package / "cards").mkdir(exist_ok=True)
    (package / "cards" / "audit.yaml").write_text(cards, encoding="utf-8")
    return package


def _store(tmp_path: Path, name: str = "fancy", cards: str = SOUND_CARD) -> ThemeStore:
    themes = tmp_path / "themes"
    themes.mkdir(exist_ok=True)
    _package(themes, name, cards)
    return ThemeStore(tmp_path, themes_dir=themes)


#: the validation -------------------------------------------------------------


async def test_a_sound_theme_has_no_problem(tmp_path):
    store = _store(tmp_path)
    bot = FakeBot(tmp_path, store)
    theme = store.load("fancy")
    assert await validate_theme(bot, 1, theme) == []


async def test_the_validation_reports_the_unknown_things(tmp_path):
    store = _store(tmp_path, cards=BROKEN_CARD)
    bot = FakeBot(tmp_path, store)
    theme = store.load("fancy")
    issues = await validate_theme(bot, 1, theme)
    codes = {issue.code for issue in issues}
    assert "theme_unknown_variable" in codes
    assert "theme_unknown_copy" in codes
    assert all(issue.where == "audit.ban" for issue in issues)


def test_an_unknown_color_token_is_rejected_at_load(tmp_path):
    """The loader refuses the theme: an unknown accent never ships."""
    from rosemary.core.v2_convert import ThemeError

    store = _store(tmp_path, cards=BROKEN_COLOR_CARD)
    with pytest.raises(ThemeError) as excinfo:
        store.load("fancy")
    assert "color_unknown" in str(excinfo.value)


async def test_the_validation_reports_an_unknown_card(tmp_path):
    store = _store(tmp_path, cards="ghost.card:\n  title: 'Ghost'\n")
    bot = FakeBot(tmp_path, store)
    theme = store.load("fancy")
    issues = await validate_theme(bot, 1, theme)
    assert [issue.code for issue in issues] == ["theme_unknown_card"]


async def test_the_validation_reads_the_stack_of_embeds(tmp_path):
    """A box that carries a slot of the message is a broken document."""
    store = _store(tmp_path, cards=BROKEN_STACK_CARD)
    bot = FakeBot(tmp_path, store)
    theme = store.load("fancy")
    issues = await validate_theme(bot, 1, theme)
    assert [issue.code for issue in issues] == ["card_bad_embed"]
    assert issues[0].where == "audit.ban"


#: the diff -------------------------------------------------------------------


def test_the_diff_says_what_the_theme_changes(tmp_path):
    store = _store(tmp_path)
    theme = store.load("fancy")
    theme.colors["brand"] = "#123456"
    diff = diff_theme(theme, load_theme())
    assert diff.deltas == ("audit.ban",)
    assert diff.colors["brand"] == (load_theme().colors["brand"], "#123456")
    assert not diff.empty


def test_the_diff_of_the_built_in_theme_is_empty():
    diff = diff_theme(load_theme(), load_theme())
    assert diff.empty


#: the preview ----------------------------------------------------------------


@pytest.mark.parametrize("form", ["text", "embed", "card"])
async def test_the_preview_renders_the_card_in_each_shape(tmp_path, form):
    store = _store(tmp_path)
    bot = FakeBot(tmp_path, store)
    await preload_themes(bot)
    payload, _allowed = await preview_card(bot, 1, "audit.ban", form)
    assert payload is not None
    if form == "text":
        assert payload.content
        assert payload.embed is None
    elif form == "embed":
        assert payload.embed is not None
    else:
        assert payload.view is not None


async def test_the_preview_renders_the_inspected_theme(tmp_path):
    """The preview shows the theme the admin picked, not the active one."""
    store = _store(tmp_path, cards="audit.ban:\n  title: 'Custom ban'\n")
    bot = FakeBot(tmp_path, store)
    await preload_themes(bot)
    theme = store.load("fancy")
    payload, _allowed = await preview_card(
        bot, 1, "audit.ban", "embed", theme=theme
    )
    assert payload is not None and payload.embed is not None
    assert payload.embed.title == "Custom ban"
    # The snapshot is restored: the guild's active theme did not change.
    from rosemary.core.themes import theme_for

    assert theme_for(bot, 1) is bot.theme
