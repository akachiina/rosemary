"""Card picker for /customize (and the /settings deep-link).

Lists every :class:`rosemary.core.cards.CardSpec` grouped by settings category
and opens the shared :class:`CardEditorView` for the chosen card. Persistence
is always the default ``CardStore``; features that keep overrides elsewhere
wire their own editor instance instead of going through this picker.
"""

from __future__ import annotations

from typing import Any

import discord

from rosemary.core.cards import CardSpec, all_cards, card_store, cards_for_category
from rosemary.ui.card_editor import CardEditorView
from rosemary.ui.containers import ActionRow, TextDisplay, designer_container
from rosemary.ui.menu import MenuView

_SELECT_LIMIT = 25


class CustomizeMenuView(MenuView):
    """Browse registered cards by category and open one in the composer."""

    def __init__(
        self,
        bot,
        guild_id: int,
        *,
        owner_id: int | None = None,
        category: str | None = None,
    ) -> None:
        super().__init__(author_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.category = category
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.register("custom_close", self._close)
        self.register("custom_pick_category", self._pick_category)
        self.register("custom_pick_card", self._pick_card)
        self.register("custom_back", self._back_to_categories)

    async def _t(self, key: str, **kwargs: Any) -> str:
        return await self.bot.translator.t(self.guild_id, key, **kwargs)

    async def prepare(self) -> None:
        await self._apply_menu_timeout()
        self.clear_items()
        for item in await self.build_picker():
            self.add_item(item)

    # -- screens -------------------------------------------------------------

    async def build_picker(self) -> list[discord.ui.ViewItem]:
        theme = self.bot.theme
        parts: list[discord.ui.ViewItem] = [
            designer_container(
                theme.color("brand"),
                TextDisplay(theme.md("title", title=await self._t("cards.customize.title"))),
                TextDisplay(await self._t("cards.customize.choose")),
            )
        ]
        if not all_cards():
            parts.append(
                designer_container(
                    theme.color("info"),
                    TextDisplay(await self._t("cards.customize.empty")),
                )
            )
            return parts

        if self.category is None:
            categories = sorted({spec.category for spec in all_cards()})
            options = [
                discord.SelectOption(
                    label=await self._category_label(value), value=value
                )
                for value in categories[:_SELECT_LIMIT]
            ]
            select = self.make_select(
                custom_id="custom_pick_category",
                placeholder=await self._t("cards.customize.category_placeholder"),
                options=options,
            )
            parts.append(ActionRow(select))
            return parts

        specs = cards_for_category(self.category)[:_SELECT_LIMIT]
        from rosemary.core.mentions import effective_policy

        options = []
        for spec in specs:
            customized = (
                await card_store(self.bot).get_document(self.guild_id, spec.key)
                is not None
            )
            label = await self._t(spec.title_key)
            if customized:
                label = f"✓ {label}"
            try:
                policy = await effective_policy(self.bot, self.guild_id, spec.key)
                policy_label = await self._t(f"cards.mentions.modes.{policy}")
            except Exception:
                policy_label = ""
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=spec.key,
                    description=str(policy_label)[:100] or None,
                )
            )
        select = self.make_select(
            custom_id="custom_pick_card",
            placeholder=await self._t("cards.customize.card_placeholder"),
            options=options,
        )
        parts.append(ActionRow(select))
        back_row = ActionRow(
            self.make_button(
                custom_id="custom_close",
                label=await self._t("cards.editor.buttons.close"),
                style=discord.ButtonStyle.secondary,
            ),
            self.make_button(
                custom_id="custom_back",
                label=await self._t("cards.editor.buttons.back"),
                emoji=theme.emojis.get("back", ""),
            ),
        )
        parts.append(back_row)
        return parts

    async def _category_label(self, value: str) -> str:
        from rosemary.core.settings import SettingCategory

        try:
            category = SettingCategory(value)
        except ValueError:
            return value
        return await self._t(f"settings.category.{category.value}")

    # -- handlers ------------------------------------------------------------

    async def _close(self, interaction: discord.Interaction) -> None:
        self.disable_all_items()
        await interaction.edit(view=self)
        self.stop()

    async def _pick_category(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        await interaction.response.defer()
        self.category = values[0]
        await self.rerender(interaction)

    async def _pick_card(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        spec: CardSpec | None = next(
            (item for item in all_cards() if item.key == values[0]), None
        )
        if spec is None:
            return
        await self._open_editor(interaction, spec)

    async def _back_to_categories(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.category = None
        await self.rerender(interaction)

    async def _open_editor(self, interaction: discord.Interaction, spec: CardSpec) -> None:
        """Swap this message into the composer for ``spec``."""
        await interaction.response.defer()
        store = card_store(self.bot)
        from rosemary.core.mentions import mention_store

        mentions = mention_store(self.bot)

        async def load_doc(guild_id: int):
            return await store.get_document(guild_id, spec.key)

        async def save_doc(guild_id: int, doc: dict[str, Any]) -> None:
            await store.save_document(guild_id, spec.key, doc)

        async def reset_doc(guild_id: int) -> None:
            await store.reset(guild_id, spec.key)

        async def load_mentions(guild_id: int) -> str | None:
            return await mentions.get_policy(guild_id, spec.key)

        async def save_mentions(guild_id: int, policy: str) -> None:
            await mentions.set_policy(guild_id, spec.key, policy)

        editor = CardEditorView(
            self.bot,
            self.guild_id,
            spec.key,
            load_doc=load_doc,
            save_doc=save_doc,
            reset_doc=reset_doc,
            owner_id=self.author_id,
            placeholders_hint=await self._t(spec.placeholders_key),
            exit_factory=lambda: CustomizeMenuView(
                self.bot,
                self.guild_id,
                owner_id=self.author_id,
                category=self.category,
            ),
            load_mentions=load_mentions,
            save_mentions=save_mentions,
        )
        await editor.prepare()
        self.stop()
        await interaction.edit(view=editor)
