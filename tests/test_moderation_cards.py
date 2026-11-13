"""Moderation cards: the /warnings card and the DM pipeline.

The warnings card lives in the default package: the document owns the frame
(the member's avatar rides the title, the body lists the warnings and the
attribution closes it). The *state* chooses the color token (the send site
resolves the theme style, the document picks the token up), so the theme of
the guild keeps owning what the token means. The DMs flow through the shared
boost_dm pipeline so themed overrides keep their rich layout.
"""

from __future__ import annotations

from pathlib import Path

import discord
import pytest
import yaml
from card_forms import (
    payload_accent,
    payload_footer_lines,
    payload_texts,
    payload_thumbnail,
)

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


def _card_bot(tmp_path):
    """A bot with the real catalogs (pt-BR) so the card copy resolves for real."""
    from rosemary.core.i18n import Translator
    from rosemary.core.storage import GuildStorage

    async def resolver(gid):
        return "pt-BR"

    theme = load_theme()

    class _Bot:
        def __init__(self) -> None:
            self.theme = theme
            self._theme_cache: dict = {}
            self.storage = GuildStorage(tmp_path)
            self.translator = Translator(
                Path(__file__).resolve().parents[1] / "language",
                resolver=resolver,
                default_placeholders=theme.emojis,
            )

    return _Bot()


_WARNINGS_VARS = {
    "user": "<@456>",
    "user_name": "Ana",
    "user_avatar": "https://example.com/avatar.png",
    "has_warnings": True,
    "title": "Warns de <@456>",
    "body": "**#1:** Flood\n-# por <@1> · 2026-10-22",
    "timestamp": "<t:1:R>",
}

_EMPTY_VARS = {**_WARNINGS_VARS, "has_warnings": False, "body": "✅ Sem warns."}


#: the card document ---------------------------------------------------------


def test_moderation_warnings_is_a_document():
    """The card lives in the default package: the document is the source of
    truth (no builder, no form builder, no seed)."""
    from rosemary.core.cards import get_card
    from rosemary.core.theme_package import default_package

    assert "moderation.warnings" in default_package().documents
    assert get_card("moderation.warnings") is not None


@pytest.mark.parametrize(
    ("variables", "style"),
    [(_WARNINGS_VARS, "moderation_warnings"), (_EMPTY_VARS, "success")],
)
async def test_warnings_card_layout_and_state_color(variables, style, tmp_path):
    """The card shows the member's avatar, the body and the attribution, and
    its color is the token the state chose."""
    from rosemary.core.card_service import render_card_message

    bot = _card_bot(tmp_path)
    state_color = bot.theme.style(style).color
    payload, _allowed = await render_card_message(
        bot,
        1,
        "moderation.warnings",
        {**variables, "state_color": state_color},
    )
    assert payload is not None
    texts = payload_texts(payload)
    assert payload_accent(payload) == bot.theme.color(state_color).value
    # The title is a bold line (the size of the title of an embed).
    assert any(text == f"**{variables['title']}**" for text in texts), texts
    assert any(variables["body"] in text for text in texts), texts
    # The subject's picture is the thumbnail; the name is not a line.
    assert not any(text == f"**{variables['user_name']}**" for text in texts), texts
    assert payload_thumbnail(payload) == variables["user_avatar"]
    # The date closes the card, readable and free of raw tokens.
    footer = "\n".join(payload_footer_lines(payload))
    assert "<@" not in footer and "<t:" not in footer


async def test_warnings_card_without_avatar_has_no_thumbnail(tmp_path):
    """A missing avatar leaves the card without a picture, never an empty one."""
    from rosemary.core.card_service import render_card_message

    bot = _card_bot(tmp_path)
    bare = {**_WARNINGS_VARS, "user_avatar": ""}
    payload, _allowed = await render_card_message(
        bot, 1, "moderation.warnings", {**bare, "state_color": "warning"}
    )
    assert payload is not None
    texts = payload_texts(payload)
    assert not any(text == f"**{bare['user_name']}**" for text in texts), texts
    assert payload_thumbnail(payload) == ""


