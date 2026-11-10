"""Golden snapshots of the one-line notices: the icon and the copy.

The card fixtures freeze the card pipeline and the panel fixtures freeze the
interactive screens. These pin the third shape: the plain-text notice, with
the icon the decorator chooses (the verb of the action or the theme tone) and
the copy the member reads. ``scripts/snapshot_notices.py`` writes the fixture
under ``tests/golden/notices.json`` and ``tests/test_golden_notices.py``
compares the current render against it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rosemary.core.feedback import notice_line
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

#: The representative notices. ``icon`` is what the send site names (the verb
#: of the action); ``None`` means the copy still carries a legacy tone token
#: and the decorator must strip it and keep the tone icon.
NOTICE_CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "ban",
        "key": "ban.success",
        "style": "success",
        "icon": "ban",
        "variables": {"member": "@Salada"},
    },
    {
        "name": "kick",
        "key": "kick.success",
        "style": "success",
        "icon": "kick",
        "variables": {"member": "@Salada"},
    },
    {
        "name": "mute",
        "key": "mute.success",
        "style": "success",
        "icon": "mute",
        "variables": {"member": "@Salada", "duration": "1h"},
    },
    {
        "name": "warn",
        "key": "warn.success",
        "style": "success",
        "icon": "warning",
        "variables": {"member": "@Salada", "count": 1, "limit": 3},
    },
    {
        "name": "unwarn_cleared",
        "key": "unwarn.cleared",
        "style": "success",
        "icon": "minus",
        "variables": {"member": "@Salada", "count": 2},
    },
    {
        "name": "unwarn_removed",
        "key": "unwarn.removed",
        "style": "success",
        "icon": "minus",
        "variables": {"member": "@Salada", "index": 2},
    },
    {
        "name": "unwarn_none",
        "key": "unwarn.none_for",
        "style": "success",
        "icon": "info",
        "variables": {"member": "@Salada"},
    },
    {
        "name": "moderation_disabled",
        "key": "moderation.error_disabled",
        "style": "danger",
        "icon": "error",
        "variables": {},
    },
    {
        "name": "moderation_no_members",
        "key": "moderation.error_no_members",
        "style": "danger",
        "icon": "error",
        "variables": {},
    },
    {
        "name": "invalid_duration",
        "key": "mute.invalid_duration",
        "style": "danger",
        "icon": "clock",
        "variables": {},
    },
    {
        "name": "invalid_index",
        "key": "unwarn.invalid_index",
        "style": "danger",
        "icon": "minus",
        "variables": {},
    },
    {
        "name": "cleaner_conflict",
        "key": "cleaner.error_conflict",
        "style": "danger",
        "icon": "error",
        "variables": {},
    },
    {
        "name": "confirm_failed",
        "key": "confirm.failed",
        "style": "danger",
        "icon": "error",
        "variables": {},
    },
    {
        "name": "no_permission",
        "key": "general.no_permission",
        "style": "warning",
        "icon": "lock",
        "variables": {},
    },
    {
        "name": "bot_no_permission",
        "key": "general.bot_no_permission",
        "style": "danger",
        "icon": "lock",
        "variables": {},
    },
    {
        "name": "unknown_error",
        "key": "general.unknown_error",
        "style": "danger",
        "icon": "error",
        "variables": {},
    },
    {
        "name": "menu_expired",
        "key": "menu.expired",
        "style": "warning",
        "icon": None,
        "variables": {},
    },
    {
        "name": "language_updated",
        "key": "language.updated",
        "style": "success",
        "icon": "speech",
        "variables": {"language": "pt-BR"},
    },
    {
        "name": "language_invalid",
        "key": "language.invalid",
        "style": "danger",
        "icon": "error",
        "variables": {"language": "xx-XX", "available": "pt-BR, en-US"},
    },
    {
        "name": "settings_saved_legacy_token",
        "key": "settings.saved",
        "style": "success",
        "icon": None,
        "variables": {"setting": "Idioma"},
    },
    {
        "name": "themes_selected",
        "key": "themes.selected",
        "style": "success",
        "icon": "palette",
        "variables": {"theme": "neon"},
    },
    {
        "name": "themes_removed",
        "key": "themes.removed",
        "style": "success",
        "icon": "trash",
        "variables": {"theme": "neon"},
    },
    {
        "name": "themes_not_found",
        "key": "themes.not_found",
        "style": "danger",
        "icon": None,
        "variables": {},
    },
    {
        "name": "boost_role_registered",
        "key": "boost.success.role_registered",
        "style": "success",
        "icon": "register",
        "variables": {"role": "@Boost"},
    },
    {
        "name": "boost_member_removed",
        "key": "boost.success.member_removed",
        "style": "success",
        "icon": "minus",
        "variables": {"member": "@Salada"},
    },
    {
        "name": "boost_error_legacy_token",
        "key": "boost.errors.role_already_registered",
        "style": "danger",
        "icon": None,
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


async def render_all(bot: NoticeBot | None = None) -> dict[str, dict[str, str]]:
    """Render every representative notice in both catalogs."""
    bot = bot or make_bot()
    snapshots: dict[str, dict[str, str]] = {}
    for language in LANGUAGES:
        bot.translator = Translator(
            LANG_DIR,
            resolver=lambda guild_id, code=language: code,
            default_placeholders=bot.theme.emojis,
        )
        lines: dict[str, str] = {}
        for case in NOTICE_CASES:
            text = await bot.translator.t(
                GUILD_ID, case["key"], **case.get("variables", {})
            )
            lines[case["name"]] = notice_line(
                bot,
                GUILD_ID,
                text,
                style=case.get("style", "info"),
                icon=case.get("icon"),
            )
        snapshots[language] = lines
    return snapshots
