"""Card picker for /customize (and the /settings deep-link).

Lists every :class:`rosemary.core.cards.CardSpec` grouped by settings category
and opens the shared :class:`CardEditorView` for the chosen card. Persistence
is always the default ``CardStore``; features that keep overrides elsewhere
wire their own editor instance instead of going through this picker.
"""

from __future__ import annotations

import contextlib
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
            from rosemary.core.mentions import effective_pings

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
                    pings = await effective_pings(self.bot, self.guild_id, spec.key)
                    policy_label = await self._t(
                        "cards.editor.mentions.on" if pings else "cards.editor.mentions.off"
                    )
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
        log.debug(
            "custom_pick_card clicked (guild=%s, values=%s, message=%s)",
            self.guild_id,
            (interaction.data or {}).get("values"),
            getattr(getattr(interaction, "message", None), "id", None),
        )
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

    async def _placeholders_hint(self, spec: CardSpec) -> str:
        """Human-readable hint built strictly from the card's variable contract.

        ``{name}`` per contracted variable (resolved mentions render as
        ``{@name}`` so admins learn the canonical spelling). Cards with an
        empty contract get the theme-emoji note only — never a generic string
        listing placeholders the card does not actually receive.
        """
        from rosemary.core.variables import VARIABLES

        mention_kinds = {"mention"}
        parts = []
        for name in spec.variables:
            entry = VARIABLES.get(name)
            if entry is None:
                parts.append(f"{{{name}}}")
                continue
            spelling = f"{{@{name}}}" if entry.kind in mention_kinds else f"{{{name}}}"
            parts.append(spelling)
        if parts:
            return ", ".join(parts)
        return await self._t("cards.editor.placeholders_none")

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
            placeholders_hint=await self._placeholders_hint(spec),
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
        log.debug(
            "opening editor for card %s (guild=%s, message=%s)",
            spec.key,
            self.guild_id,
            getattr(getattr(interaction, "message", None), "id", None),
        )
        try:
            await interaction.edit(view=editor)
        except discord.NotFound:
            log.warning(
                "customize editor open failed: interaction/message gone "
                "(stale panel? card=%s guild=%s)",
                spec.key,
                self.guild_id,
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("customize editor open failed for card %s", spec.key)
            return
        self.stop()


# -- stale-message recovery ---------------------------------------------------

#: Picker component ids that must keep working after a bot restart. Ephemeral
#: messages survive the restart looking alive, but the live view is gone, so
#: py-cord silently no-ops the click. A persistent view registered at boot
#: catches them via the ``(component_type, None, custom_id)`` store fallback.
_PICKER_PERSISTENT_IDS = (
    "custom_pick_category",
    "custom_pick_card",
    "custom_back",
    "custom_close",
    "custom_prev",
    "custom_next",
)


class CustomizeMenuRecoveryView(MenuView):
    """Persistent fallback for stale (pre-restart) picker messages.

    Re-opens a fresh picker so pre-restart ephemeral panels dispatch again
    instead of doing nothing. Handlers bound here are the last-resort match —
    exact ``(type, message_id, custom_id)`` entries always win, so this never
    intercepts clicks on live menus.
    """

    def __init__(self, bot) -> None:
        # author_id None: anyone clicking a stale panel gets a working menu.
        super().__init__(author_id=None, timeout=None)
        self.bot = bot
        self.guild_id = None
        self._register_handlers()
        # ``add_view`` only indexes ids that exist as real view children, so a
        # dispatch-only view still carries minimal stub components. They are
        # never rendered: this view is registered bot-wide, never sent.
        for custom_id in _PICKER_PERSISTENT_IDS:
            if custom_id.startswith("custom_pick"):
                item = self.make_select(
                    custom_id=custom_id,
                    placeholder="stale",
                    options=[discord.SelectOption(label="stale", value="stale")],
                )
            else:
                item = self.make_button(custom_id=custom_id, label="stale")
            self.add_item(discord.ui.ActionRow(item))

    async def _t(self, key: str, **kwargs: Any) -> str:
        return await self.bot.translator.t(self.guild_id, key, **kwargs)

    async def _recover(self, interaction: discord.Interaction) -> None:
        """Replace the stale message with a fresh picker and re-own it."""
        guild_id = interaction.guild_id
        if guild_id is None or not interaction.response.is_done():
            # Never ACK-less: defer covers missing guild and double-ACK cases.
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.defer(ephemeral=True)
            if guild_id is None:
                return
        log.info(
            "stale customize panel recovered (guild=%s, custom_id=%s, message=%s)",
            guild_id,
            interaction.custom_id,
            getattr(getattr(interaction, "message", None), "id", None),
        )
        view = CustomizeMenuView(self.bot, guild_id, owner_id=interaction.user.id)
        try:
            await view.prepare()
            await interaction.edit(view=view)
            return
        except discord.NotFound:
            log.warning(
                "stale customize panel could not be edited (message gone, guild=%s)",
                guild_id,
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("stale customize panel recovery failed (guild=%s)", guild_id)
        # Edit failed (expired interaction token, deleted message): give the
        # user a fresh panel as a followup so the click still yields a menu.
        with contextlib.suppress(discord.HTTPException):
            await interaction.followup.send(view=view, ephemeral=True)

    def _register_handlers(self) -> None:
        for custom_id in _PICKER_PERSISTENT_IDS:
            self.register(custom_id, self._recover)


def register_recovery_view(bot) -> None:
    """Register the stale-picker fallback; call once at boot (on_ready)."""
    bot.add_view(CustomizeMenuRecoveryView(bot))
