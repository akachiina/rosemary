"""The ``/personalizar`` command group: full card editor + utilities."""

import contextlib
import json

import discord
from discord.ext import commands

from rosemary.core.card_service import (
    export_payload,
    get_effective_document,
    import_payload,
    render_card,
)
from rosemary.core.cards import get_card
from rosemary.ui.confirm import ConfirmView
from rosemary.ui.customize_menu import CustomizeMenuView


def _card_candidates(ctx: discord.AutocompleteContext) -> list[str]:
    from rosemary.core.cards import all_cards

    query = (ctx.value or "").lower()
    keys = sorted(spec.key for spec in all_cards())
    return [key for key in keys if query in key.lower()][:25]


def _card_option(description: str):
    return discord.Option(
        str,
        description=description,
        autocomplete=discord.utils.basic_autocomplete(_card_candidates),
    )


class ResetConfirmView(ConfirmView):
    """Confirm resetting one card to its default."""

    def __init__(self, bot, guild_id: int, owner_id: int | None, key: str) -> None:
        from rosemary.core.card_actions import sync_guild
        from rosemary.core.card_history import history_store
        from rosemary.core.cards import card_store

        async def on_confirm(interaction: discord.Interaction) -> str:
            await card_store(bot).reset(guild_id, key)
            from rosemary.core.mentions import mention_store

            await mention_store(bot).reset(guild_id, key)
            await history_store(bot).append(
                guild_id, key, {"v": 2, "blocks": []}, owner_id
            )
            await sync_guild(bot, guild_id)
            t = bot.translator.t
            return await t(guild_id, "cards.editor.reset_done")

        super().__init__(
            bot,
            guild_id=guild_id,
            owner_id=owner_id,
            on_confirm=on_confirm,
            confirm_style=discord.ButtonStyle.danger,
        )
        self._key = key

    async def question_items(self):
        from rosemary.ui.containers import TextDisplay

        t = self.bot.translator.t
        return [
            TextDisplay(
                await t(
                    self.guild_id, "cards.editor.confirm_reset", key=self._key
                )
            )
        ]


class CustomizeCog(commands.Cog):
    """Card editor, manager and utilities (localized as /personalizar)."""

    def __init__(self, bot) -> None:
        self.bot = bot

    personalize = discord.SlashCommandGroup(
        "customize",
        description="Customize the bot's messages for this server",
        default_member_permissions=discord.Permissions(manage_guild=True),
        contexts={discord.InteractionContextType.guild},
    )

    def _known_key(self, key: str):
        return get_card(key)

    async def _unknown(self, ctx: discord.ApplicationContext):
        t = self.bot.translator.t
        return await ctx.respond(
            await t(ctx.guild_id, "cards.editor.unknown_card"), ephemeral=True
        )

    @personalize.command(name="menu")
    async def customize_menu(self, ctx: discord.ApplicationContext) -> None:
        """Open the card picker (ephemeral)."""
        if not ctx.response.is_done():
            await ctx.response.defer(ephemeral=True)
        view = CustomizeMenuView(self.bot, ctx.guild_id, owner_id=ctx.author.id)
        await view.prepare()
        await ctx.respond(view=view, ephemeral=True)

    @personalize.command(name="ver")
    async def customize_view(
        self, ctx: discord.ApplicationContext, card: _card_option("Card key (ex: bump.reminder)")
    ) -> None:
        """Preview what members receive for one card (ephemeral)."""
        t = self.bot.translator.t
        if self._known_key(card) is None:
            return await self._unknown(ctx)
        if not ctx.response.is_done():
            await ctx.response.defer(ephemeral=True)
        view = await render_card(self.bot, ctx.guild_id, card)
        if view is None:
            return await ctx.respond(
                await t(ctx.guild_id, "cards.editor.compare_no_default"),
                ephemeral=True,
            )
        await ctx.respond(view=view, ephemeral=True)

    @personalize.command(name="testar")
    async def customize_test(
        self, ctx: discord.ApplicationContext, card: _card_option("Card key (ex: bump.reminder)")
    ) -> None:
        """Send a live test of one card (ephemeral, with samples)."""
        t = self.bot.translator.t
        if self._known_key(card) is None:
            return await self._unknown(ctx)
        if not ctx.response.is_done():
            await ctx.response.defer(ephemeral=True)
        view = await render_card(self.bot, ctx.guild_id, card)
        if view is None:
            return await ctx.respond(
                await t(ctx.guild_id, "cards.editor.compare_no_default"),
                ephemeral=True,
            )
        await ctx.followup.send(view=view, ephemeral=True)
        await ctx.respond(await t(ctx.guild_id, "cards.editor.test_sent"), ephemeral=True)

    @personalize.command(name="resetar")
    async def customize_reset(
        self, ctx: discord.ApplicationContext, card: _card_option("Card key (ex: bump.reminder)")
    ) -> None:
        """Reset one card to its default (asks first)."""
        if self._known_key(card) is None:
            return await self._unknown(ctx)
        view = ResetConfirmView(self.bot, ctx.guild_id, ctx.author.id, card)
        await view.prepare()
        await ctx.respond(view=view, ephemeral=True)

    @personalize.command(name="exportar")
    async def customize_export(
        self, ctx: discord.ApplicationContext, card: _card_option("Card key (ex: bump.reminder)")
    ) -> None:
        """Download one card as JSON (ephemeral)."""
        import io

        t = self.bot.translator.t
        if self._known_key(card) is None:
            return await self._unknown(ctx)
        if not ctx.response.is_done():
            await ctx.response.defer(ephemeral=True)
        doc = await get_effective_document(self.bot, ctx.guild_id, card)
        if doc is None:
            return await ctx.respond(
                await t(ctx.guild_id, "cards.editor.compare_no_default"),
                ephemeral=True,
            )
        filename, data = export_payload(card, doc)
        with contextlib.suppress(discord.HTTPException):
            await ctx.followup.send(
                content=await t(ctx.guild_id, "cards.editor.exported"),
                file=discord.File(io.BytesIO(data), filename=filename),
                ephemeral=True,
            )

    @personalize.command(name="importar")
    async def customize_import(
        self,
        ctx: discord.ApplicationContext,
        card: _card_option("Card key (ex: bump.reminder)"),
        arquivo: discord.Option(discord.Attachment, description="JSON exportado pelo editor"),
    ) -> None:
        """Import card JSON exported before (asks nothing, validates first)."""
        t = self.bot.translator.t
        if self._known_key(card) is None:
            return await self._unknown(ctx)
        if not ctx.response.is_done():
            await ctx.response.defer(ephemeral=True)
        try:
            raw = await arquivo.read()
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            payload = None
        ok, _reason = await import_payload(self.bot, ctx.guild_id, card, payload)
        if not ok:
            return await ctx.respond(
                await t(ctx.guild_id, "cards.editor.import_invalid"), ephemeral=True
            )
        await ctx.respond(await t(ctx.guild_id, "cards.editor.imported"), ephemeral=True)
