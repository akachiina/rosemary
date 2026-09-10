"""Tests for ``rosemary.ui.theme`` (colors, emojis and markdown templates)."""

from __future__ import annotations

import discord
import pytest
import yaml

from rosemary.ui.theme import Theme, load_theme


def _write_theme(tmp_path, **sections) -> str:
    path = tmp_path / "theme.yaml"
    path.write_text(yaml.safe_dump(sections), encoding="utf-8")
    return str(path)


def test_load_theme_markdown_section(tmp_path):
    path = _write_theme(
        tmp_path,
        colors={"brand": "#57F287"},
        emojis={"success": "✅"},
        markdown={"title": "# {title}", "entry": "**{label}:**\n{value}"},
    )
    theme = load_theme(path)
    assert theme.markdown["title"] == "# {title}"
    assert theme.md("title", title="Configurações") == "# Configurações"
    assert (
        theme.md("entry", label="Moderação ativada", value="Desativado")
        == "**Moderação ativada:**\nDesativado"
    )


def test_load_theme_medals_section(tmp_path):
    path = _write_theme(
        tmp_path,
        medals={1: "🥇", 2: "🥈", 3: "🥉", "default": "📊"},
    )
    theme = load_theme(path)
    assert theme.medals["1"] == "🥇"
    assert theme.medals["default"] == "📊"


def test_md_injects_emojis_as_defaults():
    theme = Theme(
        {},
        {"success": "✅", "gear": "⚙️"},
        {"flash": "{success} {gear} {text}"},
        {},
        {}
    )
    assert theme.md("flash", text="ok") == "✅ ⚙️ ok"


def test_md_explicit_kwargs_override_emojis():
    theme = Theme({"emojis": {"success": "✅"}}, {"success": "✅"}, {"line": "{success}"}, {}, {})
    assert theme.md("line", success="X") == "X"


def test_md_missing_template_raises():
    theme = Theme({}, {}, {}, {}, {})
    with pytest.raises(KeyError):
        theme.md("does_not_exist")


def test_default_theme_defines_expected_templates():
    theme = load_theme()
    for name in ("title", "category", "entry", "edit_title", "section", "footer"):
        assert name in theme.markdown


def test_theme_styles_resolution():
    theme = Theme(
        colors={"info": "#111", "success": "#222"},
        emojis={"success": "✅", "star": "⭐"},
        markdown={"title": "# {title}", "custom": "{star} {body}"},
        styles={
            "base": {"color": "info", "template": "title", "prefix": "A", "suffix": "B"},
            "sub": {
                "extends": "base",
                "color": "success",
                "template": "custom",
                "prefix": "{success} ",
            },
            "cycle": {"extends": "cycle_parent"},
                "cycle_parent": {"extends": "cycle"}
        },
        bump={}
    )

    base = theme.style("base")
    assert base.color == "info"
    assert theme.color(base.color) == discord.Colour(0x111)
    assert base.template == "title"
    assert base.prefix == "A"
    assert base.suffix == "B"

    sub = theme.style("sub")
    assert sub.color == "success"
    assert theme.color(sub.color) == discord.Colour(0x222)
    assert sub.template == "custom"
    assert sub.prefix == "✅ "  # Emoji placeholder resolved automatically
    assert sub.suffix == "B"  # Inherited

    # Fallback to base for unknown styles
    unknown = theme.style("does_not_exist")
    assert unknown.color == "info"
    assert unknown.template == "title"

    # Cycle detection safety
    cycle = theme.style("cycle")
    assert cycle.color == "info"  # Base fallback if cycle detected

    # Rendering test
    rendered = theme.styled("sub", body="hello")
    assert rendered == "✅ ⭐ helloB"


def test_every_theme_emoji_is_discord_valid():
    """Every emoji in ui/theme.yaml must be usable as a component emoji.

    Buttons/selects pass theme glyphs to Discord as ``emoji.name``, and
    Discord rejects anything outside the RGI emoji set with 400 Invalid Form
    Body (``emoji.name: Invalid emoji``) — which made the /personalizar
    editor fail to open when the text-only '✓' was used as a button emoji.
    This sweep keeps every future theme emoji button-safe.
    """
    from discord.utils import UNICODE_EMOJIS

    theme = load_theme()
    assert theme.emojis  # non-empty
    for name, glyph in theme.emojis.items():
        assert glyph in UNICODE_EMOJIS, (
            f"theme emoji {name!r} ({glyph!r}) is not a valid Discord emoji; "
            "components using it would be rejected with 400 50035"
        )