async def test_warnings_card_color_follows_theme_style(tmp_path):
    """Restyling ``styles.moderation_warnings`` in a theme file changes the
    card: the send site resolves the style to a token and the document uses
    it."""
    from rosemary.core.card_service import render_card_message
    from rosemary.core.themes import theme_for
    from rosemary.ui.theme import load_theme as _load

    default_yaml = (
        Path(__file__).resolve().parents[1] / "themes" / "default" / "tokens.yaml"
    ).read_text(encoding="utf-8")
    data = yaml.safe_load(default_yaml)
    data["styles"]["moderation_warnings"]["color"] = "danger"
    theme_file = tmp_path / "custom.yaml"
    theme_file.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    bot = _card_bot(tmp_path)
    bot._theme_cache[1] = _load(theme_file)

    state_color = theme_for(bot, 1).style("moderation_warnings").color
    payload, _allowed = await render_card_message(
        bot, 1, "moderation.warnings", {**_WARNINGS_VARS, "state_color": state_color}
    )
    assert payload is not None
    assert payload_accent(payload) == theme_for(bot, 1).color("danger").value


async def test_warnings_card_v2_form_is_valid(tmp_path):
    """The classic form is the default, but the card still has a valid
    Components V2 layout (the repaint path)."""
    from rosemary.core.card_service import render_card_message

    bot = _card_bot(tmp_path)
    payload, _allowed = await render_card_message(
        bot,
        1,
        "moderation.warnings",
        {**_WARNINGS_VARS, "state_color": "warning"},
        form="card",
    )
    assert payload is not None and payload.view is not None
    _assert_valid_layout(payload.view, "moderation.warnings")
    texts = [
        item.content
        for item in payload.view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]
    assert any("Flood" in text for text in texts)


#: DM pipeline ----------------------------------------------------------------


#: The plain-text DM notices: the card key and the variables it renders with.
DM_NOTICES = {
    "ban.dm": {"guild": "Serv", "moderator": "<@2>", "reason": "spam"},
    "kick.dm": {"guild": "Serv", "moderator": "<@2>", "reason": "spam"},
    "mute.dm": {
        "guild": "Serv",
        "moderator": "<@2>",
        "reason": "spam",
        "duration": "1h",
    },
    "warn.dm": {"guild": "Serv", "moderator": "<@2>", "reason": "spam"},
    "trap.dm": {"server": "Serv", "action": "A mensagem foi apagada."},
    "confessions.dm.banned": {"reason": "spam"},
}


@pytest.mark.parametrize("key", sorted(DM_NOTICES))
async def test_the_dm_notices_keep_the_plain_text_grammar(key, tmp_path):
    """A notice that says more than one thing opens with a bold title (the
    icon outside the bold) and lists its fields as bullets."""
    from rosemary.core.card_service import render_card_message

    bot = _card_bot(tmp_path)
    payload, _allowed = await render_card_message(bot, 1, key, DM_NOTICES[key])
    assert payload is not None
    assert payload.embed is None, key
    content = payload.content
    lines = content.splitlines()
    # The title: the icon opens the line and stays outside the bold.
    assert not lines[0].startswith("**"), content
    assert "**" in lines[0], content
    # The fields: one item per line, as bullets.
    fields = [line for line in lines[1:] if line.startswith("• ")]
    assert fields, content
    for field in fields:
        assert field.startswith("• **"), field
        assert ":** " in field, field
    # No field disguised as a blockquote.
    assert not any(line.startswith("> ") for line in lines), content


@pytest.mark.parametrize("key", sorted(DM_NOTICES))
async def test_the_dm_notices_carry_their_own_data(key, tmp_path):
    """The notice says what happened: the values the send site hands it
    reach the member."""
    from rosemary.core.card_service import render_card_message

    bot = _card_bot(tmp_path)
    payload, _allowed = await render_card_message(bot, 1, key, DM_NOTICES[key])
    content = payload.content
    for name, value in DM_NOTICES[key].items():
        if name == "action":
            assert value in content, content
            continue
        assert str(value) in content, f"{key}: {name} missing from {content!r}"


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
    assert dest.calls[1]["content"] == "warn.dm:r"
    # A DM never pings: the copy ships silent.
    assert dest.calls[1]["allowed_mentions"].none()


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
