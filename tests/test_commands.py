"""Guard against `from __future__ import annotations` breaking py-cord options.

py-cord inspects slash-command annotations with ``inspect.signature`` and never
evaluates them, so stringified annotations (e.g. ``"discord.Option(...)"``)
silently become string raw types that crash at invocation with
``issubclass() arg 1 must be a class``. This test asserts every option's raw
type is a real class and that no option's ``_raw_type`` is a ``str``.
"""

from __future__ import annotations

import discord
from discord.enums import SlashCommandOptionType
from discord.ext import commands

from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme


class _Bot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=None)
        self.storage = GuildStorage("/tmp/cmd_options")
        self.theme = load_theme()


def _all_commands(bot: _Bot) -> list:
    return list(bot.pending_application_commands)


def test_command_option_raw_types_are_classes() -> None:
    bot = _Bot()
    from rosemary.cogs.boost_roles import BoostRolesCog
    from rosemary.cogs.debug import DebugCog
    from rosemary.cogs.moderation import ModerationCog
    from rosemary.cogs.settings import SettingsCog

    bot.add_cog(BoostRolesCog(bot))
    bot.add_cog(DebugCog(bot))
    bot.add_cog(SettingsCog(bot))
    bot.add_cog(ModerationCog(bot))
    from rosemary.cogs.bump_leaderboard import BumpLeaderboardCog
    from rosemary.cogs.bump_reminder import BumpReminderCog

    bot.add_cog(BumpReminderCog(bot))
    bot.add_cog(BumpLeaderboardCog(bot))

    commands_ = _all_commands(bot)
    assert commands_, "expected the new slash commands to be registered"
    assert {
        "ban",
        "kick",
        "mute",
        "warn",
        "warnings",
        "unwarn",
        "settings",
        "debug",
        "boost",
        "boost_admin",
        "bump_leaderboard",
        "bump_stats",
        "test_bump",
        "test_bump_reminder",
        "bump_status",
        "test_bump_leaderboard",
        "reset_bump_week",
        "add_test_bumps",
    } <= {c.qualified_name for c in commands_}

    for cmd in commands_:
        for option in cmd.options:
            raw = option._raw_type
            assert not isinstance(raw, str), (
                f"command {cmd.qualified_name!r} option {option.name!r} has a "
                f"stringified annotation: {raw!r}. py-cord cannot parse these; "
                "remove `from __future__ import annotations` from the cog file."
            )


def test_member_options_resolve_to_user_type() -> None:
    bot = _Bot()
    from rosemary.cogs.moderation import ModerationCog

    bot.add_cog(ModerationCog(bot))
    mute = next(c for c in _all_commands(bot) if c.qualified_name == "mute")
    member = next(o for o in mute.options if o.name == "member")
    assert member.input_type is SlashCommandOptionType.user
    assert member._raw_type is discord.Member
