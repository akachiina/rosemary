"""A guild joined while online must get slash commands without a restart.

Commands are synced per guild (never globally), so a server added after
``on_ready`` would have no commands until the next restart/reconnect unless
``on_guild_join`` syncs it. Regression test with fakes.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from discord.ext import commands

from rosemary.bot import _COMMAND_NAME_RE, RosemaryBot
from rosemary.core.i18n import Translator
from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme

ROOT = Path(__file__).resolve().parents[2]
LANG_DIR = ROOT / "rosemary" / "language"


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


async def test_invalid_localized_name_falls_back_to_base() -> None:
    """A bad translation must not break the sync (400 Invalid Form Body)."""

    class _BadTranslator:
        async def t(self, guild_id, key, **variables):
            if key.endswith(".name"):
                return "not a valid name!!"
            return "description"

    bot = RosemaryBot.__new__(RosemaryBot)
    bot.translator = _BadTranslator()
    bot._command_base_keys = {"about": SimpleNamespace(name="about", description="d")}
    bot._setup_done = True
    bot.synced = []

    async def sync_commands(*, guild_ids=None, **kwargs):
        bot.synced.append(list(guild_ids or []))

    bot.sync_commands = sync_commands
    await bot.on_guild_join(SimpleNamespace(id=7))
    assert bot._command_base_keys["about"].name == "about"
    assert bot.synced == [[7]]


def _register_all(bot: commands.Bot) -> None:
    """Register every cog the way ``RosemaryBot._setup`` does."""
    from rosemary.cogs.about import AboutCog
    from rosemary.cogs.birthdays import BirthdayCog
    from rosemary.cogs.boost_roles import BoostRolesCog
    from rosemary.cogs.broadcast import BroadcastCog
    from rosemary.cogs.bump_leaderboard import BumpLeaderboardCog
    from rosemary.cogs.bump_reminder import BumpReminderCog
    from rosemary.cogs.cleaner import CleanerCog
    from rosemary.cogs.customize import CustomizeCog
    from rosemary.cogs.debug import DebugCog
    from rosemary.cogs.invites import InvitesCog
    from rosemary.cogs.language import LanguageCog
    from rosemary.cogs.moderation import ModerationCog
    from rosemary.cogs.partnerships import PartnershipsCog
    from rosemary.cogs.reminders import RemindersCog
    from rosemary.cogs.settings import SettingsCog
    from rosemary.cogs.starboard import StarboardCog
    from rosemary.cogs.tickets import TicketsCog
    from rosemary.cogs.updater import UpdaterCog
    from rosemary.cogs.utility import UtilityCog
    from rosemary.cogs.welcome import WelcomeCog

    for cog_class in (
        AboutCog,
        BirthdayCog,
        BoostRolesCog,
        BroadcastCog,
        BumpLeaderboardCog,
        BumpReminderCog,
        CleanerCog,
        CustomizeCog,
        DebugCog,
        InvitesCog,
        LanguageCog,
        ModerationCog,
        PartnershipsCog,
        RemindersCog,
        SettingsCog,
        StarboardCog,
        TicketsCog,
        UpdaterCog,
        UtilityCog,
        WelcomeCog,
    ):
        bot.add_cog(cog_class(bot))


def _resolver(language: str):
    async def resolve(guild_id):
        return language

    return resolve


@pytest.mark.parametrize("language", ["en-US", "pt-BR"])
async def test_every_command_localizes_to_valid_discord_name(language: str) -> None:
    """Every registered command must resolve to a valid name in both catalogs.

    Regression for /limpar: a missing ``<base>.command.name`` key made ``t()``
    return the raw dotted key, and Discord rejected the whole bulk sync with
    ``400 Invalid Form Body (In <n>.name)``.
    """
    bot = commands.Bot(command_prefix="!")
    bot.storage = GuildStorage(tempfile.mkdtemp())
    bot.theme = load_theme()
    _register_all(bot)
    assert bot.pending_application_commands, "expected registered commands"

    theme = load_theme()
    translator = Translator(
        LANG_DIR, resolver=_resolver(language), default_placeholders=theme.emojis
    )
    invalid: list[str] = []
    for cmd in bot.pending_application_commands:
        name = await translator.t(1, f"{cmd.name}.command.name")
        if (
            not _COMMAND_NAME_RE.match(name)
            or name != name.lower()
            or name == f"{cmd.name}.command.name"
        ):
            invalid.append(f"{cmd.name!r} -> {name!r}")
    assert invalid == []
