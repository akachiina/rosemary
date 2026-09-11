"""Override-vs-default resolution at the swept send sites.

Overrides now come from the guild's active theme file (``cards:`` section,
raw Discord V2); these tests seed a tiny theme and exercise the plain /
rich / fallback / invalid paths through the real resolution chain.
"""

from __future__ import annotations

from rosemary.core.cards import log_description, text_or
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore, preload_themes
from rosemary.ui import boost_dm
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return f"{key}:{variables.get('role_name', '')}"


class FakeBot:
    theme = load_theme()
    translator = FakeTranslator()

    def __init__(self, tmp_path):
        self.storage = GuildStorage(tmp_path)


class FakeDestination:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, *args, **kwargs):
        entry = dict(kwargs)
        if args:
            entry["content"] = args[0]
        self.calls.append(entry)

    @property
    def last(self) -> dict:
        return self.calls[-1]


async def make_theme(bot, tmp_path, cards_yaml: str):
    """Seed one guild theme with the given ``cards:`` body and snapshot it."""
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir(exist_ok=True)
    (themes_dir / "t.yaml").write_text(f"name: t\ncards:\n{cards_yaml}", encoding="utf-8")
    bot._theme_store = ThemeStore(tmp_path, themes_dir=themes_dir)
    await bot._theme_store.set_active(1, "t")
    bot.guilds = [type("G", (), {"id": 1})()]
    await preload_themes(bot)


async def test_dm_send_uses_plain_override(tmp_path):
    bot = FakeBot(tmp_path)
    await make_theme(
        bot,
        tmp_path,
        '  boost.dm.transferred:\n    - type: 10\n      content: "CUSTOM {role_name}"\n',
    )
    dest = FakeDestination()
    await boost_dm.send(bot, dest, 1, "boost.dm.transferred", role_name="Rosas")
    assert dest.last.get("content") == "CUSTOM Rosas"
    assert "view" not in dest.last


async def test_dm_send_uses_rich_override(tmp_path):
    bot = FakeBot(tmp_path)
    # A structural override (container) is valid at load; maybe_text yields
    # None for it and the DM goes out as a V2 view instead of plain text.
    await make_theme(
        bot,
        tmp_path,
        "  boost.dm.registered:\n"
        "    - type: 17\n"
        "      accent_color: 5865F2\n"
        "      components:\n"
        '        - type: 10\n'
        '          content: "rich!"\n',
    )
    from rosemary.core.cards import maybe_text, maybe_view

    assert await maybe_text(bot, 1, "boost.dm.registered", role_name="x") is None
    assert await maybe_view(bot, 1, "boost.dm.registered", {}) is not None


async def test_dm_send_falls_back_to_translator(tmp_path):
    bot = FakeBot(tmp_path)
    dest = FakeDestination()
    await boost_dm.send(bot, dest, 1, "boost.dm.member_left", role_name="Rosas")
    assert dest.last.get("content") == "boost.dm.member_left:Rosas"


async def test_text_or_and_log_description_fallback(tmp_path):
    bot = FakeBot(tmp_path)
    assert await text_or(bot, 1, "warn.dm", "DEFAULT", guild="g") == "DEFAULT"
    assert (
        await log_description(bot, 1, "boost.logs.rename.description", old="a", new="b")
        == "boost.logs.rename.description:"
    )


async def test_invalid_override_falls_back(tmp_path):
    bot = FakeBot(tmp_path)
    await make_theme(
        bot,
        tmp_path,
        "  boost.dm.invite:\n"
        "    - type: 10\n"
        '      content: "text"\n'
        "    - type: 17\n"
        "      components: []\n",  # container nesting at top level is fine, empty child is not
    )
    dest = FakeDestination()
    await boost_dm.send(bot, dest, 1, "boost.dm.invite", role_name="x")
    assert dest.last.get("content", "").startswith("boost.dm.invite:")
