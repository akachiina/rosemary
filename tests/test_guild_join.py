"""A guild joined while online must get slash commands without a restart.

Commands are synced per guild (never globally), so a server added after
``on_ready`` would have no commands until the next restart/reconnect unless
``on_guild_join`` syncs it. Regression test with fakes.
"""

from __future__ import annotations

from types import SimpleNamespace

from rosemary.bot import RosemaryBot


class _Translator:
    def __init__(self, language: str) -> None:
        self.language = language
        self.calls: list[tuple[int | None, str]] = []

    async def t(self, guild_id, key, **variables):
        self.calls.append((guild_id, key))
        if self.language == "pt-BR":
            return {"about.command.name": "sobre"}.get(key, key)
        return {"about.command.name": "about"}.get(key, key)


def _bot(language: str = "en-US") -> RosemaryBot:
    bot = RosemaryBot.__new__(RosemaryBot)
    bot.translator = _Translator(language)
    bot._command_base_keys = {"about": SimpleNamespace(name="about", description="d")}
    bot._setup_done = True
    bot.synced: list[list[int]] = []

    async def sync_commands(*, guild_ids=None, **kwargs):
        bot.synced.append(list(guild_ids or []))

    bot.sync_commands = sync_commands
    return bot


async def test_guild_join_syncs_only_the_new_guild() -> None:
    bot = _bot("pt-BR")
    guild = SimpleNamespace(id=999)
    await bot.on_guild_join(guild)
    assert bot.synced == [[999]]
    assert bot._command_base_keys["about"].name == "sobre"
    assert (999, "about.command.name") in bot.translator.calls


async def test_guild_join_before_setup_is_noop() -> None:
    bot = _bot()
    bot._setup_done = False
    await bot.on_guild_join(SimpleNamespace(id=999))
    assert bot.synced == []


async def test_reapply_localization_still_syncs_every_guild() -> None:
    bot = _bot("en-US")
    bot._connection = SimpleNamespace(
        guilds=[SimpleNamespace(id=1), SimpleNamespace(id=2)]
    )
    await bot.reapply_command_localization()
    assert bot.synced == [[1], [2]]
    assert bot._command_base_keys["about"].name == "about"
