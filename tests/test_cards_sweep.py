"""Override-vs-default resolution at the swept send sites."""

from __future__ import annotations

from rosemary.core.cards import log_description, text_or
from rosemary.core.storage import GuildStorage
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


async def make_store(bot, key, doc):
    from rosemary.core.cards import card_store

    await card_store(bot).save_document(1, key, doc)


async def test_dm_send_uses_plain_override(tmp_path):
    bot = FakeBot(tmp_path)
    await make_store(
        bot,
        "boost.dm.transferred",
        {"v": 1, "blocks": [{"type": "text", "body": "CUSTOM {role_name}"}]},
    )
    dest = FakeDestination()
    await boost_dm.send(bot, dest, 1, "boost.dm.transferred", role_name="Rosas")
    assert dest.last.get("content") == "CUSTOM Rosas"
    assert "view" not in dest.last


async def test_dm_send_uses_rich_override(tmp_path):
    bot = FakeBot(tmp_path)
    await make_store(
        bot,
        "boost.dm.registered",
        {
            "v": 1,
            "blocks": [
                {
                    "type": "container",
                    "color": "brand",
                    "children": [{"type": "text", "body": "rich!"}],
                }
            ],
        },
    )
    dest = FakeDestination()
    await boost_dm.send(bot, dest, 1, "boost.dm.registered", role_name="x")
    assert "view" in dest.last and "content" not in dest.last


async def test_dm_send_falls_back_to_translator(tmp_path):
    bot = FakeBot(tmp_path)
    dest = FakeDestination()
    await boost_dm.send(bot, dest, 1, "boost.dm.member_left", role_name="Rosas")
    assert dest.last.get("content") == "boost.dm.member_left:Rosas"


async def test_text_or_and_log_description_fallback(tmp_path):
    bot = FakeBot(tmp_path)
    assert (
        await text_or(bot, 1, "warn.dm", "DEFAULT", guild="g")
        == "DEFAULT"
    )
    assert (
        await log_description(bot, 1, "boost.logs.rename.description", old="a", new="b")
        == "boost.logs.rename.description:"
    )


async def test_invalid_override_falls_back(tmp_path):
    bot = FakeBot(tmp_path)
    await make_store(
        bot,
        "boost.dm.invite",
        {"v": 1, "blocks": [{"type": "row", "buttons": []}]},
    )
    dest = FakeDestination()
    await boost_dm.send(bot, dest, 1, "boost.dm.invite", role_name="x")
    assert dest.last.get("content", "").startswith("boost.dm.invite:")
