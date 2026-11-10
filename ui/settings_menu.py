"""The interactive /settings menu, built on :class:`rosemary.ui.kit.Panel`.

All user-facing text comes from the language catalogs through ``t()``. Values
are edited with translated components (buttons for booleans, selects for
choices/channels, a modal for strings and integers), so the settings layer
never parses free text; it always receives canonical values.

The chrome (the header, the body, the footer and the navigation) is the
theme's: the menu owns only its content, so every panel of the bot looks
the same and restyling them all is one edit.
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import timedelta
from typing import Any

import discord

from rosemary.core.cards import safe_format
from rosemary.core.debug import SETTINGS_LOG_EVENT, send_log
from rosemary.core.settings import (
    CATEGORIES,
    SETTINGS,
    FormattedValue,
    SettingCategory,
    SettingSpec,
    SettingType,
    format_value,
    get_setting,
    reset_setting,
    set_setting,
    settings_for_category,
)
from rosemary.core.time_parser import TimeParser, localized_aliases
from rosemary.ui.kit import ActionRow, EmptyState, Panel, Section, TextDisplay, divider
from rosemary.ui.modals import make_text_modal

log = logging.getLogger(__name__)

ITEMS_PER_PAGE = 7
#: Results of the search screen, one select page. Discord's select cap is 25.
SEARCH_PER_PAGE = 25
#: Places shown per management page. Each row is a Section (text + button),
#: so the 40-component ceiling bounds this, not a flat count.
LIST_ITEMS_PER_PAGE = 10


def _fold(text: str) -> str:
    """A search key: lowercase and without accents (pt-BR "Moderação" == "moderacao")."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


