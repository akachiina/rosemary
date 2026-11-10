"""Every representative notice renders exactly what the golden fixture froze.

The card and panel fixtures pin the other two shapes. These pin the plain-text
notice: the icon the decorator chooses (the verb of the action or the theme
tone) and the copy the member reads. ``scripts/snapshot_notices.py`` writes
the fixture; when a change is deliberate, re-run it and review the diff::

    ../.venv/bin/python scripts/snapshot_notices.py
"""

from __future__ import annotations

import json

from golden_notices import (
    GOLDEN_PATH,
    GUILD_ID,
    LANGUAGES,
    NOTICE_CASES,
    make_bot,
    render_all,
)

#: The copies the send sites own without a legacy tone token. A new notice
#: key joins this list when its copy is cleaned; the decorator still strips a
#: token from the keys outside it (the theme may keep an old catalog).
CLEAN_COPY_KEYS = (
    "ban.success",
    "kick.success",
    "mute.success",
    "warn.success",
    "unwarn.cleared",
    "unwarn.removed",
    "unwarn.none_for",
    "unwarn.invalid_index",
    "moderation.error_failed",
    "moderation.error_disabled",
    "moderation.error_no_members",
    "moderation.error_self",
    "moderation.error_self_bot",
    "moderation.error_author_permissions",
    "moderation.error_bot_permissions",
    "moderation.error_hierarchy",
    "moderation.error_bot_hierarchy",
    "moderation.auto_ban_failed",
    "mute.invalid_duration",
    "cleaner.error_disabled",
    "cleaner.error_no_mode",
    "cleaner.error_conflict",
    "cleaner.error_bad_regex",
    "cleaner.error_last_range",
    "confirm.failed",
    "general.unknown_error",
    "general.no_permission",
    "general.bot_no_permission",
    "menu.owner_only",
    "menu.expired",
    "language.updated",
    "language.invalid",
)

_TONE_TOKENS = ("{success}", "{error}", "{warning}", "{info}")


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


async def test_no_notice_opens_with_two_icons():
    """The decorator owns the icon: the copy never brings a second mark."""
    bot = make_bot()
    icons = sorted({value for value in bot.theme.emojis.values() if value}, key=len, reverse=True)
    for lines in (await render_all(bot)).values():
        for name, line in lines.items():
            leading = next((icon for icon in icons if line.startswith(icon)), None)
            assert leading, f"{name}: no icon: {line!r}"
            rest = line[len(leading) :].lstrip()
            assert not any(rest.startswith(icon) for icon in icons), (
                f"{name}: two icons: {line!r}"
            )


async def test_a_legacy_copy_keeps_one_tone_icon():
    """A copy that still opens with ``{success}`` loses its own mark."""
    lines = (await render_all(make_bot()))["pt-BR"]
    line = lines["settings_saved_legacy_token"]
    assert line.startswith("✅ ")
    assert line.count("✅") == 1

    error = lines["boost_error_legacy_token"]
    assert error.startswith("❌ ")
    assert error.count("❌") == 1


async def test_the_verb_icon_replaces_the_tone():
    """A muted member reads the mute icon, never a second success check."""
    lines = (await render_all(make_bot()))["pt-BR"]
    assert lines["mute"].startswith("🔇 ")
    assert "✅" not in lines["mute"]
    assert lines["ban"].startswith("🚫 ")
    assert lines["kick"].startswith("👢 ")


async def test_the_clean_copies_carry_no_tone_token():
    """The moderation family speaks in words: the icon is the decorator's."""
    bot = make_bot()
    for language in LANGUAGES:
        for key in CLEAN_COPY_KEYS:
            raw = await bot.translator.raw(GUILD_ID, key)
            assert isinstance(raw, str), (language, key)
            head = raw.lstrip()
            assert not head.startswith(_TONE_TOKENS), (language, key, raw[:40])


async def test_the_verb_icon_keeps_the_themed_decoration():
    """A themed prefix may decorate the line; the verb replaces only the mark."""
    from rosemary.core.feedback import notice_line

    class Style:
        prefix = "⭐ >> "

    class Theme:
        def style(self, name: str) -> Style:
            return Style()

        def icon(self, name: str) -> str:
            return {"success": "✅", "mute": "🔇"}.get(name, "")

    class Bot:
        theme = Theme()

    line = notice_line(Bot(), 1, "✅ copy", style="success", icon="mute")
    assert line == "🔇 >> copy"
