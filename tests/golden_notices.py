"""Golden snapshots of the one-line notices: the icon and the copy.

The card fixtures freeze the card pipeline and the panel fixtures freeze the
interactive screens. These pin the third shape: the plain-text notice. The
copy is the catalog's, verbatim: it opens with the icon of the action (a
theme emoji token), so the catalog chooses the icon and the theme gives it
the art. ``scripts/snapshot_notices.py`` writes the fixture under
``tests/golden/notices.json`` and ``tests/test_golden_notices.py`` compares
the current render against it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rosemary.core.i18n import Translator
from rosemary.ui.theme import load_theme

#: Where the fixture lives.
GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "notices.json"
#: The guild the snapshots render for.
GUILD_ID = 1
#: The languages the fixture captures (the bot's two catalogs).
LANGUAGES = ("pt-BR", "en-US")
#: The catalog directory (relative to the repo root, never to the CWD).
LANG_DIR = Path(__file__).resolve().parents[1] / "language"

#: The representative notices. The copy carries its own icon (a theme emoji
#: token), so a case names the catalog key and the variables, nothing else:
#: the code does not decide the icon, the catalog does.
NOTICE_CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "ban",
        "key": "ban.success",
        "variables": {"member": "@Salada"},
    },
    {
        "name": "kick",
        "key": "kick.success",
        "variables": {"member": "@Salada"},
    },
    {
        "name": "mute",
        "key": "mute.success",
        "variables": {"member": "@Salada", "duration": "1h"},
    },
    {
        "name": "warn",
        "key": "warn.success",
        "variables": {"member": "@Salada", "count": 1, "limit": 3},
    },
    {
        "name": "unwarn_cleared",
        "key": "unwarn.cleared",
        "variables": {"member": "@Salada", "count": 2},
    },
    {
        "name": "unwarn_removed",
        "key": "unwarn.removed",
        "variables": {"member": "@Salada", "index": 2},
    },
    {
        "name": "unwarn_none",
        "key": "unwarn.none_for",
        "variables": {"member": "@Salada"},
    },
    {
        "name": "moderation_disabled",
        "key": "moderation.error_disabled",
        "variables": {},
    },
    {
        "name": "moderation_no_members",
        "key": "moderation.error_no_members",
        "variables": {},
    },
    {
        "name": "invalid_duration",
        "key": "mute.invalid_duration",
        "variables": {},
    },
    {
        "name": "invalid_index",
        "key": "unwarn.invalid_index",
        "variables": {},
    },
    {
        "name": "cleaner_conflict",
        "key": "cleaner.error_conflict",
        "variables": {},
    },
    {
        "name": "confirm_failed",
        "key": "confirm.failed",
        "variables": {},
    },
    {
        "name": "no_permission",
        "key": "general.no_permission",
        "variables": {},
    },
    {
        "name": "bot_no_permission",
        "key": "general.bot_no_permission",
        "variables": {},
    },
    {
        "name": "unknown_error",
        "key": "general.unknown_error",
        "variables": {},
    },
    {
        "name": "menu_expired",
        "key": "menu.expired",
        "variables": {},
    },
    {
        "name": "language_updated",
        "key": "language.updated",
        "variables": {"language": "pt-BR"},
    },
    {
        "name": "language_invalid",
        "key": "language.invalid",
        "variables": {"language": "xx-XX", "available": "pt-BR, en-US"},
    },
    {
        "name": "settings_saved",
        "key": "settings.saved",
        "variables": {"setting": "Idioma"},
    },
    {
        "name": "themes_selected",
        "key": "themes.selected",
        "variables": {"theme": "neon"},
    },
    {
        "name": "themes_removed",
        "key": "themes.removed",
        "variables": {"theme": "neon"},
    },
    {
        "name": "themes_not_found",
        "key": "themes.not_found",
        "variables": {},
    },
    {
        "name": "boost_role_registered",
        "key": "boost.success.role_registered",
        "variables": {"role": "@Boost"},
    },
    {
        "name": "boost_member_removed",
        "key": "boost.success.member_removed",
        "variables": {"member": "@Salada"},
    },
    {
        "name": "boost_error",
        "key": "boost.errors.role_already_registered",
        "variables": {},
    },
)


class NoticeBot:
    """The minimum bot the notice layer needs: the real theme and catalogs."""

    def __init__(self) -> None:
        self.theme = load_theme()
        self.translator = Translator(
            LANG_DIR,
            resolver=lambda guild_id: LANGUAGES[0],
            default_placeholders=self.theme.emojis,
        )


def make_bot() -> NoticeBot:
    """A bot with the built-in theme and the real catalogs."""
    return NoticeBot()


def _translator(bot: NoticeBot, language: str) -> Translator:
    return Translator(
        LANG_DIR,
        resolver=lambda guild_id, code=language: code,
        default_placeholders=bot.theme.emojis,
    )


async def render_all(bot: NoticeBot | None = None) -> dict[str, dict[str, str]]:
    """Render every representative notice in both catalogs."""
    bot = bot or make_bot()
    snapshots: dict[str, dict[str, str]] = {}
    for language in LANGUAGES:
        bot.translator = _translator(bot, language)
        lines: dict[str, str] = {}
        for case in NOTICE_CASES:
            text = await bot.translator.t(
                GUILD_ID, case["key"], **case.get("variables", {})
            )
            lines[case["name"]] = text
        snapshots[language] = lines
    return snapshots


async def render_raw(bot: NoticeBot | None = None) -> dict[str, dict[str, str]]:
    """The raw templates of every representative notice (the tokens intact)."""
    bot = bot or make_bot()
    snapshots: dict[str, dict[str, str]] = {}
    for language in LANGUAGES:
        bot.translator = _translator(bot, language)
        copies: dict[str, str] = {}
        for case in NOTICE_CASES:
            raw = await bot.translator.raw(GUILD_ID, case["key"])
            copies[case["name"]] = raw if isinstance(raw, str) else str(raw)
        snapshots[language] = copies
    return snapshots
