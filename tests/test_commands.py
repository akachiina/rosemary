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
    """Top-level commands plus every nested group subcommand."""
    found = []

    def walk(cmd) -> None:
        found.append(cmd)
        for sub in getattr(cmd, "subcommands", None) or []:
            walk(sub)

    for cmd in bot.pending_application_commands:
        walk(cmd)
    return found


def test_command_option_raw_types_are_classes() -> None:
    bot = _Bot()
    from rosemary.cogs.birthdays import BirthdayCog
    from rosemary.cogs.boost_roles import BoostRolesCog
    from rosemary.cogs.broadcast import BroadcastCog
    from rosemary.cogs.debug import DebugCog
    from rosemary.cogs.moderation import ModerationCog
    from rosemary.cogs.settings import SettingsCog

    bot.add_cog(BirthdayCog(bot))
    bot.add_cog(BoostRolesCog(bot))
    bot.add_cog(BroadcastCog(bot))
    bot.add_cog(DebugCog(bot))
    bot.add_cog(SettingsCog(bot))
    bot.add_cog(ModerationCog(bot))
    from rosemary.cogs.info import InfoCog

    bot.add_cog(InfoCog(bot))
    from rosemary.cogs.bump_leaderboard import BumpLeaderboardCog
    from rosemary.cogs.bump_reminder import BumpReminderCog

    bot.add_cog(BumpReminderCog(bot))
    bot.add_cog(BumpLeaderboardCog(bot))
    # Mirror production: the shared `/bump` group is registered explicitly.
    from rosemary.bot import rebind_shared_group_commands
    from rosemary.cogs.bump_leaderboard import bump_group

    bot.add_application_command(bump_group)
    rebind_shared_group_commands(bot)
    from rosemary.cogs.roleplay import RoleplayCog

    bot.add_cog(RoleplayCog(bot))
    from rosemary.cogs.confessions import ConfessionsCog

    bot.add_cog(ConfessionsCog(bot))

    commands_ = _all_commands(bot)
    assert commands_, "expected the new slash commands to be registered"
    assert {
        "ban",
        "birthday set",
        "birthday list",
        "birthday clear",
        "kick",
        "mute",
        "warn",
        "warnings",
        "unwarn",
        "settings",
        "debug",
        "boost",
        "boost admin",
        "bump leaderboard",
        "bump stats",
        "bump test",
        "bump test_reminder",
        "bump status",
        "bump_admin test_leaderboard",
        "bump_admin reset_week",
        "bump_admin add_test",
        "info server",
        "info user",
        "info emoji",
        "info role",
        "info channel",
        "confess send",
        "confess reply",
        "confess ban",
        "confess unban",
        "confess banned",
        "roleplay hug",
        "roleplay kiss",
        "roleplay cuddle",
        "broadcast start",
        "broadcast stop",
    } <= {c.qualified_name for c in commands_}

    for cmd in commands_:
        for option in getattr(cmd, "options", []):
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
