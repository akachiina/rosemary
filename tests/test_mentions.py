"""Mentions engine: theme-driven pings and spec-declared defaults.

The policy matrix (``none``/``single``/``winner_auto``/``role``/``all``) was
replaced by mentions-as-content: pings are decided by the ``<@id>`` tokens
actually present in the resolved text (see
:mod:`tests.test_mention_placeholders`), gated by a per-card on/off toggle that
now lives in the guild's active theme file (``pings:`` section) instead of
``mentions.json``. What stays here is the spec-declared defaults that back the
toggle when the theme does not override it.
"""

from __future__ import annotations

import rosemary.core.card_specs  # noqa: F401  (populates the card registry)
from rosemary.core.cards import get_card
from rosemary.core.mentions import MODES, pings_default, spec_default, theme_pings


def test_modes_known():
    """The policy vocabulary is unchanged (spec defaults still declare it)."""
    assert set(MODES) == {"none", "single", "winner_auto", "role", "all"}


def test_spec_defaults_back_the_toggle():
    assert spec_default("bump.leaderboard") == "winner_auto"
    assert spec_default("bump.reminder") == "role"
    assert spec_default("bump.thank_you") == "single"
    assert spec_default("birthdays.announce") == "single"
    assert spec_default("events.welcome") == "single"
    assert spec_default("bump.no_bumps") == "none"
    assert spec_default("bump.logs.week_reset.description") == "none"
    assert spec_default("boost.logs.register.description") == "none"
    assert spec_default("no.such.card") == "none"
    assert get_card("bump.leaderboard").mention_default == "winner_auto"


def test_pings_default_derives_from_spec():
    assert pings_default("bump.thank_you") is True
    assert pings_default("bump.leaderboard") is True
    assert pings_default("bump.no_bumps") is False
    assert pings_default("moderation.logs.ban.description") is False
    assert pings_default("no.such.card") is False


class _Bot:
    """Bot fake with no theme cache: theme_pings must fall back to defaults."""

    def __init__(self, tmp_path, themes_dir=None):
        from rosemary.core.storage import GuildStorage
        from rosemary.core.themes import ThemeStore
        from rosemary.ui.theme import load_theme

        self.storage = GuildStorage(tmp_path)
        self.theme = load_theme()
        self._theme_store = ThemeStore(tmp_path, themes_dir=themes_dir)


async def test_theme_pings_falls_back_to_spec_default(tmp_path):
    bot = _Bot(tmp_path)
    assert theme_pings(bot, 1, "bump.thank_you") is True
    assert theme_pings(bot, 1, "bump.no_bumps") is False
    assert theme_pings(bot, 1, "no.such.card") is False


async def test_theme_pings_reads_active_theme_override(tmp_path):
    from rosemary.core.themes import ThemeStore, preload_themes

    themes_dir = tmp_path / "themes"
    themes_dir.mkdir()
    (themes_dir / "quiet.yaml").write_text(
        "name: quiet\npings:\n  bump.thank_you: false\n  bump.no_bumps: true\n",
        encoding="utf-8",
    )
    store = ThemeStore(tmp_path, themes_dir=themes_dir)
    await store.set_active(1, "quiet")

    bot = _Bot(tmp_path, themes_dir=themes_dir)
    bot.guilds = [type("G", (), {"id": 1})()]
    await preload_themes(bot)

    assert theme_pings(bot, 1, "bump.thank_you") is False  # theme says off
    assert theme_pings(bot, 1, "bump.no_bumps") is True  # theme says on
    assert theme_pings(bot, 1, "bump.leaderboard") is True  # not overridden
