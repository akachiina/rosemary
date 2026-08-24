"""The ``/language`` command - set the per-guild language.

Gated by ``manage_guild``; responds ephemeral in the newly selected language.
The option lists every available language dynamically, read from the language
folder (``{flag} {name}`` from the ``language_meta`` block of each YAML), shown
as fixed suggestions (no type-to-filter narrowing).
"""

import discord
from discord.ext import commands

from rosemary.core.debug import send_channel_log
from rosemary.ui.containers import DesignerView, TextDisplay, designer_container


def _language_candidates(ctx: discord.AutocompleteContext):
    """Yield every loaded language as an ``OptionChoice`` (name = flag + name)."""
    translator = ctx.bot.translator
    return [
        discord.OptionChoice(
            name=translator.language_display(code),
            value=code,
        )
        for code in translator.available_languages
    ]


class LanguageCog(commands.Cog):
    """Server language selection."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @discord.slash_command(
        name="language",
        description="Set the server language",
        default_member_permissions=discord.Permissions(manage_guild=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def language(
        self,
        ctx: discord.ApplicationContext,
        language: discord.Option(
            str,
            description="Language code (e.g. en-US, pt-BR)",
            autocomplete=discord.utils.basic_autocomplete(
                _language_candidates,
                filter=lambda ctx, item: True,
            ),
        ),
    ) -> None:
        """Choose the language used by the bot in this server."""
        translator = self.bot.translator
        t = translator.t
        available = translator.available_languages

        if language not in available:
            labels = "\n".join(
                f"- {translator.language_display(code)}" for code in available
            )
            message = await t(
                ctx.guild_id,
                "language.invalid",
                language=language,
                available=labels,
            )
        else:
            old_lang = (await self.bot.storage.get(ctx.guild_id)).get("language")
            await self.bot.storage.set(ctx.guild_id, "language", language)
            message = await t(ctx.guild_id, "language.updated", language=language)
            await send_channel_log(
                self.bot,
                ctx.guild_id,
                await t(ctx.guild_id, "settings.log.language_title"),
                await t(
                    ctx.guild_id,
                    "settings.log.language_description",
                    old=old_lang or "-",
                    new=language,
                    author=ctx.author.mention,
                ),
            )

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("success" if language in available else "danger"),
                TextDisplay(self.bot.theme.md("title", title=message)),
            )
        )
        await ctx.respond(view=view, ephemeral=True)

        if language in available:
            # Rename/re-describe the guild's commands in the new language.
            await self.bot.reapply_command_localization()
