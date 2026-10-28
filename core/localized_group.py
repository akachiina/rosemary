"""Slash command groups that dispatch by the invoking guild's language.

py-cord resolves a group subcommand by matching the interaction's name against
``group.subcommands[i].name``. Rosemary keeps one command tree per process and
renames those objects for each guild before syncing, so the live names always
belong to the last synced guild. With more than one guild language every other
guild's localized subcommand failed with
``AttributeError: 'NoneType' object has no attribute 'invoke'``.

``LocalizedSlashCommandGroup`` keeps py-cord's behavior but resolves the
incoming name through the bot's per-guild alias map first, so dispatch never
depends on the sync order.
"""

from __future__ import annotations

import discord
from discord.utils import find


class LocalizedSlashCommandGroup(discord.SlashCommandGroup):
    """Drop-in ``discord.SlashCommandGroup`` with guild-aware dispatch."""

    async def _invoke(self, ctx: discord.ApplicationContext) -> None:
        option = ctx.interaction.data["options"][0]
        sub = self._resolve(ctx, option["name"])
        if sub is None:
            raise discord.ApplicationCommandError(
                f"Subcommand {option['name']!r} of /{self.name} is not registered"
            )
        option["resolved"] = ctx.interaction.data.get("resolved")
        ctx.interaction.data = option
        await sub.invoke(ctx)

    async def invoke_autocomplete_callback(
        self, ctx: discord.AutocompleteContext
    ) -> None:
        option = ctx.interaction.data["options"][0]
        sub = self._resolve(ctx, option["name"])
        if sub is None:
            raise discord.ApplicationCommandError(
                f"Subcommand {option['name']!r} of /{self.name} is not registered"
            )
        ctx.interaction.data = option
        await sub.invoke_autocomplete_callback(ctx)

    def _resolve(
        self,
        ctx: discord.ApplicationContext | discord.AutocompleteContext,
        name: str,
    ) -> discord.SlashCommand | discord.SlashCommandGroup | None:
        """Return the subcommand registered as ``name`` in this guild.

        The bot's alias map is authoritative (it was built from the catalog for
        the guild); py-cord's live-name lookup stays as a fallback for tests and
        for guilds whose map has not been seeded yet.
        """
        resolver = getattr(ctx.bot, "resolve_subcommand_name", None)
        if resolver is not None:
            sub = resolver(ctx.interaction.guild_id, self, name)
            if sub is not None:
                return sub
        return find(lambda candidate: candidate.name == name, self.subcommands)
