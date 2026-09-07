"""Card picker for /customize (and the /settings deep-link).

Lists every :class:`rosemary.core.cards.CardSpec` grouped by settings category
and opens the shared :class:`CardEditorView` for the chosen card. Persistence
is always the default ``CardStore``; features that keep overrides elsewhere
wire their own editor instance instead of going through this picker.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from rosemary.core.cards import CardSpec, all_cards, card_store, cards_for_category
from rosemary.ui.card_editor import CardEditorView
from rosemary.ui.containers import ActionRow, TextDisplay, designer_container
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)

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
        self.page = 0
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.register("custom_close", self._close)
        self.register("custom_pick_category", self._pick_category)
        self.register("custom_pick_card", self._pick_card)
        self.register("custom_back", self._back_to_categories)
        self.register("custom_prev", self._prev_page)
        self.register("custom_next", self._next_page)

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
            categories = self._ordered_categories()
            window, _pages = self._window(categories)
            options = [
                discord.SelectOption(
                    label=(await self._category_label(value))[:100], value=value
                )
                for value in window
            ]
            select = self.make_select(
                custom_id="custom_pick_category",
                placeholder=await self._t("cards.customize.category_placeholder"),
                options=options,
            )
            parts.append(ActionRow(select))
            parts.extend(await self._pager_row(kind="categories"))
            return parts

        specs = cards_for_category(self.category)
        if not specs:
            parts.append(
                designer_container(
                    theme.color("info"),
                    TextDisplay(await self._t("cards.customize.empty_category")),
                )
            )
        else:
            from rosemary.core.mentions import effective_policy

            window, _pages = self._window(specs)
            options = []
            for spec in window:
                customized = (
                    await card_store(self.bot).get_document(self.guild_id, spec.key)
                    is not None
                )
                title = await self._t(spec.title_key)
                if customized:
                    check = self.bot.theme.emoji("check") if self.bot.theme else "✓"
                    title = f"{check or '✓'} {title}"
                try:
                    policy = await effective_policy(self.bot, self.guild_id, spec.key)
                    policy_label = await self._t(f"cards.mentions.modes.{policy}")
                except Exception:
                    log.warning("mention policy lookup failed for card %s", spec.key)
                    policy_label = ""
                options.append(
                    discord.SelectOption(
                        label=title if len(title) <= 100 else title[:99] + "…",
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
            parts.extend(await self._pager_row(kind="cards"))
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

    def _ordered_categories(self) -> list[str]:
        """Categories in /settings order, then any card-only extras sorted."""
        from rosemary.core.settings import CATEGORIES

        known = [category.value for category in CATEGORIES]
        present = {spec.category for spec in all_cards()}
        ordered = [name for name in known if name in present]
        ordered += sorted(present - set(ordered))
        return ordered

    def _window(self, items: list) -> tuple[list, int]:
        """Current page slice; clamps a stale page instead of showing nothing."""
        pages = max((len(items) + _SELECT_LIMIT - 1) // _SELECT_LIMIT, 1)
        self.page = min(max(self.page, 0), pages - 1)
        start = self.page * _SELECT_LIMIT
        return items[start : start + _SELECT_LIMIT], pages

    async def _pager_row(self, *, kind: str) -> list[discord.ui.ViewItem]:
        """Prev/Next buttons plus a page footer; empty on a single page."""
        total = (
            len(self._ordered_categories())
            if kind == "categories"
            else len(cards_for_category(self.category or ""))
        )
        pages = max((total + _SELECT_LIMIT - 1) // _SELECT_LIMIT, 1)
        if pages <= 1:
            return []
        return [
            ActionRow(
                self.make_button(
                    custom_id="custom_prev",
                    label=await self._t("cards.customize.prev"),
                    disabled=self.page <= 0,
                ),
                self.make_button(
                    custom_id="custom_next",
                    label=await self._t("cards.customize.next"),
                    disabled=self.page >= pages - 1,
                ),
            ),
            TextDisplay(
                await self._t("cards.customize.page", page=self.page + 1, pages=pages)
            ),
        ]

    async def _category_label(self, value: str) -> str:
        from rosemary.core.settings import SettingCategory

        try:
            category = SettingCategory(value)
        except ValueError:
            return value
        return await self._t(f"settings.category.{category.value}")

    # -- handlers ------------------------------------------------------------

    async def _close(self, interaction: discord.Interaction) -> None:
        import contextlib

        self.stop()
        with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            self.disable_all_items()
            await interaction.edit(view=self)

    async def _pick_category(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not values:
            return await self.rerender(interaction)
        self.category = values[0]
        self.page = 0
        await self.rerender(interaction)

    async def _pick_card(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not values:
            return await self.rerender(interaction)
        spec: CardSpec | None = next(
            (item for item in all_cards() if item.key == values[0]), None
        )
        if spec is None:
            return await self.rerender(interaction)
        await self._open_editor(interaction, spec)

    async def _back_to_categories(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self.category = None
        self.page = 0
        await self.rerender(interaction)

    async def _prev_page(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self.page = max(self.page - 1, 0)
        await self.rerender(interaction)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self.page += 1
        await self.rerender(interaction)

    async def _open_editor(self, interaction: discord.Interaction, spec: CardSpec) -> None:
        """Swap this message into the composer for ``spec``."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        store = card_store(self.bot)
        from rosemary.core.mentions import mention_store

        mentions = mention_store(self.bot)

        async def load_doc(guild_id: int):
            return await store.get_document(guild_id, spec.key)

        async def save_doc(guild_id: int, doc: dict[str, Any]) -> None:
            await store.save_document(guild_id, spec.key, doc)

        async def reset_doc(guild_id: int) -> None:
            await store.reset(guild_id, spec.key)
            await mentions.reset(guild_id, spec.key)

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
            exit_factory=lambda _category=self.category: CustomizeMenuView(
                self.bot,
                self.guild_id,
                owner_id=self.author_id,
                category=_category,
            ),
            load_mentions=load_mentions,
            save_mentions=save_mentions,
        )
        await editor.prepare()
        try:
            await interaction.edit(view=editor)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        self.stop()
