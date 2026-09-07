"""The interactive /settings menu, built on :class:`rosemary.ui.menu.MenuView`.

All user-facing text comes from the language catalogs through ``t()``. Values
are edited with translated components (buttons for booleans, selects for
choices/channels, a modal for strings and integers), so the settings layer
never parses free text; it always receives canonical values.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import discord

from rosemary.core.cards import cards_for_category
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import (
    CATEGORIES,
    SETTINGS,
    SettingCategory,
    SettingSpec,
    SettingType,
    format_value,
    get_setting,
    set_setting,
    settings_for_category,
)
from rosemary.core.time_parser import TimeParser, localized_aliases
from rosemary.ui.containers import (
    Section,
    TextDisplay,
    designer_container,
    divider,
)
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)

ITEMS_PER_PAGE = 7


class SettingsMenuView(MenuView):
    """Browse and edit the guild's settings.

    State: ``category`` (which category is shown), ``page`` (page within the
    category), ``editing_key`` (a selectable setting awaiting a new value) and
    ``flash`` (a one-shot feedback line shown on the main page).
    """

    def __init__(
        self,
        bot,
        guild_id: int,
        *,
        owner_id: int | None = None,
    ) -> None:
        super().__init__(author_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.category: SettingCategory = SettingCategory.GENERAL
        self.page = 0
        self.editing_key: str | None = None
        self.test_open = False
        self.flash: str | None = None
        self.flash_color: str | None = None
        self._register_handlers()

    # -- handler registration ----------------------------------------------

    def _register_handlers(self) -> None:
        self.register("settings_close", self._close)
        self.register("settings_back", self._back)
        self.register("settings_prev", self._prev)
        self.register("settings_next", self._next)
        self.register("settings_category", self._select_category)
        for key, spec in SETTINGS.items():
            self.register(f"settings_edit:{key}", self._edit)
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
        self.register("settings_test", self._test_action)
        self.register("settings_test_open", self._open_tests)
        self.register("settings_test_close", self._close_tests)
        self.register("settings_customize", self._open_customize)

    # -- handlers ----------------------------------------------------------

    async def _close(self, interaction: discord.Interaction) -> None:
        self.disable_all_items()
        await interaction.edit(view=self)
        self.stop()

    async def _back(self, interaction: discord.Interaction) -> None:
        self.editing_key = None
        self.test_open = False
        await self.rerender(interaction)

    async def _prev(self, interaction: discord.Interaction) -> None:
        if self.page > 0:
            self.page -= 1
        await self.rerender(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        total_pages = _category_pages(self.category)
        if self.page < total_pages - 1:
            self.page += 1
        await self.rerender(interaction)

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

    async def _set_value_button(self, interaction: discord.Interaction) -> None:
        _, key, raw = interaction.custom_id.split(":", 2)
        await self._apply_value(interaction, key, raw == "true")

    async def _set_value_select(self, interaction: discord.Interaction) -> None:
        key = interaction.custom_id.split(":", 1)[1]
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        await self._apply_value(interaction, key, values[0])

    async def _apply_value(self, interaction: discord.Interaction, key: str, raw: Any) -> None:
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
            self.editing_key = None
            await self.rerender(interaction)
            return
        category_label = await t(
            self.guild_id,
            f"settings.category.{spec.category.value}",
        )
        new_value = await get_setting(self.bot.storage, self.guild_id, spec.key)
        old_fv = format_value(spec, old_value)
        new_fv = format_value(spec, new_value)
        old_fmt = await t(self.guild_id, old_fv.display) if old_fv.translate else old_fv.display
        new_fmt = await t(self.guild_id, new_fv.display) if new_fv.translate else new_fv.display
        await send_channel_log(
            self.bot,
            self.guild_id,
            await t(self.guild_id, "settings.log.title"),
            await t(
                self.guild_id,
                "settings.log.description",
                setting=label,
                category=category_label,
                old=old_fmt,
                new=new_fmt,
                author=interaction.user.mention,
            ),
        )
        self.editing_key = None
        await self.rerender(interaction)

    async def _open_modal(
        self, interaction: discord.Interaction, spec: SettingSpec
    ) -> None:
        t = self.bot.translator.t
        current = await get_setting(self.bot.storage, self.guild_id, spec.key)
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

        field = discord.ui.InputText(
            placeholder=placeholder,
            value=field_value,
            required=True,
            custom_id=f"{spec.key}_value",
            min_length=1,
            max_length=100,
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

    # -- test actions ------------------------------------------------------

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
            await self._test_flash(interaction, "thank_you")
        elif action == "reminder":
            if reminder_cog is None:
                return await self._test_flash(interaction, "error")
            await reminder_cog.send_reminder(self.guild_id)
            await self._test_flash(interaction, "reminder")
        elif action == "lock":
            if reminder_cog is None:
                return await self._test_flash(interaction, "error")
            await reminder_cog.lock_channel(self.guild_id)
            await self._test_flash(interaction, "lock")
        elif action == "unlock":
            if reminder_cog is None:
                return await self._test_flash(interaction, "error")
            await reminder_cog.unlock_channel(self.guild_id)
            await self._test_flash(interaction, "unlock")
        elif action == "leaderboard":
            if leaderboard_cog is None:
                return await self._test_flash(interaction, "error")
            posted = await leaderboard_cog.post_current_leaderboard(self.guild_id)
            await self._test_flash(interaction, "leaderboard" if posted else "no_data")
        elif action == "reset":
            if leaderboard_cog is None:
                return await self._test_flash(interaction, "error")
            await leaderboard_cog.bump_store.reset_week(self.guild_id)
            await send_channel_log(
                self.bot,
                self.guild_id,
                await self.bot.translator.t(self.guild_id, "bump.logs.week_reset_manual.title"),
                await self.bot.translator.t(
                    self.guild_id,
                    "bump.logs.week_reset_manual.description",
                    author=interaction.user.mention,
                ),
                color="warning",
                card_key="bump.logs.week_reset_manual.description",
                mention_user_ids=[interaction.user.id],
            )
            await self._test_flash(interaction, "reset")

    async def _open_customize(self, interaction: discord.Interaction) -> None:
        """Open the message composer rooted at the current category."""
        await interaction.response.defer()
        from rosemary.ui.customize_menu import CustomizeMenuView

        picker = CustomizeMenuView(
            self.bot,
            self.guild_id,
            owner_id=self.author_id,
            category=self.category.value,
        )
        await picker.prepare()
        self.stop()
        await interaction.edit(view=picker)

    async def _open_tests(self, interaction: discord.Interaction) -> None:
        """Open the dedicated test-actions sub-screen (keeps pages under 40)."""
        await interaction.response.defer()
        self.test_open = True
        await self.rerender(interaction)

    async def _close_tests(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.test_open = False
        await self.rerender(interaction)

    async def _test_flash(self, interaction: discord.Interaction, key: str) -> None:
        self.flash = await self.bot.translator.t(
            self.guild_id, f"settings.test.{key}"
        )
        self.flash_color = "success" if key != "error" else "danger"
        await self.rerender(interaction)

    # -- rendering ---------------------------------------------------------

    async def build_items(self) -> list[discord.ui.ViewItem]:
        t = self.bot.translator.t
        if self.editing_key is not None:
            return await self._build_edit_page(t)
        if self.test_open and self.category is SettingCategory.BUMP:
            return await self._build_test_screen(t)
        return await self._build_main_page(t)

    async def _build_main_page(
        self, t: Any
    ) -> list[discord.ui.ViewItem]:
        theme = self.bot.theme
        title = await t(self.guild_id, "settings.title")
        category_label = await t(
            self.guild_id, f"settings.category.{self.category.value}"
        )
        category_word = await t(self.guild_id, "settings.category_label")
        total_pages = _category_pages(self.category)
        container_parts = [
            TextDisplay(theme.md("title", title=title)),
            TextDisplay(
                theme.md(
                    "category",
                    category_label=category_word,
                    category=category_label,
                )
            ),
        ]
        if total_pages > 1:
            container_parts.append(
                TextDisplay(
                    await t(
                        self.guild_id,
                        "settings.page_indicator",
                        page=self.page + 1,
                        pages=total_pages,
                    )
                )
            )
        if self.flash:
            container_parts.append(TextDisplay(self.flash))
            self.flash = None
            flash_color = self.flash_color or "brand"
            self.flash_color = None
        else:
            flash_color = "brand"
        container = designer_container(theme.color(flash_color), *container_parts)

        settings = settings_for_category(self.category)
        start = self.page * ITEMS_PER_PAGE
        page_settings = settings[start : start + ITEMS_PER_PAGE]

        sections = []
        for spec in page_settings:
            label = await t(self.guild_id, f"settings.{spec.key}.label")
            formatted = format_value(
                spec, await get_setting(self.bot.storage, self.guild_id, spec.key)
            )
            value_text = (
                await t(self.guild_id, formatted.display)
                if formatted.translate
                else formatted.display
            )
            if formatted.is_default:
                value_text = await t(
                    self.guild_id, "settings.default_value", value=value_text
                )
            section = Section(
                TextDisplay(theme.md("entry", label=label, value=value_text)),
                accessory=self.make_button(
                    custom_id=f"settings_edit:{spec.key}",
                    label=await t(self.guild_id, "settings.edit"),
                    emoji=theme.emojis.get("gear", ""),
                ),
            )
            sections.append(section)

        category_select = self.make_select(
            custom_id="settings_category",
            placeholder=await t(self.guild_id, "settings.category.placeholder"),
            options=[
                discord.SelectOption(
                    label=await t(self.guild_id, f"settings.category.{c.value}"),
                    value=c.value,
                )
                for c in CATEGORIES
            ],
        )

        # Close stays first on every page so it never jumps around.
        nav = [
            self.make_button(
                custom_id="settings_close",
                label=await t(self.guild_id, "settings.close"),
                style=discord.ButtonStyle.danger,
            )
        ]
        if self.page > 0:
            nav.append(
                self.make_button(
                    custom_id="settings_prev",
                    label=await t(self.guild_id, "settings.prev"),
                ),
            )
        if self.page < total_pages - 1:
            nav.append(
                self.make_button(
                    custom_id="settings_next",
                    label=await t(self.guild_id, "settings.next"),
                )
            )

        items: list[discord.ui.ViewItem] = [
            container,
            divider(),
            *sections,
        ]
        if cards_for_category(self.category.value):
            theme_emojis = self.bot.theme.emojis
            customize_row = discord.ui.ActionRow(
                self.make_button(
                    custom_id="settings_customize",
                    label=await t(self.guild_id, "cards.customize.open"),
                    style=discord.ButtonStyle.primary,
                    emoji=theme_emojis.get("palette", ""),
                )
            )
            items.append(customize_row)
        if self.category is SettingCategory.BUMP:
            items.append(await self._open_tests_button(t))
        items.extend(
            [
                divider("large"),
                discord.ui.ActionRow(category_select),
                discord.ui.ActionRow(*nav),
            ]
        )
        return items

    async def _open_tests_button(self, t: Any) -> discord.ui.ViewItem:
        """Single entry button replacing the heavy inline test section."""
        theme = self.bot.theme
        return discord.ui.ActionRow(
            self.make_button(
                custom_id="settings_test_open",
                label=await t(self.guild_id, "settings.test.open"),
                style=discord.ButtonStyle.primary,
                emoji=theme.emojis.get("rocket", ""),
            )
        )

    async def _build_test_screen(
        self, t: Any
    ) -> list[discord.ui.ViewItem]:
        theme = self.bot.theme
        title = await t(self.guild_id, "settings.test.title")
        hint = await t(self.guild_id, "settings.test.hint")
        container = designer_container(
            theme.color(theme.style("bump_test").color),
            TextDisplay(theme.md("section", title=title)),
            TextDisplay(hint),
        )
        return [
            container,
            divider(),
            discord.ui.ActionRow(
                self.make_button(
                    custom_id="settings_test:thank_you",
                    label=await t(self.guild_id, "settings.test.thank_you"),
                    style=discord.ButtonStyle.success,
                    emoji=theme.emojis.get("rocket", ""),
                ),
                self.make_button(
                    custom_id="settings_test:reminder",
                    label=await t(self.guild_id, "settings.test.reminder"),
                    emoji=theme.emojis.get("clock", ""),
                ),
                self.make_button(
                    custom_id="settings_test:leaderboard",
                    label=await t(self.guild_id, "settings.test.leaderboard"),
                    emoji=theme.emojis.get("trophy", ""),
                ),
            ),
            discord.ui.ActionRow(
                self.make_button(
                    custom_id="settings_test:lock",
                    label=await t(self.guild_id, "settings.test.lock"),
                    emoji=theme.emojis.get("lock", ""),
                ),
                self.make_button(
                    custom_id="settings_test:unlock",
                    label=await t(self.guild_id, "settings.test.unlock"),
                    emoji=theme.emojis.get("unlock", ""),
                ),
                self.make_button(
                    custom_id="settings_test:reset",
                    label=await t(self.guild_id, "settings.test.reset"),
                    style=discord.ButtonStyle.danger,
                    emoji=theme.emojis.get("refresh", ""),
                ),
            ),
            discord.ui.ActionRow(
                self.make_button(
                    custom_id="settings_test_close",
                    label=await t(self.guild_id, "settings.back"),
                    emoji=theme.emojis.get("back", ""),
                )
            ),
        ]

    async def _build_edit_page(
        self, t: Any
    ) -> list[discord.ui.ViewItem]:
        spec = SETTINGS[self.editing_key]
        label = await t(self.guild_id, f"settings.{spec.key}.label")
        description = await t(self.guild_id, f"settings.{spec.key}.description")
        container = designer_container(
            self.bot.theme.color("brand"),
            TextDisplay(self.bot.theme.md("edit_title", label=label)),
            TextDisplay(description),
            TextDisplay(await t(self.guild_id, "settings.select_value")),
        )

        items: list[discord.ui.ViewItem] = [container]

        if spec.value_type is SettingType.BOOLEAN:
            current = await get_setting(self.bot.storage, self.guild_id, spec.key)
            items.append(
                discord.ui.ActionRow(
                    self.make_button(
                        custom_id=f"settings_set_value:{spec.key}:true",
                        label=await t(self.guild_id, "settings.bool.true"),
                        style=discord.ButtonStyle.success,
                        disabled=current is True,
                    ),
                    self.make_button(
                        custom_id=f"settings_set_value:{spec.key}:false",
                        label=await t(self.guild_id, "settings.bool.false"),
                        style=discord.ButtonStyle.danger,
                        disabled=current is False,
                    ),
                )
            )
        elif spec.value_type is SettingType.CHOICE:
            select = self.make_select(
                custom_id=f"settings_set:{spec.key}",
                placeholder=label,
                options=[
                    discord.SelectOption(
                        label=await t(
                            self.guild_id, f"settings.{spec.key}.choices.{value}"
                        ),
                        value=value,
                    )
                    for value in spec.choices
                ],
            )
            items.append(discord.ui.ActionRow(select))
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
            items.append(discord.ui.ActionRow(select))
        elif spec.value_type is SettingType.ROLE:
            select = discord.ui.Select(
                select_type=discord.ComponentType.role_select,
                custom_id=f"settings_set:{spec.key}",
                placeholder=label,
                min_values=1,
                max_values=1,
            )
            select.callback = self._handlers[f"settings_set:{spec.key}"]
            items.append(discord.ui.ActionRow(select))

        items.append(divider())
        items.append(
            discord.ui.ActionRow(
                self.make_button(
                    custom_id="settings_back",
                    label=await t(self.guild_id, "settings.back"),
                )
            )
        )
        return items

def _category_pages(category: SettingCategory) -> int:
    total = len(settings_for_category(category))
    return max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
