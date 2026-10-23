"""Moderation cards: the /warnings builder and the DM pipeline.

The builder owns the warnings card (member avatar, state-driven color,
author footer); the DMs flow through the shared boost_dm pipeline so themed
overrides keep their rich layout. The registration test pins the production
import path: bot.py reaches these builders through card_specs' side-effect
imports, nothing else loads them.
"""

from __future__ import annotations

from pathlib import Path

import discord
import pytest
import yaml

import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.ui.theme import load_theme


def _validate_components(d, path, errors):
    """Walk a component dict tree asserting Discord's V2 placement rules."""
    ctype = d.get("type")
    if ctype == 9:  # Section
        for child in d.get("components", []):
            if child.get("type") != 10:
                errors.append(f"{path}: Section item type {child.get('type')} (not 10)")
        accessory = d.get("accessory")
        if accessory is not None and accessory.get("type") not in (11, 2):
            errors.append(f"{path}: Section accessory type {accessory.get('type')}")
    elif ctype == 17:  # Container
        for child in d.get("components", []):
            if child.get("type") not in (1, 9, 10, 12, 13, 14):
                errors.append(f"{path}: Container child type {child.get('type')}")
            _validate_components(child, f"{path}/child", errors)
    elif ctype == 11:  # Thumbnail outside a Section accessory
        errors.append(f"{path}: Thumbnail outside a Section accessory")


def _assert_valid_layout(view, label):
    errors = []
    for item in view.to_components():
        d = item.to_component_dict() if not isinstance(item, dict) else item
        _validate_components(d, label, errors)
    assert errors == [], "\n".join(errors)


def _builder_bot():
    """A bot with the real catalogs (pt-BR) so builder copy resolves for real."""
    from rosemary.core.i18n import Translator

    async def resolver(gid):
        return "pt-BR"

    theme = load_theme()

    class _Bot:
        def __init__(self) -> None:
            self.theme = theme
            self._theme_cache: dict = {}
            self.translator = Translator(
                Path(__file__).resolve().parents[1] / "language",
                resolver=resolver,
                default_placeholders=theme.emojis,
            )

    return _Bot()


_WARNINGS_VARS = {
    "user": "<@456>",
    "user_avatar": "https://example.com/avatar.png",
    "has_warnings": True,
    "title": "Warns de <@456>",
    "body": "**#1:** Flood\n-# por <@1> · 2026-10-22",
    "timestamp": "<t:1:R>",
}

_EMPTY_VARS = {**_WARNINGS_VARS, "has_warnings": False, "body": "✅ Sem warns."}


#: builder + registration -----------------------------------------------------


def test_moderation_builder_registered():
    from rosemary.core.cards import get_default_builder
    from rosemary.core.moderation_cards import default_moderation_warnings_document

    assert (
        get_default_builder("moderation.warnings") is default_moderation_warnings_document
    )


def test_moderation_warnings_has_no_seed():
    """A seed would shadow the builder and strip the avatar (AGENTS.md rule)."""
    from rosemary.core.card_service import SEED_PARTS_BY_KEY

    assert "moderation.warnings" not in SEED_PARTS_BY_KEY


def test_card_specs_registers_core_builders():
    """Production reaches the bump/moderation builders only through the
    card_specs side-effect imports (bot.py imports card_specs). Without them
    the live bot rendered the code fallbacks forever while tests stayed
    green, because the test files imported the builder modules themselves."""
    import sys

    from rosemary.core import cards as cards_module

    for key in ("bump.thank_you", "bump.leaderboard", "moderation.warnings"):
        cards_module._DEFAULT_BUILDERS.pop(key, None)
    for name in (
        "rosemary.core.card_specs",
        "rosemary.core.bump_cards",
        "rosemary.core.moderation_cards",
    ):
        sys.modules.pop(name, None)
    try:
        assert cards_module.get_default_builder("bump.thank_you") is None
        import rosemary.core.card_specs  # noqa: F401  (production import path)

        assert cards_module.get_default_builder("bump.thank_you") is not None
        assert cards_module.get_default_builder("bump.leaderboard") is not None
        assert cards_module.get_default_builder("moderation.warnings") is not None
    finally:
        # Other test modules keep their original bindings; the freshly
        # imported modules stay cached for later imports. Registration is
        # idempotent (dict assignment), so both states are consistent.
        pass


#: builder documents ----------------------------------------------------------


@pytest.mark.parametrize(
    ("variables", "style"),
    [(_WARNINGS_VARS, "moderation_warnings"), (_EMPTY_VARS, "success")],
)
async def test_warnings_builder_layout_and_state_color(variables, style):
    """The card follows the theme styles and produces a valid V2 document."""
    from rosemary.core.card_service import render_document
    from rosemary.core.moderation_cards import default_moderation_warnings_document

    bot = _builder_bot()
    doc = await default_moderation_warnings_document(bot, 1, **variables)
    container = doc["blocks"][0]
    assert container["type"] == "container"
    assert container["color"] == bot.theme.style(style).color

    section = container["children"][0]
    assert section["type"] == "section"
    assert section["accessory"]["url"] == variables["user_avatar"]
    bodies = [c["body"] for c in container["children"] if c["type"] == "text"]
    assert any("-#" in body for body in bodies)  # author footer block

    view = await render_document(
        bot, doc, dict(variables), guild_id=1, card_key="moderation.warnings"
    )
    _assert_valid_layout(view, "moderation_warnings")
    texts = [
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]
    assert any(variables["body"].splitlines()[0] in text for text in texts)
    assert any("-# por <@" in text for text in texts)  # footer with the user