class SettingsMenuView(Panel):
    """Browse and edit the guild's settings.

    State: ``category`` (which category is shown), ``page`` (page within the
    category), ``editing_key`` (a selectable setting awaiting a new value) and
    ``flash`` (a one-shot feedback line shown on the main page).
    """

    #: The navigation keeps the ids of the menu: a message posted before the
    #: migration must keep working.
    nav_ids = {
        "back": "settings_back",
        "previous": "settings_prev",
        "next": "settings_next",
        "close": "settings_close",
    }
    nav_labels = {
        "back": "settings.back",
        "previous": "settings.prev",
        "next": "settings.next",
        "close": "settings.close",
    }

    def __init__(
        self,
        bot,
        guild_id: int,
        *,
        owner_id: int | None = None,
    ) -> None:
        super().__init__(bot, guild_id, author_id=owner_id)
        self.category: SettingCategory = SettingCategory.GENERAL
        self.page = 0
        self.editing_key: str | None = None
        self.list_page = 0
        self.add_mode = False
        self._pending_adds: tuple[int, ...] = ()
        self.test_open = False
        #: The search state (the query and the result page) lives in the panel,
        #: never in the guild storage: closing the menu forgets it.
        self.search_open = False
        self.query = ""
        self.search_page = 0
        self._screen = "main"
        self._register_handlers()

    #: handler registration ----------------------------------------------

    def _register_handlers(self) -> None:
        self.register("settings_category", self._select_category)
        self.register("settings_search", self._open_search)
        self.register("settings_search_pick", self._pick_search_result)
        for key, spec in SETTINGS.items():
            self.register(f"settings_edit:{key}", self._edit)
            self.register(f"settings_reset:{key}", self._reset_setting)
            if spec.value_type is SettingType.BOOLEAN:
                self.register(f"settings_set_value:{key}:true", self._set_value_button)
                self.register(f"settings_set_value:{key}:false", self._set_value_button)
            elif spec.value_type in (
                SettingType.CHOICE,
                SettingType.CHANNEL,
                SettingType.CATEGORY,
                SettingType.ROLE,
            ):
                self.register(f"settings_set:{key}", self._set_value_select)
            elif spec.value_type is SettingType.CHANNEL_LIST:
                self.register(f"settings_list_add:{key}", self._list_add_mode)
                self.register(f"settings_list_pick:{key}", self._list_pick)
                self.register(f"settings_list_confirm:{key}", self._list_confirm)
                self.register(f"settings_list_cancel:{key}", self._list_cancel)
                self.register(f"settings_list_clear:{key}", self._list_clear)
                self.register(f"settings_list_prev:{key}", self._list_prev)
                self.register(f"settings_list_next:{key}", self._list_next)
                self.register("settings_list_remove", self._list_remove)
        self.register("settings_test", self._test_action)
        self.register("settings_test_open", self._open_tests)
        self.register("settings_colors_manage", self._open_colors)
        self.register("settings_roleplay_manage", self._open_roleplay)

    #: handlers ----------------------------------------------------------

    async def on_back(self, interaction: discord.Interaction) -> None:
        """Back one step: the edit, then the search, then the category list."""
        self.test_open = False
        self.add_mode = False
        self._pending_adds = ()
        self.list_page = 0
        if self.editing_key is not None:
            self.editing_key = None
            await self.rerender(interaction)
            return
        if self.search_open:
            self.search_open = False
            self.query = ""
            self.search_page = 0
            await self.rerender(interaction)
            return
        await self.rerender(interaction)

    async def on_previous(self, interaction: discord.Interaction) -> None:
        if self.search_open:
            if self.search_page > 0:
                self.search_page -= 1
            await self.rerender(interaction)
            return
        if self.page > 0:
            self.page -= 1
        await self.rerender(interaction)

    async def on_next(self, interaction: discord.Interaction) -> None:
        if self.search_open:
            pages = self._search_pages(len(await self._search_results()))
            if self.search_page < pages - 1:
                self.search_page += 1
            await self.rerender(interaction)
            return
        total_pages = _category_pages(self.category)
        if self.page < total_pages - 1:
            self.page += 1
        await self.rerender(interaction)

    async def _open_colors(self, interaction: discord.Interaction) -> None:
        """Swap into the color panel manager on the same message (anti-invite
        pattern: no second orphan message, Back returns here)."""
        from rosemary.ui.colors_menu import ColorsManagerView

        if not interaction.response.is_done():
            await interaction.response.defer()
        manager = ColorsManagerView(
            self.bot, self.guild_id, self.author_id or interaction.user.id
        )
        # V2 payloads must be built before the edit (prepare() assembles the
        # components); rerender edits the message in place.
        await manager.prepare()
        await interaction.edit(view=manager)
        self.stop()

    async def _open_roleplay(self, interaction: discord.Interaction) -> None:
        """Swap into the gif manager on the same message (color-manager pattern)."""
        from rosemary.ui.roleplay_menu import RoleplayManagerView

        if not interaction.response.is_done():
            await interaction.response.defer()
        manager = RoleplayManagerView(
            self.bot, self.guild_id, self.author_id or interaction.user.id
        )
        await manager.prepare()
        await interaction.edit(view=manager)
        self.stop()

    async def _select_category(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        try:
            self.category = SettingCategory(values[0])
        except ValueError:
            return
        self.page = 0
        self.editing_key = None
        self.test_open = False
        await self.rerender(interaction)

    async def _edit(self, interaction: discord.Interaction) -> None:
        key = interaction.custom_id.split(":", 1)[1]
        spec = SETTINGS[key]
        if spec.value_type in (SettingType.STRING, SettingType.INTEGER):
            await self._open_modal(interaction, spec)
        else:
            self.editing_key = key
            await self.rerender(interaction)

    #: search ------------------------------------------------------------

    async def _open_search(self, interaction: discord.Interaction) -> None:
        """Open the search modal (the results are a page of the same menu)."""
        modal = make_text_modal(
            title=await self.t("settings.search.title"),
            custom_id="settings_search_modal",
            label=await self.t("settings.search.label"),
            placeholder=await self.t("settings.search.placeholder"),
            max_length=60,
            on_submit=self._search_submit,
        )
        await interaction.response.send_modal(modal)

    async def _search_submit(
        self, interaction: discord.Interaction, value: str
    ) -> None:
        """Filter the registry by the term typed in the modal."""
        if not interaction.response.is_done():
            await interaction.response.defer()
        self.query = (value or "").strip()
        self.search_page = 0
        self.search_open = True
        self.editing_key = None
        await self.rerender(interaction)

    async def _search_results(self) -> list[str]:
        """The setting keys whose localized label carries the query.

        Accent- and case-insensitive, so "moderacao" finds "Moderação". The
        registry order is kept (the categories stay grouped).
        """
        needle = _fold(self.query)
        if not needle:
            return []
        results: list[str] = []
        for key in SETTINGS:
            label = await self.t(f"settings.{key}.label")
            if needle in _fold(label):
                results.append(key)
        return results

    async def _pick_search_result(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values or values[0] not in SETTINGS:
            return
        self.editing_key = values[0]
        self.list_page = 0
        await self.rerender(interaction)

    @staticmethod
    def _search_pages(total: int) -> int:
        return max(1, (total + SEARCH_PER_PAGE - 1) // SEARCH_PER_PAGE)

    async def _reset_setting(self, interaction: discord.Interaction) -> None:
        """Restore the default by removing the key (never writing ``spec.default``)."""
        key = interaction.custom_id.split(":", 1)[1]
        spec = SETTINGS[key]
        if not await self._ack(interaction):
            return
        old_value = await get_setting(self.bot.storage, self.guild_id, spec.key)
        await reset_setting(self.bot.storage, self.guild_id, spec.key)
        await self._log_change(interaction, spec, old_value)
        label = await self.t(f"settings.{spec.key}.label")
        self.flash = await self.t("settings.reset_done", setting=label)
        self.flash_color = "success"
        self.flash_icon = "reset"
        self.editing_key = None
        await self.rerender(interaction)

    async def _set_value_button(self, interaction: discord.Interaction) -> None:
        _, key, raw = interaction.custom_id.split(":", 2)
        await self._apply_value(interaction, key, raw == "true")

    async def _set_value_select(self, interaction: discord.Interaction) -> None:
        key = interaction.custom_id.split(":", 1)[1]
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        await self._apply_value(interaction, key, values[0])

    #: CHANNEL_LIST management screens ------------------------------------

    async def _list_value(self, key: str) -> tuple[int, ...]:
        return await get_setting(self.bot.storage, self.guild_id, key)

    async def _list_add_mode(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        self.add_mode = True
        self._pending_adds = ()
        await self.rerender(interaction)

    async def _list_pick(self, interaction: discord.Interaction) -> None:
        """Collect multi-select picks; Concluir applies them."""
        if not await self._ack(interaction):
            return
        values = (interaction.data or {}).get("values") or []
        picks: list[int] = []
        for raw in values:
            try:
                picks.append(int(raw))
            except (TypeError, ValueError):
                continue
        merged = list(self._pending_adds)
        merged.extend(p for p in picks if p not in merged)
        self._pending_adds = tuple(merged)
        await self.rerender(interaction)

    async def _list_confirm(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        key = interaction.custom_id.split(":", 1)[1]
        current = list(await self._list_value(key))
        for place in self._pending_adds:
            if place not in current:
                current.append(place)
        self.add_mode = False
        self._pending_adds = ()
        self.list_page = 0
        await self._apply_value(interaction, key, current, stay_in_edit=True)

    async def _list_cancel(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        self.add_mode = False
        self._pending_adds = ()
        await self.rerender(interaction)

    async def _list_clear(self, interaction: discord.Interaction) -> None:
        key = interaction.custom_id.split(":", 1)[1]
        self.list_page = 0
        await self._apply_value(interaction, key, (), stay_in_edit=True)

    async def _list_prev(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        if self.list_page > 0:
            self.list_page -= 1
        await self.rerender(interaction)

    async def _list_next(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        key = interaction.custom_id.split(":", 1)[1]
        total = len(await self._list_value(key))
        pages = max(1, -(-total // LIST_ITEMS_PER_PAGE))
        if self.list_page < pages - 1:
            self.list_page += 1
        await self.rerender(interaction)

    async def _list_remove(self, interaction: discord.Interaction) -> None:
        """Remove one place: ``settings_list_remove:{key}:{id}``."""
        _, key, raw = interaction.custom_id.split(":", 2)
        try:
            place = int(raw)
        except ValueError:
            return
        current = [p for p in await self._list_value(key) if p != place]
        # Removing the last row of a page must not strand an empty view.
        pages = max(1, -(-len(current) // LIST_ITEMS_PER_PAGE))
        if self.list_page >= pages:
            self.list_page = pages - 1
        await self._apply_value(interaction, key, current, stay_in_edit=True)

    async def _apply_value(
        self,
        interaction: discord.Interaction,
        key: str,
        raw: Any,
        *,
        stay_in_edit: bool = False,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        t = self.bot.translator.t
        spec = SETTINGS[key]
        label = await t(self.guild_id, f"settings.{spec.key}.label")
        old_value = await get_setting(self.bot.storage, self.guild_id, spec.key)
        try:
            aliases = (
                await localized_aliases(self.bot.translator, self.guild_id)
                if spec.is_duration
                else None
            )
            await set_setting(self.bot.storage, self.guild_id, spec.key, raw, aliases=aliases)
            self.flash = await t(self.guild_id, "settings.saved", setting=label)
            self.flash_color = "success"
        except ValueError:
            self.flash = await t(self.guild_id, "settings.invalid_value", setting=label)
            self.flash_color = "danger"
            self.flash_icon = "error"
            self.editing_key = None
            await self.rerender(interaction)
            return
        await self._log_change(interaction, spec, old_value)
        if not stay_in_edit:
            self.editing_key = None
        await self.rerender(interaction)

    async def _log_change(
        self,
        interaction: discord.Interaction,
        spec: SettingSpec,
        old_value: Any,
    ) -> None:
        """Log the new value and notify the features that react to it.

        Shared by a save and by a reset: both paths change an effective value,
        so both write the same staff log card and run the same hooks.
        """
        t = self.bot.translator.t
        label = await t(self.guild_id, f"settings.{spec.key}.label")
        category_label = await t(
            self.guild_id,
            f"settings.category.{spec.category.value}",
        )
        new_value = await get_setting(self.bot.storage, self.guild_id, spec.key)
        old_fv = format_value(spec, old_value)
        new_fv = format_value(spec, new_value)
        old_fmt = await t(self.guild_id, old_fv.display) if old_fv.translate else old_fv.display
        new_fmt = await t(self.guild_id, new_fv.display) if new_fv.translate else new_fv.display
        await send_log(
            self.bot,
            self.guild_id,
            SETTINGS_LOG_EVENT,
            setting=label,
            category=category_label,
            old=old_fmt,
            new=new_fmt,
            author=interaction.user.mention,
        )
        from rosemary.core.panels import on_setting_changed

        await on_setting_changed(self.bot, self.guild_id, spec.key)
        # First-time setup hook: enabling colors (or pointing its channel at
        # a fresh guild) must seed entries + roles and post the panel right
        # away, not only on the next boot or manager open.
        if spec.key in ("colors.enabled", "colors.panel_channel"):
            colors_cog = self.bot.get_cog("ColorsCog")
            if colors_cog is not None:
                await colors_cog.on_first_setup(self.guild_id)
        # The trap notice card lives in the trap channel: enabling the trap
        # or re-pointing its channel must (re)post the card immediately.
        if spec.key in ("trap.enabled", "trap.channel", "trap.action"):
            trap_cog = self.bot.get_cog("TrapCog")
            if trap_cog is not None:
                await trap_cog.repaint_notice(self.bot, self.guild_id)

    async def _setting_value(self, spec: SettingSpec) -> tuple[Any, bool]:
        """``(effective value, stored?)`` with catalog-localized defaults.

        A setting that was never saved still has a user-facing default. Message
        templates carry an English literal in ``spec.default`` for persistence,
        so the UI resolves ``default_catalog_key`` instead (pt-BR guilds must
        not see the English fallback in the menu or edit modal).
        """
        data = await self.bot.storage.get(self.guild_id)
        stored = spec.key in data
        value = await get_setting(self.bot.storage, self.guild_id, spec.key)
        if not stored and spec.default_catalog_key:
            key = spec.default_catalog_key
            localized = await self.bot.translator.raw(self.guild_id, key)
            if isinstance(localized, str) and localized != key:
                # Defaults are message templates: resolve theme emojis but keep
                # unknown placeholders such as ``{ping_role}`` visible. Going
                # through ``t()`` would abort the whole format on the first
                # unknown placeholder and leak ``{unlock}`` into the field.
                emojis = self.bot.theme.emojis if self.bot.theme else {}
                value = safe_format(localized, emojis)
        return value, stored

    async def _open_modal(
        self, interaction: discord.Interaction, spec: SettingSpec
    ) -> None:
        t = self.bot.translator.t
        current, _stored = await self._setting_value(spec)
        if spec.is_duration and current is not None:
            field_value = TimeParser.format_duration(timedelta(seconds=int(current)))
        else:
            field_value = str(current) if current is not None else ""
        label = await t(self.guild_id, f"settings.{spec.key}.label")
        description = await t(self.guild_id, f"settings.{spec.key}.description")
        # Discord caps the modal placeholder at 100 characters; truncate so a
        # long catalog description never crashes the modal.
        placeholder = description if len(description) <= 100 else f"{description[:97]}..."
        title = await t(self.guild_id, "settings.edit_title", setting=label)

        # Multiline settings (message templates) edit through a paragraph
        # field: real newlines and up to Discord's message-length cap instead
        # of the 100-char single-line default.
        multiline = bool(getattr(spec, "multiline", False))
        field = discord.ui.InputText(
            placeholder=placeholder,
            value=field_value,
            required=True,
            custom_id=f"{spec.key}_value",
            min_length=1,
            max_length=2000 if multiline else 100,
            style=discord.InputTextStyle.paragraph if multiline else discord.InputTextStyle.short,
        )
        modal = discord.ui.DesignerModal(title=title, custom_id=f"settings_modal:{spec.key}")
        modal.add_item(discord.ui.Label(label, item=field))
        modal.callback = self._make_modal_callback(spec, field)
        await interaction.response.send_modal(modal)

    def _make_modal_callback(
        self, spec: SettingSpec, field: discord.ui.InputText
    ) -> Any:
        async def submit(interaction: discord.Interaction) -> None:
            await self._apply_value(interaction, spec.key, field.value)

        return submit

    #: test actions ------------------------------------------------------

    async def _test_action(self, interaction: discord.Interaction) -> None:
        """Run a Bump test action from the settings BUMP category."""
        # ACK the component interaction immediately: the actions below perform
        # slow external IO (channel sends, card rendering). Discord only accepts
        # the initial response within 3s or the token is invalidated with
        # ``Unknown interaction`` (10062), which breaks the rerender flash.
        await interaction.response.defer()
        action = interaction.custom_id.split(":", 1)[1]
        reminder_cog = self.bot.get_cog("BumpReminderCog")
        leaderboard_cog = self.bot.get_cog("BumpLeaderboardCog")

        if action == "thank_you":
            if reminder_cog is None:
                return await self._test_flash(interaction, "error")
            await reminder_cog.send_thank_you(self.guild_id, interaction.user.id)
            await self._test_flash(interaction, "thank_you_done")
        elif action == "reminder":
            if reminder_cog is None:
                return await self._test_flash(interaction, "error")
            await reminder_cog.send_reminder(self.guild_id)
            await self._test_flash(interaction, "reminder_done")
        elif action == "lock":
            if reminder_cog is None:
                return await self._test_flash(interaction, "error")
            await reminder_cog.lock_channel(self.guild_id)
            await self._test_flash(interaction, "lock_done")
        elif action == "unlock":
            if reminder_cog is None:
                return await self._test_flash(interaction, "error")
            await reminder_cog.unlock_channel(self.guild_id)
            await self._test_flash(interaction, "unlock_done")
        elif action == "leaderboard":
            if leaderboard_cog is None:
                return await self._test_flash(interaction, "error")
            posted = await leaderboard_cog.post_current_leaderboard(self.guild_id)
            await self._test_flash(interaction, "leaderboard_done" if posted else "no_data")
        elif action == "reset":
            if leaderboard_cog is None:
                return await self._test_flash(interaction, "error")
            await leaderboard_cog.bump_store.reset_week(
                self.guild_id, reason="settings_reset", actor_id=interaction.user.id
            )
            log.warning(
                "Bump week reset forced in %s by %s (source=settings_reset)",
                self.guild_id,
                interaction.user.id,
            )
            await send_log(
                self.bot,
                self.guild_id,
                "bump.logs.week_reset_manual",
                author=interaction.user.mention,
            )
            await self._test_flash(interaction, "reset_done")

    async def _open_tests(self, interaction: discord.Interaction) -> None:
        """Open the dedicated test-actions sub-screen (keeps pages under 40)."""
        await interaction.response.defer()
        self.test_open = True
        await self.rerender(interaction)

    async def _test_flash(self, interaction: discord.Interaction, key: str) -> None:
        self.flash = await self.bot.translator.t(
            self.guild_id, f"settings.test.{key}"
        )
        self.flash_color = "danger" if key == "error" else "success"
        self.flash_icon = {
            "error": "error",
            "no_data": "warning",
            "thank_you_done": "speech",
            "reminder_done": "clock",
            "lock_done": "lock",
            "unlock_done": "unlock",
            "leaderboard_done": "chart",
            "reset_done": "reset",
        }.get(key)
        await self.rerender(interaction)

    #: rendering ---------------------------------------------------------

    async def build_items(self) -> list[discord.ui.ViewItem]:
        """The items of the current screen (the state picks the screen)."""
        if self.editing_key is not None:
            spec = SETTINGS[self.editing_key]
            if spec.value_type is SettingType.CHANNEL_LIST:
                self._screen = "list_add" if self.add_mode else "list"
                return await (
                    self._list_add_screen(spec)
                    if self.add_mode
                    else self._list_screen(spec)
                )
            self._screen = "edit"
            return await self._edit_screen(spec)
        if self.test_open and self.category is SettingCategory.BUMP:
            self._screen = "test"
            return await self._test_screen()
        if self.search_open:
            self._screen = "search"
            return await self._search_screen()
        self._screen = "main"
        return await self._main_screen()

    async def header_lines(self) -> list[discord.ui.ViewItem]:
        """The extra lines inside the frame (the page indicator, the hints)."""
        return list(self.lines)

    async def _main_screen(self) -> list[discord.ui.ViewItem]:
        category_label = await self.t(
            f"settings.category.{self.category.value}"
        )
        category_word = await self.t("settings.category_label")
        category_icon = self.emoji(f"category_{self.category.value}") or ""
        category_count = await self.t(
            "settings.category_count",
            count=len(settings_for_category(self.category)),
        )
        total_pages = _category_pages(self.category)
        self.heading_key = "settings.title"
        self.title = await self.t("settings.title")
        self.body = await self.t(
            "settings.category_summary",
            category_label=category_word,
            icon=category_icon,
            category=category_label,
            count=category_count,
        )
        self.nav_actions = ("close",)
        if self.page > 0:
            self.nav_actions = ("close", "previous")
        if self.page < total_pages - 1:
            self.nav_actions = (*self.nav_actions, "next")
        self.nav_extra = []
        self.lines = []
        if total_pages > 1:
            self.lines.append(
                TextDisplay(
                    await self.t(
                        "settings.page_indicator",
                        page=self.page + 1,
                        pages=total_pages,
                    )
                )
            )

        sections: list[discord.ui.ViewItem] = [divider()]
        settings = settings_for_category(self.category)
        start = self.page * ITEMS_PER_PAGE
        for spec in settings[start : start + ITEMS_PER_PAGE]:
            label = await self.t(f"settings.{spec.key}.label")
            type_icon = self.emoji(f"type_{spec.value_type.value}") or ""
            value, stored = await self._setting_value(spec)
            formatted = format_value(spec, value)
            if not stored and spec.default_catalog_key:
                # The literal default is English; the catalog default is the
                # user-facing text. Keep the "default" badge even though the
                # localized string differs from ``spec.default``.
                formatted = FormattedValue(
                    formatted.display,
                    formatted.translate,
                    is_default=True,
                    preview=formatted.preview,
                )
            value_text = (
                await self.t(formatted.display)
                if formatted.translate
                else formatted.display
            )
            if formatted.preview:
                value_text = await self.t(
                    "settings.preview_value", value=value_text
                )
            if formatted.is_default:
                value_text = await self.t(
                    "settings.default_value", value=value_text
                )
            if spec.value_type is SettingType.BOOLEAN:
                # One click flips it: the row's accessory is the state itself,
                # not an "Edit" button leading to a second screen.
                current = bool(value)
                accessory = await self.button(
                    f"settings_set_value:{spec.key}:{'false' if current else 'true'}",
                    "settings.bool.true" if current else "settings.bool.false",
                    emoji="success" if current else "error",
                    style="success" if current else "danger",
                )
            else:
                accessory = await self.button(
                    f"settings_edit:{spec.key}",
                    "settings.edit",
                    emoji="gear",
                )
            sections.append(
                Section(
                    self.entry(f"{type_icon} {label}" if type_icon else label, value_text),
                    accessory=accessory,
                )
            )

        actions: list[discord.ui.ViewItem] = []
        if self.category is SettingCategory.COLORS:
            self.nav_extra.append(
                await self.button(
                    "settings_colors_manage",
                    "settings.colors.manage",
                    emoji="palette",
                    style="primary",
                )
            )
        if self.category is SettingCategory.ROLEPLAY:
            self.nav_extra.append(
                await self.button(
                    "settings_roleplay_manage",
                    "settings.roleplay.manage",
                    emoji="people",
                    style="primary",
                )
            )
        if self.category is SettingCategory.BUMP:
            actions.append(
                ActionRow(
                    await self.button(
                        "settings_test_open",
                        "settings.test.open",
                        style="primary",
                        emoji="rocket",
                    )
                )
            )
        options: list[discord.SelectOption] = []
        for category in CATEGORIES:
            count = len(settings_for_category(category))
            hint = await self.t(f"settings.category_hint.{category.value}")
            count_text = await self.t("settings.category_count", count=count)
            options.append(
                discord.SelectOption(
                    label=await self.t(f"settings.category.{category.value}"),
                    value=category.value,
                    description=f"{hint} · {count_text}"[:100],
                    emoji=self.emoji(f"category_{category.value}") or None,
                    default=category is self.category,
                )
            )
        category_select = await self.select(
            "settings_category",
            "settings.category.placeholder",
            options,
        )
        actions.extend(
            [
                divider("large"),
                ActionRow(category_select),
                ActionRow(
                    await self.button(
                        "settings_search",
                        "settings.search.button",
                        emoji="search",
                        style="primary",
                    )
                ),
            ]
        )
        return await self.compose(sections, actions)

    async def _search_screen(self) -> list[discord.ui.ViewItem]:
        """The results of one search, one select page at a time."""
        results = await self._search_results()
        total_pages = self._search_pages(len(results))
        self.search_page = min(self.search_page, total_pages - 1)
        self.heading_key = ""
        self.title = await self.t("settings.search.title")
        self.body = await self.t(
            "settings.search.results",
            query=self.query,
            count=len(results),
        )
        self.nav_actions = ("back",)
        if self.search_page > 0:
            self.nav_actions = ("back", "previous")
        if self.search_page < total_pages - 1:
            self.nav_actions = (*self.nav_actions, "next")
        self.nav_extra = []
        self.lines = []
        if total_pages > 1:
            self.lines.append(
                TextDisplay(
                    await self.t(
                        "settings.page_indicator",
                        page=self.search_page + 1,
                        pages=total_pages,
                    )
                )
            )
        sections: list[discord.ui.ViewItem] = []
        if not results:
            sections.append(
                EmptyState(
                    await self.t("settings.search.empty", query=self.query),
                    icon=self.emoji("search"),
                ).item()
            )
            return await self.compose(sections, [])
        start = self.search_page * SEARCH_PER_PAGE
        options: list[discord.SelectOption] = []
        for key in results[start : start + SEARCH_PER_PAGE]:
            spec = SETTINGS[key]
            options.append(
                discord.SelectOption(
                    label=await self.t(f"settings.{key}.label"),
                    value=key,
                    description=await self.t(
                        f"settings.category.{spec.category.value}"
                    ),
                    emoji=self.emoji(f"type_{spec.value_type.value}") or None,
                )
            )
        select = self.make_select(
            custom_id="settings_search_pick",
            placeholder=await self.t("settings.search.pick"),
            options=options,
        )
        return await self.compose(sections, [ActionRow(select)])

    async def _test_screen(self) -> list[discord.ui.ViewItem]:
        self.heading_key = ""
        self.title = await self.t("settings.test.title")
        self.body = await self.t("settings.test.hint")
        self.nav_actions = ("back",)
        self.nav_extra = []
        actions = [
            ActionRow(
                await self.button(
                    "settings_test:thank_you",
                    "settings.test.thank_you",
                    style="success",
                    emoji="rocket",
                ),
                await self.button(
                    "settings_test:reminder",
                    "settings.test.reminder",
                    emoji="clock",
                ),
                await self.button(
                    "settings_test:leaderboard",
                    "settings.test.leaderboard",
                    emoji="trophy",
                ),
            ),
            ActionRow(
                await self.button(
                    "settings_test:lock",
                    "settings.test.lock",
                    emoji="lock",
                ),
                await self.button(
                    "settings_test:unlock",
                    "settings.test.unlock",
                    emoji="unlock",
                ),
                await self.button(
                    "settings_test:reset",
                    "settings.test.reset",
                    style="danger",
                    emoji="refresh",
                ),
            ),
        ]
        return await self.compose([], actions)

    async def _list_screen(self, spec: SettingSpec) -> list[discord.ui.ViewItem]:
        """Manage one CHANNEL_LIST setting: rows with per-row Remove."""
        label = await self.t(f"settings.{spec.key}.label")
        places = list(await self._list_value(spec.key))
        self.heading_key = ""
        self.title = label
        self.body = await self.t(f"settings.{spec.key}.description")
        self.nav_actions = ("back",)
        self.nav_extra = []
        total_pages = 1
        page_places: list[int] = []
        sections: list[discord.ui.ViewItem] = [divider()]
        self.lines = []
        if not places:
            sections.append(
                EmptyState(
                    await self.t("settings.list.empty"),
                    icon=self.emoji("folder"),
                ).item()
            )
        else:
            total_pages = max(1, -(-len(places) // LIST_ITEMS_PER_PAGE))
            self.list_page = min(self.list_page, total_pages - 1)
            start = self.list_page * LIST_ITEMS_PER_PAGE
            page_places = places[start : start + LIST_ITEMS_PER_PAGE]
            if total_pages > 1:
                self.lines.append(
                    TextDisplay(
                        await self.t(
                            "settings.page_indicator",
                            page=self.list_page + 1,
                            pages=total_pages,
                        )
                    )
                )
        for place in page_places:
            sections.append(
                Section(
                    TextDisplay(f"<#{place}>"),
                    accessory=await self.button(
                        f"settings_list_remove:{spec.key}:{place}",
                        "settings.list.remove",
                        emoji="trash",
                        style="danger",
                    ),
                )
            )
        actions = [divider()]
        row = [
            await self.button(
                f"settings_list_add:{spec.key}",
                "settings.list.add",
                emoji="plus",
                style="success",
            )
        ]
        if places:
            row.append(
                await self.button(
                    f"settings_list_clear:{spec.key}",
                    "settings.list.clear",
                    emoji="trash",
                    style="danger",
                )
            )
        actions.append(ActionRow(*row))
        if self.list_page > 0:
            self.nav_actions = ("back", "previous")
        if self.list_page < total_pages - 1:
            self.nav_actions = (*self.nav_actions, "next")
        return await self.compose(sections, actions)

    async def _list_add_screen(self, spec: SettingSpec) -> list[discord.ui.ViewItem]:
        """Batch-add screen: one multi-select (channels + categories)."""
        label = await self.t(f"settings.{spec.key}.label")
        pending = self._pending_adds
        self.heading_key = ""
        self.title = label
        self.body = await self.t("settings.list.add_hint")
        self.nav_actions = ()
        self.nav_extra = []
        sections: list[discord.ui.ViewItem] = []
        if pending:
            shown = " ".join(f"<#{p}>" for p in pending[:25])
            sections.append(
                TextDisplay(
                    await self.t(
                        "settings.list.pending", count=len(pending), places=shown
                    )
                )
            )
        # One picker handles both shapes: channel_types accepts text channels
        # AND categories together (Discord renders categories in the list).
        select = discord.ui.Select(
            select_type=discord.ComponentType.channel_select,
            custom_id=f"settings_list_pick:{spec.key}",
            placeholder=await self.t("settings.list.pick_placeholder"),
            min_values=1,
            max_values=25,
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.category,
            ],
        )
        select.callback = self._handlers[f"settings_list_pick:{spec.key}"]
        actions = [
            ActionRow(select),
            ActionRow(
                await self.button(
                    f"settings_list_confirm:{spec.key}",
                    "settings.list.confirm",
                    emoji="check",
                    style="success",
                    disabled=not pending,
                ),
                await self.button(
                    f"settings_list_cancel:{spec.key}",
                    "settings.cancel",
                    emoji="back",
                ),
            ),
        ]
        return await self.compose(sections, actions)

    async def _edit_screen(self, spec: SettingSpec) -> list[discord.ui.ViewItem]:
        label = await self.t(f"settings.{spec.key}.label")
        self.heading_key = ""
        self.title = label
        self.body = await self.t(f"settings.{spec.key}.description")
        self.nav_actions = ("back",)
        self.nav_extra = []
        self.lines = [TextDisplay(await self.t("settings.select_value"))]
        actions: list[discord.ui.ViewItem] = []

        if spec.value_type is SettingType.BOOLEAN:
            current = await get_setting(self.bot.storage, self.guild_id, spec.key)
            actions.append(
                ActionRow(
                    await self.button(
                        f"settings_set_value:{spec.key}:true",
                        "settings.bool.true",
                        style="success",
                        emoji="success",
                        disabled=current is True,
                    ),
                    await self.button(
                        f"settings_set_value:{spec.key}:false",
                        "settings.bool.false",
                        style="danger",
                        emoji="error",
                        disabled=current is False,
                    ),
                )
            )
        elif spec.value_type is SettingType.CHOICE:
            select = await self.select(
                f"settings_set:{spec.key}",
                f"settings.{spec.key}.label",
                [
                    discord.SelectOption(
                        label=await self.t(
                            f"settings.{spec.key}.choices.{value}"
                        ),
                        value=value,
                    )
                    for value in spec.choices
                ],
            )
            actions.append(ActionRow(select))
        elif spec.value_type in (SettingType.CHANNEL, SettingType.CATEGORY):
            # CATEGORY filters the native picker to guild categories only;
            # without the filter the client lists text/voice channels first
            # and categories are effectively unpickable.
            select = discord.ui.Select(
                select_type=discord.ComponentType.channel_select,
                custom_id=f"settings_set:{spec.key}",
                placeholder=label,
                min_values=1,
                max_values=1,
                channel_types=[discord.ChannelType.category]
                if spec.value_type is SettingType.CATEGORY
                else None,
            )
            select.callback = self._handlers[f"settings_set:{spec.key}"]
            actions.append(ActionRow(select))
        elif spec.value_type is SettingType.ROLE:
            select = discord.ui.Select(
                select_type=discord.ComponentType.role_select,
                custom_id=f"settings_set:{spec.key}",
                placeholder=label,
                min_values=1,
                max_values=1,
            )
            select.callback = self._handlers[f"settings_set:{spec.key}"]
            actions.append(ActionRow(select))

        _value, stored = await self._setting_value(spec)
        if stored:
            actions.append(
                ActionRow(
                    await self.button(
                        f"settings_reset:{spec.key}",
                        "settings.reset",
                        emoji="reset",
                        style="warning",
                    )
                )
            )

        return await self.compose([], actions)


def _category_pages(category: SettingCategory) -> int:
    total = len(settings_for_category(category))
    return max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
