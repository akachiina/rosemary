"""Per-guild gif manager for interaction cards (the color-manager pattern).

Opened from the Roleplay category in ``/settings``; first edit seeds
the guild's list from the theme's ``roleplay.gifs.<action>`` section, then
the guild owns its own list (theme edits no longer apply). Entries are
either ``{"url": ...}`` (a fixed gif) or ``{"api": <category>}`` (a random
gif from that nekos.best category at send time); pick order lives in
:func:`rosemary.core.roleplay.resolve_gif`.
"""

from __future__ import annotations

import discord

from rosemary.core.roleplay import ACTIONS, RoleplayStore, valid_api_category
from rosemary.ui.kit import ActionRow, EmptyState, Panel, TextDisplay


class RoleplayManagerView(Panel):
    """Manage the guild's gif lists per action."""

    nav_ids = {"back": "rp_mgr_exit"}
    nav_labels = {"back": "settings.back"}

    def __init__(self, bot, guild_id: int, author_id: int) -> None:
        super().__init__(bot, guild_id, author_id=author_id)
        self.store = RoleplayStore(bot.storage.data_dir)
        self.action = ACTIONS[0]
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.register("rp_mgr_exit", self._exit)
        self.register("rp_mgr_action", self._pick_action)
        self.register("rp_mgr_add", self._open_add)
        self.register("rp_mgr_remove", self._remove)
        self.register("rp_mgr_restore", self._restore)
        self.register("rp_mgr_add_modal", self._add_submit)

    #: navigation ------------------------------------------------------------

    async def _exit(self, interaction: discord.Interaction) -> None:
        from rosemary.ui.settings_menu import SettingsMenuView

        if not await self._ack(interaction):
            return
        settings_view = SettingsMenuView(
            self.bot, self.guild_id, owner_id=self.author_id
        )
        await settings_view.prepare()
        await interaction.edit(view=settings_view)
        self.stop()

    async def _pick_action(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        values = (interaction.data or {}).get("values") or []
        if values and values[0] in ACTIONS:
            self.action = values[0]
            self.flash = None
        await self.rerender(interaction)

    #: list ------------------------------------------------------------------

    async def _gifs(self) -> list[dict[str, str]]:
        """The effective list, seeding from the theme on first open."""
        gifs = await self.store.get_gifs(self.guild_id, self.action)
        if not gifs:
            theme = self.bot.theme
            gifs = theme.roleplay_gifs(self.action) if theme else []
            if gifs:
                await self.store.set_gifs(self.guild_id, self.action, gifs)
        return gifs

    async def _open_add(self, interaction: discord.Interaction) -> None:
        from rosemary.ui.modals import make_two_field_modal

        await interaction.response.send_modal(
            make_two_field_modal(
                title=(await self.t("roleplay.manager.add_modal_title"))[:45],
                custom_id="rp_mgr_add_modal",
                first_label=await self.t("roleplay.manager.add_hint"),
                first_max_length=400,
                second_label=await self.t("roleplay.manager.add_api_hint"),
                second_max_length=40,
                on_submit=lambda inner, url, category: self._add_submit(
                    inner, url, category
                ),
            )
        )

    async def _add_submit(
        self, interaction: discord.Interaction, url: str, category: str
    ) -> None:
        """Persist one entry from the modal (url and/or api category)."""
        url = (url or "").strip()
        category = (category or "").strip().lower()
        valid_url = url.startswith("http://") or url.startswith("https://")
        if not valid_url and not valid_api_category(category):
            await interaction.response.send_message(
                await self.t("roleplay.manager.invalid"), ephemeral=True
            )
            return
        entry: dict[str, str] = {}
        if valid_url:
            entry["url"] = url
        if category:
            entry["api"] = category
        gifs = await self._gifs()
        gifs.append(entry)
        await self.store.set_gifs(self.guild_id, self.action, gifs)
        self.flash = await self.t("roleplay.manager.added")
        self.flash_color = "brand"
        self.flash_icon = "plus"
        await self._ack(interaction)
        await self.rerender(interaction)

    async def _remove(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        values = (interaction.data or {}).get("values") or []
        if not values or not values[0].isdigit():
            return
        gifs = await self._gifs()
        index = int(values[0])
        if 0 <= index < len(gifs):
            gifs.pop(index)
            await self.store.set_gifs(self.guild_id, self.action, gifs)
            self.flash = await self.t("roleplay.manager.removed")
            self.flash_color = "danger"
            self.flash_icon = "trash"
        await self.rerender(interaction)

    async def _restore(self, interaction: discord.Interaction) -> None:
        """Re-seed from the theme, discarding the guild list."""
        if not await self._ack(interaction):
            return
        theme = self.bot.theme
        gifs = theme.roleplay_gifs(self.action) if theme else []
        await self.store.set_gifs(self.guild_id, self.action, gifs)
        self.flash = await self.t("roleplay.manager.restored")
        self.flash_color = "brand"
        self.flash_icon = "refresh"
        await self.rerender(interaction)

    #: rendering --------------------------------------------------------------

    async def build_items(self) -> list[discord.ui.ViewItem]:
        """Build the screen: the theme's chrome plus this panel's content."""
        if self.expired:
            return await self.expired_items()
        action = await self.t(f"roleplay.actions.{self.action}")
        await self.set_screen(
            "roleplay.manager.title",
            "roleplay.manager.body",
            heading="roleplay.manager",
            action=action,
        )
        self.nav("back", back="settings.back")
        gifs = await self._gifs()

        lines = [await self._entry_line(index, gif) for index, gif in enumerate(gifs[:10])]
        if lines:
            sections = [
                TextDisplay(
                    f"**{await self.t('roleplay.manager.list_title')}**\n"
                    + "\n".join(lines)
                )
            ]
        else:
            sections = [
                TextDisplay(f"**{await self.t('roleplay.manager.list_title')}**"),
                EmptyState(
                    await self.t("roleplay.manager.empty"),
                    icon=self.emoji("frame"),
                ).item(),
            ]

        actions = [
            ActionRow(
                self.make_select(
                    custom_id="rp_mgr_action",
                    placeholder=await self.t("roleplay.manager.title"),
                    options=[
                        discord.SelectOption(
                            label=await self.t(f"roleplay.actions.{item}"),
                            value=item,
                            default=(item == self.action),
                        )
                        for item in ACTIONS
                    ],
                )
            ),
            ActionRow(
                await self.button(
                    "rp_mgr_add",
                    "roleplay.manager.add",
                    style="success",
                    emoji="plus",
                ),
                await self.button(
                    "rp_mgr_restore",
                    "roleplay.manager.restore",
                    emoji="refresh",
                ),
            ),
        ]
        if gifs:
            options = [
                discord.SelectOption(
                    label=f"{index + 1}",
                    description=(gif.get("url") or f"api: {gif.get('api', '')}")[:100],
                )
                for index, gif in enumerate(gifs[:25])
            ]
            actions.append(
                ActionRow(
                    self.make_select(
                        custom_id="rp_mgr_remove",
                        placeholder=await self.t("roleplay.manager.remove_hint"),
                        options=options,
                    )
                )
            )
        return await self.compose(sections, actions)

    async def _entry_line(self, index: int, gif: dict[str, str]) -> str:
        """One numbered list line: the url, or the api category tag."""
        url = (gif.get("url") or "").strip()
        if url:
            mode = await self.t("roleplay.manager.mode_url")
            return f"{index + 1}. {mode}: {url}"
        category = (gif.get("api") or "").strip()
        mode = await self.t("roleplay.manager.mode_api")
        return f"{index + 1}. {mode}: {category}"