async def test_warnings_builder_without_avatar_omits_accessory():
    """A missing avatar must degrade to plain text, never an empty accessory."""
    from rosemary.core.moderation_cards import default_moderation_warnings_document

    bot = _builder_bot()
    bare = dict(_WARNINGS_VARS)
    bare["user_avatar"] = ""
    doc = await default_moderation_warnings_document(bot, 1, **bare)
    section = doc["blocks"][0]["children"][0]
    assert "accessory" not in section


async def test_warnings_builder_color_follows_theme_style(tmp_path):
    """Restyling ``styles.moderation_warnings`` in a theme file changes the
    card, exactly like the bump builders."""
    from rosemary.core.moderation_cards import default_moderation_warnings_document
    from rosemary.ui.theme import load_theme as _load

    default_yaml = (
        Path(__file__).resolve().parents[1] / "themes" / "default.yaml"
    ).read_text(encoding="utf-8")
    data = yaml.safe_load(default_yaml)
    data["styles"]["moderation_warnings"]["color"] = "danger"
    theme_file = tmp_path / "custom.yaml"
    theme_file.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    bot = _builder_bot()
    bot._theme_cache[1] = _load(theme_file)

    doc = await default_moderation_warnings_document(bot, 1, **_WARNINGS_VARS)
    assert doc["blocks"][0]["color"] == "danger"


#: DM pipeline ----------------------------------------------------------------


class _FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return f"{key}:{variables.get('reason', '')}"

    async def raw(self, guild_id, key):
        return key


class _ThemeBot:
    """A bot with a real ThemeStore so themed overrides resolve for real."""

    def __init__(self, tmp_path, themes_dir) -> None:
        from rosemary.core.themes import ThemeStore

        self.theme = load_theme()
        self.translator = _FakeTranslator()
        self._theme_store = ThemeStore(tmp_path, themes_dir=themes_dir)
        self.guilds = []
        self._theme_cache: dict = {}

    async def activate(self, name: str) -> None:
        from rosemary.core.themes import preload_themes

        await self._theme_store.set_active(1, name)
        self._theme_cache.clear()
        await preload_themes(self)


class _Dest:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, *args, **kwargs):
        self.calls.append({"content": args[0] if args else None, **kwargs})


def _make_themes(tmp_path, cards_yaml: str):
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir(exist_ok=True)
    (themes_dir / "t.yaml").write_text(
        f"name: t\ncards:\n{cards_yaml}", encoding="utf-8"
    )
    (themes_dir / "d.yaml").write_text("name: d\n", encoding="utf-8")
    return themes_dir


async def test_moderation_dm_rich_override_renders_as_view(tmp_path):
    """Themed rich DM overrides keep their layout (the old text_or path
    flattened them); the default copy still goes out as plain text."""
    from rosemary.ui import boost_dm

    themes_dir = _make_themes(
        tmp_path,
        "  warn.dm:\n"
        "    - type: 17\n"
        "      components:\n"
        "        - type: 10\n"
        '          content: "# Card rico"\n',
    )
    bot = _ThemeBot(tmp_path, themes_dir)
    dest = _Dest()

    await bot.activate("t")
    await boost_dm.send(bot, dest, 1, "warn.dm", guild="S", moderator="<@2>", reason="r")
    assert len(dest.calls) == 1
    assert dest.calls[0].get("view") is not None
    assert dest.calls[0].get("content") is None

    # Without an override the translated copy is the plain message.
    await bot.activate("d")
    await boost_dm.send(bot, dest, 1, "warn.dm", guild="S", moderator="<@2>", reason="r")
    assert len(dest.calls) == 2
    assert dest.calls[1] == {"content": "warn.dm:r"}


async def test_trap_dm_resolves_override_by_card_key(tmp_path):
    """The trap DM reads the themed override at the spec key (``trap.dm``),
    not the old ``trap.dm.text``: editor-written overrides now apply."""
    from rosemary.ui import boost_dm

    themes_dir = _make_themes(
        tmp_path,
        "  trap.dm:\n"
        "    - type: 17\n"
        "      components:\n"
        "        - type: 10\n"
        '          content: "# Armadilha customizada"\n',
    )
    bot = _ThemeBot(tmp_path, themes_dir)
    dest = _Dest()

    await bot.activate("t")
    await boost_dm.send(
        bot, dest, 1, "trap.dm", default_key="trap.dm.text", server="S", action="ban"
    )
    assert len(dest.calls) == 1
    # The override rendered as a card; the text default was never needed.
    assert dest.calls[0].get("view") is not None
    assert dest.calls[0].get("content") is None
