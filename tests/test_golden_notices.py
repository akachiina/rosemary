"""Every representative notice renders exactly what the golden fixture froze.

The card and panel fixtures pin the other two shapes. These pin the plain-text
notice: the copy the member reads, with the icon the copy itself carries (a
theme emoji token). The code never decides the icon: the catalog chooses the
token and the theme gives it the art. ``scripts/snapshot_notices.py`` writes
the fixture; when a change is deliberate, re-run it and review the diff::

    ../.venv/bin/python scripts/snapshot_notices.py
"""

from __future__ import annotations

import json
import re

from golden_notices import (
    GOLDEN_PATH,
    GUILD_ID,
    LANGUAGES,
    NOTICE_CASES,
    make_bot,
    render_all,
    render_raw,
)

#: The icon of a notice: one theme emoji token at the head of the copy.
_TOKEN = re.compile(r"^\{([A-Za-z0-9_]+)\}\s")


def test_every_case_has_a_fixture():
    fixture = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    names = {case["name"] for case in NOTICE_CASES}
    for language in LANGUAGES:
        assert set(fixture[language]) == names, language


async def test_notices_match_the_golden():
    actual = await render_all(make_bot())
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert actual == expected, (
        "the rendered notices changed (re-run scripts/snapshot_notices.py if "
        "the change is deliberate)"
    )


async def test_every_copy_carries_its_icon_token():
    """The catalog owns the icon: the copy opens with a theme emoji token."""
    bot = make_bot()
    tokens = set(bot.theme.emojis)
    for language, copies in (await render_raw(bot)).items():
        for name, copy in copies.items():
            match = _TOKEN.match(copy)
            assert match is not None, f"{language}/{name}: no icon token: {copy!r}"
            assert match.group(1) in tokens, (
                f"{language}/{name}: unknown icon token "
                f"{{{match.group(1)}}}: {copy!r}"
            )


async def test_every_notice_opens_with_one_icon():
    """The member reads exactly one icon: the one the copy carries."""
    bot = make_bot()
    icons = sorted({value for value in bot.theme.emojis.values() if value}, key=len, reverse=True)
    for language, lines in (await render_all(bot)).items():
        for name, line in lines.items():
            leading = next((icon for icon in icons if line.startswith(icon)), None)
            assert leading, f"{language}/{name}: no icon: {line!r}"
            rest = line[len(leading) :].lstrip()
            assert not any(rest.startswith(icon) for icon in icons), (
                f"{language}/{name}: two icons: {line!r}"
            )


async def test_the_icon_comes_from_the_theme():
    """The catalog names the token, the theme gives it the art."""
    bot = make_bot()
    lines = (await render_all(bot))["pt-BR"]
    assert lines["ban"].startswith(bot.theme.emojis["ban"])
    assert lines["kick"].startswith(bot.theme.emojis["kick"])
    assert lines["mute"].startswith(bot.theme.emojis["mute"])
    assert lines["warn"].startswith(bot.theme.emojis["warning"])
    assert lines["unwarn_cleared"].startswith(bot.theme.emojis["minus"])


async def test_a_missing_token_ships_the_copy_verbatim():
    """The notice layer adds nothing: the copy is the answer."""
    bot = make_bot()
    lines = (await render_all(bot))["pt-BR"]
    assert lines["moderation_disabled"].startswith("❌ ")
    assert lines["moderation_disabled"].count("❌") == 1
    assert "🔒" not in lines["moderation_disabled"]


async def test_the_theme_gives_the_token_its_art():
    """A guild theme may change the art: the token resolves to the glyph."""
    from golden_notices import LANG_DIR

    from rosemary.core.i18n import Translator

    theme = make_bot().theme
    art = {**theme.emojis, "ban": "🚷"}
    translator = Translator(
        LANG_DIR,
        resolver=lambda guild_id: "pt-BR",
        default_placeholders=art,
    )
    text = await translator.t(GUILD_ID, "ban.success", member="@Salada")
    assert text.startswith("🚷 ")
    assert theme.emojis["ban"] == "🚫"
