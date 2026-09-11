"""debug.card_paths: every card send can trace its path + origin to the log.

The trace hook lives in :func:`rosemary.core.card_service.render_card_message`
— the single choke point every rich card send passes through. These tests pin:
off by default, emits ``card.<key>`` + origin (theme name or default) when on,
and delivery is delegated to :func:`core.debug.send_channel_log` (whose Discord
side has its own tests in :mod:`tests.test_debug`).
"""

from __future__ import annotations

import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.core.settings import SETTINGS, SettingCategory, SettingType


class _Translator:
    async def t(self, guild_id, key, **kwargs):
        if kwargs:
            return f"{key}:{kwargs}"
        return key

    async def raw(self, guild_id, key):
        return "Rosemary body text" if key == "about.text" else key


def _make_bot(tmp_path, themes_yaml=None):
    from rosemary.core.storage import GuildStorage
    from rosemary.core.themes import ThemeStore
    from rosemary.ui.theme import load_theme

    class Bot:
        pass

    bot = Bot()
    bot.storage = GuildStorage(tmp_path)
    bot.theme = load_theme()
    bot.translator = _Translator()
    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    setup = None
    if themes_yaml:
        (tmp_path / "themes").mkdir(exist_ok=True)
        (tmp_path / "themes" / "t.yaml").write_text(themes_yaml, encoding="utf-8")
        bot.guilds = [type("G", (), {"id": 1})()]

        async def _setup():
            await bot._theme_store.set_active(1, "t")
            from rosemary.core.themes import preload_themes

            await preload_themes(bot)

        setup = _setup
    return bot, setup


def _capture_logs(monkeypatch):
    """Replace ``core.debug.send_channel_log`` (late-imported by the hook)."""
    import rosemary.core.debug as debug_mod

    calls: list[dict] = []

    async def fake_send(bot, guild_id, title, description, **kwargs):
        calls.append({"title": title, "description": description, **kwargs})
        return True

    monkeypatch.setattr(debug_mod, "send_channel_log", fake_send)
    return calls


async def test_setting_registered_in_logging_category():
    spec = SETTINGS["debug.card_paths"]
    assert spec.category is SettingCategory.LOGGING
    assert spec.value_type is SettingType.BOOLEAN
    assert spec.default is False


async def test_trace_disabled_by_default(tmp_path, monkeypatch):
    bot, _ = _make_bot(tmp_path)
    calls = _capture_logs(monkeypatch)
    from rosemary.core.card_service import render_card_message

    await render_card_message(bot, 1, "about.card", {})
    assert calls == []


async def test_trace_emits_path_with_theme_origin(tmp_path, monkeypatch):
    bot, setup = _make_bot(
        tmp_path,
        "name: t\ncards:\n  about.card:\n    - type: 10\n      content: 'x'\n",
    )
    await setup()
    await bot.storage.set(1, "debug.card_paths", True)
    calls = _capture_logs(monkeypatch)
    from rosemary.core.card_service import render_card_message

    view, _allowed = await render_card_message(bot, 1, "about.card", {})
    assert view is not None  # themed document rendered
    assert len(calls) == 1
    assert "card.about.card" in calls[0]["description"]
    assert "debug.trace.theme" in calls[0]["description"]
    assert "t" in calls[0]["description"]


async def test_trace_emits_default_origin_without_theme(tmp_path, monkeypatch):
    bot, _ = _make_bot(tmp_path)
    await bot.storage.set(1, "debug.card_paths", True)
    calls = _capture_logs(monkeypatch)
    from rosemary.core.card_service import render_card_message

    view, _allowed = await render_card_message(bot, 1, "about.card", {})
    assert view is not None  # catalog seed rendered
    assert len(calls) == 1
    assert "card.about.card" in calls[0]["description"]
    assert "debug.trace.default" in calls[0]["description"]


async def test_trace_skipped_when_card_has_no_document(tmp_path, monkeypatch):
    bot, _ = _make_bot(tmp_path)
    await bot.storage.set(1, "debug.card_paths", True)
    calls = _capture_logs(monkeypatch)
    from rosemary.core.card_service import render_card_message

    view, _allowed = await render_card_message(bot, 1, "no.such.card", {})
    assert view is None
    assert calls == []  # nothing sent, nothing traced
