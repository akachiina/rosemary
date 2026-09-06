"""Tests for ``rosemary.core.i18n`` (Translator) and the theme wiring."""

from __future__ import annotations

from pathlib import Path

import yaml

from rosemary.core.i18n import Translator, _flatten
from rosemary.ui.theme import load_theme

ROOT = Path(__file__).resolve().parents[2]
LANG_DIR = ROOT / "rosemary" / "language"


def _resolver(language: str):
    async def resolve(guild_id):
        return language

    return resolve


def _read_keys(code: str) -> set[str]:
    with (LANG_DIR / f"{code}.yaml").open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return set(_flatten(data))


async def test_catalogs_load_and_key_parity():
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert tr.available_languages == ["en-US", "pt-BR"]
    assert _read_keys("en-US") == _read_keys("pt-BR")
    assert _read_keys("en-US")  # non-empty catalog


def test_settings_descriptions_fit_modal_placeholder():
    """Discord modal placeholders cap at 100 chars, so every settings
    description must stay within the limit (bump.detection_text regression)."""
    for code in ("en-US", "pt-BR"):
        with (LANG_DIR / f"{code}.yaml").open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        for key, spec in (data.get("settings") or {}).items():
            if isinstance(spec, dict) and "description" in spec:
                assert len(spec["description"]) <= 100, (
                    f"{code} settings.{key}.description exceeds 100 chars"
                )


async def test_t_default():
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert await tr.t(1, "about.title") == "About Rosemary"


async def test_t_per_guild():
    tr = Translator(LANG_DIR, resolver=_resolver("pt-BR"))
    assert await tr.t(1, "about.title") == "Sobre a Rosemary"


async def test_t_fallback_raw_key():
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert await tr.t(1, "no.such.key") == "no.such.key"


async def test_t_fallback_language():
    # Unknown catalog code resolves to the default language text, not the raw key.
    tr = Translator(LANG_DIR, resolver=_resolver("xx-XX"))
    assert await tr.t(1, "about.title") == "About Rosemary"


async def test_t_formatting():
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert await tr.t(1, "about.version", version="9.9.9", channel="stable") == (
        "Version: 9.9.9 (stable)"
    )
    # Missing variable: formatting error is swallowed, template returned unchanged.
    assert await tr.t(1, "about.version") == "Version: {version} ({channel})"


async def test_missing_languages_dir_no_crash(tmp_path):
    tr = Translator(tmp_path / "nope", resolver=_resolver("en-US"))
    assert tr.available_languages == []
    assert await tr.t(1, "about.title") == "about.title"


async def test_language_meta_is_not_a_translation_key():
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert await tr.t(1, "language_meta.name") == "language_meta.name"
    assert await tr.t(1, "language_meta") == "language_meta"


async def test_language_display_uses_meta():
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert tr.language_display("en-US") == "🇺🇸 English"
    assert tr.language_display("pt-BR") == "🇧🇷 Português do Brasil"


async def test_command_metadata_is_localized():
    en = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert await en.t(1, "about.command.name") == "about"
    assert await en.t(1, "about.command.description") == "About Rosemary"
    assert await en.t(1, "language.command.name") == "language"
    assert await en.t(1, "language.command.description") == "Set the server language"

    pt = Translator(LANG_DIR, resolver=_resolver("pt-BR"))
    assert await pt.t(1, "about.command.name") == "sobre"
    assert await pt.t(1, "language.command.name") == "idioma"
    assert await pt.t(1, "language.command.description") == "Define o idioma do servidor"


async def test_theme_emojis_are_auto_injected():
    theme = load_theme()
    assert theme.emojis["success"] == "✅"
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"), default_placeholders=theme.emojis)
    assert await tr.t(1, "language.updated", language="en-US") == "✅ Language set to **en-US**."


async def test_leaderboard_week_end_timestamp_renders():
    """The 'week ends in' line renders a relative timestamp in both catalogs."""
    theme = load_theme()
    pt = Translator(LANG_DIR, resolver=_resolver("pt-BR"), default_placeholders=theme.emojis)
    desc = await pt.t(
        1,
        "bump.messages.leaderboard_description",
        start_timestamp=111,
        end_timestamp=222,
        next_reset_timestamp=333,
        winner_mention="<@1>",
        winner_count=1,
        role_name="Bump-MVP",
        leaderboard_list="- x",
    )
    assert "<t:333:R>" in desc
    assert "⏳" in desc
    assert await pt.t(1, "bump.stats.week_end", next_reset_ts=444) == (
        "⏳ A semana termina em: <t:444:R>"
    )

    en = Translator(LANG_DIR, resolver=_resolver("en-US"), default_placeholders=theme.emojis)
    assert await en.t(1, "bump.stats.week_end", next_reset_ts=444) == (
        "⏳ The week ends in: <t:444:R>"
    )


async def test_arbitrary_placeholder_from_theme(tmp_path):
    # A user-added emoji key in theme.yaml is usable in any string without code.
    (tmp_path / "en-US.yaml").write_text(
        "x:\n  msg: \"emoji: {emoji_23}\"\n", encoding="utf-8"
    )
    tr = Translator(
        tmp_path,
        resolver=_resolver("en-US"),
        default_placeholders={"emoji_23": "🔥"},
    )
    assert await tr.t(1, "x.msg") == "emoji: 🔥"


async def test_explicit_variables_override_placeholders(tmp_path):
    (tmp_path / "en-US.yaml").write_text(
        "x:\n  msg: \"emoji: {success}\"\n", encoding="utf-8"
    )
    tr = Translator(
        tmp_path,
        resolver=_resolver("en-US"),
        default_placeholders={"success": "✅"},
    )
    assert await tr.t(1, "x.msg", success="custom") == "emoji: custom"


async def test_unknown_placeholder_does_not_crash(tmp_path):
    (tmp_path / "en-US.yaml").write_text(
        "x:\n  msg: \"emoji: {emoji_99}\"\n", encoding="utf-8"
    )
    tr = Translator(tmp_path, resolver=_resolver("en-US"))
    assert await tr.t(1, "x.msg") == "emoji: {emoji_99}"


async def test_invalid_message_multiline_available():
    theme = load_theme()
    tr = Translator(
        LANG_DIR, resolver=_resolver("pt-BR"), default_placeholders=theme.emojis
    )
    message = await tr.t(
        1,
        "language.invalid",
        language="xx-XX",
        available="- 🇺🇸 English\n- 🇧🇷 Português do Brasil",
    )
    assert "Disponíveis:" in message
    assert "\n- 🇺🇸 English\n- 🇧🇷 Português do Brasil" in message
    assert message.startswith(theme.emojis["error"])
