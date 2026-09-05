"""Rosemary Discord bot: class definition and entrypoint."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from rosemary.core.logging import setup_logging  # noqa: E402
from rosemary.core.settings import get_setting  # noqa: E402
from rosemary.ui.theme import Theme, load_theme  # noqa: E402

if TYPE_CHECKING:
    from discord.commands import ApplicationCommand

    from rosemary.core.i18n import Translator
    from rosemary.core.storage import GuildStorage

log = logging.getLogger(__name__)

#: Sentinel prefix that can never match real chat text. Returning None from
#: get_prefix raises inside py-cord, so DM messages (no guild) get this instead.
_NO_PREFIX = "__rosemary_no_prefix__"


class RosemaryBot(commands.Bot):
    """Minimal multilingual bot built on py-cord."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        # Prefix is resolved per guild from the general.prefix setting, so plain
        # chat text only triggers a command when it starts with the guild prefix.
        super().__init__(command_prefix=self._get_prefix, intents=intents, description="Rosemary")
        self.storage: GuildStorage | None = None
        self.translator: Translator | None = None
        self.theme: Theme | None = None
        self._command_base_keys: dict[str, ApplicationCommand] = {}
        self._setup_done = False
        self.start_time: float | None = None

    def _setup(self) -> None:
        # py-cord has no setup_hook; run once at first on_ready. add_cog is
        # synchronous here (returns None), unlike discord.py.
        from rosemary.cogs.about import AboutCog
        from rosemary.cogs.anti_invite import AntiInviteCog
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
        from rosemary.cogs.reminders import RemindersCog
        from rosemary.cogs.settings import SettingsCog
        from rosemary.cogs.starboard import StarboardCog
        from rosemary.cogs.utility import UtilityCog
        from rosemary.cogs.welcome import WelcomeCog
        from rosemary.core import card_specs  # noqa: F401  (fills the card registry)
        from rosemary.core.i18n import Translator
        from rosemary.core.storage import GuildStorage

        self.start_time = time.monotonic()

        data_dir = Path(__file__).resolve().parent / "data"
        self.storage = GuildStorage(data_dir=data_dir)

        async def _resolver(guild_id: int | None) -> str:
            settings = await self.storage.get(guild_id) if guild_id is not None else {}
            return settings.get("language", "en-US")

        languages_dir = Path(__file__).resolve().parent / "language"
        self.theme = load_theme()
        self.translator = Translator(
            languages_dir=languages_dir,
            resolver=_resolver,
            default_placeholders=self.theme.emojis,
        )

        self.add_cog(AboutCog(self))
        self.add_cog(LanguageCog(self))
        self.add_cog(SettingsCog(self))
        self.add_cog(CustomizeCog(self))
        self.add_cog(ModerationCog(self))
        self.add_cog(CleanerCog(self))
        self.add_cog(RemindersCog(self))
        self.add_cog(BroadcastCog(self))
        self.add_cog(AntiInviteCog(self))
        self.add_cog(UtilityCog(self))
        self.add_cog(BoostRolesCog(self))
        self.add_cog(BumpReminderCog(self))
        self.add_cog(BumpLeaderboardCog(self))
        self.add_cog(DebugCog(self))
        self.add_cog(InvitesCog(self))
        self.add_cog(WelcomeCog(self))
        self.add_cog(StarboardCog(self))
        self.add_cog(BirthdayCog(self))
        # py-cord's sync add_cog never calls cog_load (and cog listeners added
        # here only fire on the *next* ready dispatch), so background work must
        # be started explicitly. Cogs define an async ``start()`` hook.
        for cog in self.cogs.values():
            start = getattr(cog, "start", None)
            if start is not None:
                asyncio.create_task(start())
        # Snapshot the decorator-registered names (English) before any localization
        # mutates them, so per-guild re-syncs can look up the right catalog keys.
        self._command_base_keys = {cmd.name: cmd for cmd in self.pending_application_commands}
        self._setup_done = True
        log.info("Rosemary loaded: cogs registered")

    async def _get_prefix(self, bot, message) -> str:
        """Resolve the guild-scoped command prefix for a message."""
        if message.guild is None or bot.storage is None:
            return _NO_PREFIX
        prefix = await get_setting(bot.storage, message.guild.id, "general.prefix")
        return prefix or _NO_PREFIX

    async def _sync_guild_commands(self, guild: discord.Guild) -> None:
        """Localize slash command names/descriptions and sync them to one guild."""
        t = self.translator.t
        for base_name, cmd in self._command_base_keys.items():
            key = f"{base_name}.command"
            cmd.name = await t(guild.id, f"{key}.name")
            cmd.description = await t(guild.id, f"{key}.description")
        await self.sync_commands(guild_ids=[guild.id])

    async def reapply_command_localization(self) -> None:
        """Re-register slash commands with names/descriptions in each guild's
        language. Commands are synced per guild; on_ready and every /language
        change call this to keep the command list in sync with the guild."""
        for guild in self.guilds:
            await self._sync_guild_commands(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Sync localized slash commands to a newly joined guild.

        Commands are registered per guild (never globally), so without this a
        server added while the bot is already online would have no commands
        until the next restart/reconnect.
        """
        if not self._setup_done:
            # Joined before the first ready: on_ready covers this guild.
            return
        try:
            await self._sync_guild_commands(guild)
            log.info("Commands synced for joined guild %s", guild.id)
        except Exception as exc:
            log.error("Failed to sync commands for joined guild %s: %s", guild.id, exc)

    async def on_ready(self) -> None:
        """Register cogs on first ready, publish an explicit online presence
        (Discord otherwise shows the bot grey/offline despite the gateway being
        connected), then sync commands instantly per guild in its language."""
        if self._setup_done is False:
            self._setup()

        await self.change_presence(status=discord.Status.online)
        log.info("Rosemary online as %s", self.user)

        if self.guilds:
            await self.reapply_command_localization()
            log.info(
                "Commands synced in %d guild(s): %s",
                len(self.guilds),
                [g.id for g in self.guilds],
            )
        else:
            log.info("Rosemary online (no guilds yet)")

    async def on_application_command_error(
        self, context: discord.ApplicationContext, exception: Exception
    ) -> None:
        log.error("Error in command %s: %s", context.command, exception)


def main() -> None:
    """Boot the bot: load ``.env``, require ``BOT_TOKEN``, then run."""
    env_file = Path(__file__).resolve().parent / ".env"
    load_dotenv(env_file if env_file.exists() else None)
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("ERROR: BOT_TOKEN is not set. Add it to a .env file or export it.")
        raise SystemExit(1)
    setup_logging()
    RosemaryBot().run(token)


if __name__ == "__main__":
    main()
