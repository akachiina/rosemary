"""The ``/about`` command and the themed ``!help`` list.

``/about`` renders bot information with Components V2 and has a ``!about``
prefix alias. ``!help`` replaces py-cord's default English help with a themed
card listing the server's TEXT commands (prefix aliases keep latin names by
design; only their descriptions localize), grouped and rendered through the
``help.card`` CardSpec: a theme may restyle the whole card."""

from __future__ import annotations

import discord
from discord.ext import commands

from rosemary import __version__
from rosemary.ui.containers import (
    DesignerView,
    TextDisplay,
    designer_container,
    divider,
)
from rosemary.ui.responses import PrefixContext

#: ``!help`` sections: (group key, ((prefix command, description key), ...)).
#: Prefix names stay latin on purpose (the slash commands localize per guild;
#: the prefix aliases do not), so only the descriptions localize. Each
#: description reuses the matching slash command's catalog key: one source of
#: truth per feature. Swept by test_prefix_commands.py against the commands
#: actually registered, so a new alias without a help entry fails the suite.
_HELP_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "moderation",
        (
            ("ban", "ban.command.description"),
            ("kick", "kick.command.description"),
            ("warn", "warn.command.description"),
            ("mute", "mute.command.description"),
            ("warnings", "warnings.command.description"),
            ("unwarn", "unwarn.command.description"),
            ("limpar", "limpar.command.description"),
        ),
    ),
    (
        "info",
        (
            ("serverinfo", "info_server.command.description"),
            ("userinfo", "info_user.command.description"),
            ("roleinfo", "info_role.command.description"),
            ("channelinfo", "info_channel.command.description"),
            ("emoji", "info_emoji.command.description"),
        ),
    ),
    (
        "general",
        (
            ("ping", "ping.command.description"),
            ("about", "about.command.description"),
            ("help", "help.self_description"),
        ),
    ),
)


async def default_help_document(bot, guild_id: int, **variables) -> dict:
    """Catalog-default ``!help`` card (no theme override).

    The send site renders every group (heading included) into its ``*_list``
    variable, so this layout is purely structural: heading, intro, divider,
    one text block per list. An empty list means the group is off for the
    guild (interactions with roleplay disabled) and its block is omitted;
    a missing value keeps the placeholder so editor previews stay rich.
    """
    children: list[dict] = [
        {"type": "text", "body": "# {title}"},
        {"type": "text", "body": "{body}"},
        {"type": "divider"},
    ]
    for name in ("moderation_list", "info_list", "general_list", "interactions_list"):
        value = variables.get(name)
        if isinstance(value, str) and not value.strip():
            continue
        children.append({"type": "text", "body": f"{{{name}}}"})
    return {
        "v": 1,
        "blocks": [{"type": "container", "color": "brand", "children": children}],
    }


def _register_default_builders() -> None:
    from rosemary.core.cards import set_default_builder

    set_default_builder("help.card", default_help_document)


_register_default_builders()


class AboutCog(commands.Cog):
    """Bot information."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @discord.slash_command(
        name="about",
        description="About Rosemary",
    )
    async def about(self, ctx: discord.ApplicationContext) -> None:
        """Show information about Rosemary."""
        from rosemary.core.settings import get_setting

        t = self.bot.translator.t
        if ctx.guild_id is not None:
            channel = await get_setting(self.bot.storage, ctx.guild_id, "updater.channel")
        else:
            channel = "stable"
        from rosemary.core.card_service import CardPayload, render_card_message

        variables = {"version": __version__, "channel": channel}
        payload, _allowed = await render_card_message(
            self.bot, ctx.guild_id, "about.card", variables
        )
        if payload is None:
            # Sections require an accessory (Thumbnail/Button); plain text
            # must use TextDisplay directly to keep the message valid.
            container = designer_container(
                self.bot.theme.color("brand"),
                TextDisplay(
                    self.bot.theme.md(
                        "title", title=await t(ctx.guild_id, "about.title")
                    )
                ),
                TextDisplay(await t(ctx.guild_id, "about.text")),
                divider(),
                TextDisplay(
                    await t(ctx.guild_id, "about.version", version=__version__, channel=channel)
                ),
            )
            view = DesignerView(store=False)
            view.add_item(container)
            payload = CardPayload(view=view)
        await ctx.respond(**payload.message_kwargs())

    #: prefix commands ---------------------------------------------------

    @commands.command(name="about")
    async def about_prefix(self, ctx: commands.Context) -> None:
        """Prefix alias: !about"""
        await self.about(PrefixContext(ctx))

    @commands.command(name="help")
    async def help_prefix(self, ctx: commands.Context) -> None:
        """Themed list of the server's text commands (py-cord help removed).

        Prefix names are latin and stable, so only the descriptions localize.
        The rendered groups travel as ``help.card`` variables and the send
        goes through :func:`render_card_message`: a theme override replaces
        the whole card, the default builder lays it out, tracing included.
        """
        if getattr(self.bot, "translator", None) is None:
            return  # before _setup(): no commands or translations loaded yet
        from rosemary.core.card_service import CardPayload, render_card_message
        from rosemary.core.roleplay import ACTIONS
        from rosemary.core.settings import get_setting

        t = self.bot.translator.t
        theme = self.bot.theme
        guild_id = ctx.guild.id if ctx.guild is not None else None
        prefix = "!"
        if guild_id is not None:
            prefix = await get_setting(self.bot.storage, guild_id, "general.prefix") or "!"

        async def render_section(group: str, entries) -> str:
            """One group: ``##`` heading plus one ``**!name:** description`` line."""
            lines = [theme.md("section", title=await t(guild_id, f"help.group_{group}"))]
            for name, desc_key in entries:
                lines.append(
                    theme.md(
                        "category",
                        category_label=f"`{prefix}{name}`",
                        category=await t(guild_id, desc_key),
                    )
                )
            return "\n".join(lines)

        variables: dict[str, str] = {
            "prefix": prefix,
            "title": await t(guild_id, "help.title"),
            "body": await t(guild_id, "help.body", prefix=prefix),
        }
        for group, entries in _HELP_SECTIONS:
            variables[f"{group}_list"] = await render_section(group, entries)
        interactions = ""
        if guild_id is not None and await get_setting(
            self.bot.storage, guild_id, "roleplay.enabled"
        ):
            verbs = "`" + " · ".join(f"{prefix}{action}" for action in ACTIONS) + "`"
            interactions = "\n".join(
                [
                    theme.md(
                        "section", title=await t(guild_id, "help.group_interactions")
                    ),
                    theme.md(
                        "category",
                        category_label=verbs,
                        category=await t(
                            guild_id, "help.roleplay_description", prefix=prefix
                        ),
                    ),
                ]
            )
        variables["interactions_list"] = interactions

        payload, allowed = await render_card_message(
            self.bot, guild_id, "help.card", variables
        )
        if payload is None:
            # Defensive: a broken themed document never breaks the listing.
            view = DesignerView(store=False)
            view.add_item(
                designer_container(
                    theme.color("brand"),
                    TextDisplay(theme.md("title", title=variables["title"])),
                    TextDisplay(variables["body"]),
                )
            )
            payload = CardPayload(view=view)
        await ctx.reply(
            **payload.message_kwargs(), allowed_mentions=allowed, mention_author=False
        )
