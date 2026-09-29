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
from rosemary.ui.menu import MenuView


class RoleplayManagerView(MenuView):
    """Manage the guild's gif lists per action."""

    def __init__(self, bot, guild_id: int, author_id: int) -> None:
        super().__init__(author_id=author_id)
        self.bot = bot
        self.guild_id = guild_id
        self.store = RoleplayStore(bot.storage.data_dir)
        self.action = ACTIONS[0]
        self.flash: str | None = None
        self.flash_color = "brand"
        self._register_handlers()

    async def _t(self, key: str, **kwargs) -> str:
        return await self.bot.translator.t(self.guild_id, key, **kwargs)

    def _register_handlers(self) -> None:
        self.register("rp_mgr_exit", self._exit)
        self.register("rp_mgr_action", self._pick_action)
        self.register("rp_mgr_add", self._open_add)
        self.register("rp_mgr_remove", self._remove)
        self.register("rp_mgr_restore", self._restore)
        self.register("rp_mgr_add_modal", self._add_submit)

    async def _ack(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    #: navigation ------------------------------------------------------------

    async def _exit(self, interaction: discord.Interaction) -> None:
        from rosemary.ui.settings_menu import SettingsMenuView

        settings_view = SettingsMenuView(
            self.bot, self.guild_id, owner_id=self.author_id
        )
        await settings_view.prepare()
        await interaction.edit(view=settings_view)
        self.stop()

    async def _pick_action(self, interaction: discord.Interaction) -> None:
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
                title=(await self._t("roleplay.manager.add_modal_title"))[:45],
                custom_id="rp_mgr_add_modal",
                first_label=await self._t("roleplay.manager.add_hint"),
                first_max_length=400,
                second_label=await self._t("roleplay.manager.add_api_hint"),
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
                await self._t("roleplay.manager.invalid"), ephemeral=True
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
        self.flash = await self._t("roleplay.manager.added")
        self.flash_color = "brand"
        await self._ack(interaction)
        await self.rerender(interaction)

    async def _remove(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values or not values[0].isdigit():
            return await self._ack(interaction)
        gifs = await self._gifs()
        index = int(values[0])
        if 0 <= index < len(gifs):
            gifs.pop(index)
            await self.store.set_gifs(self.guild_id, self.action, gifs)
            self.flash = await self._t("roleplay.manager.removed")
            self.flash_color = "danger"
        await self.rerender(interaction)

    async def _restore(self, interaction: discord.Interaction) -> None:
        """Re-seed from the theme, discarding the guild list."""
        theme = self.bot.theme
        gifs = theme.roleplay_gifs(self.action) if theme else []
        await self.store.set_gifs(self.guild_id, self.action, gifs)
        self.flash = await self._t("roleplay.manager.restored")
        self.flash_color = "brand"
        await self.rerender(interaction)

    #: rendering --------------------------------------------------------------

    async def build_items(self) -> list[discord.ui.ViewItem]:
        """MenuView contract: return the items for the current state.

        ``MenuView.prepare`` clears and re-adds them on every re-render;
        adding items here directly made rerenders accumulate rows until
        Discord 400'd on a duplicate custom_id.
        """
        from rosemary.core.card_service import menu_heading_items
        from rosemary.ui.containers import TextDisplay, designer_container

        theme = self.bot.theme
        action = await self._t(f"roleplay.actions.{self.action}")
        parts = await menu_heading_items(self.bot, self.guild_id, "roleplay.manager") or [
            TextDisplay(
                theme.md("title", title=await self._t("roleplay.manager.title"))
                if theme
                else await self._t("roleplay.manager.title")
            ),
            TextDisplay(
                await self._t("roleplay.manager.body", action=action)
            ),
        ]
        if self.flash:
            parts.append(TextDisplay(self.flash))
        container = designer_container(
            theme.color(self.flash_color or "brand") if theme else discord.Colour.blurple(),
            *parts,
        )
        items: list[discord.ui.ViewItem] = [container]

        gifs = await self._gifs()
        lines = [await self._entry_line(index, gif) for index, gif in enumerate(gifs[:10])]
        items.append(
            TextDisplay(
                f"**{await self._t('roleplay.manager.list_title')}**\n"
                + ("\n".join(lines) if lines else await self._t("roleplay.manager.empty"))
            )
        )

        items.append(
            discord.ui.ActionRow(
                self.make_select(
                    custom_id="rp_mgr_action",
                    placeholder=await self._t("roleplay.manager.title"),
                    options=[
                        discord.SelectOption(
                            label=await self._t(f"roleplay.actions.{action}"),
                            value=action,
                            default=(action == self.action),
                        )
                        for action in ACTIONS
                    ],
                )
            )
        )
        items.append(
            discord.ui.ActionRow(
                self.make_button(
                    custom_id="rp_mgr_add",
                    label=await self._t("roleplay.manager.add"),
                    style=discord.ButtonStyle.primary,
                ),
                self.make_button(
                    custom_id="rp_mgr_restore",
                    label=await self._t("roleplay.manager.restore"),
                ),
            )
        )
        if gifs:
            options = [
                discord.SelectOption(
                    label=f"{index + 1}",
                    description=(
                        gif.get("url") or f"api: {gif.get('api', '')}"
                    )[:100],
                )
                for index, gif in enumerate(gifs[:25])
            ]
            items.append(
                discord.ui.ActionRow(
                    self.make_select(
                        custom_id="rp_mgr_remove",
                        placeholder=await self._t("roleplay.manager.remove_hint"),
                        options=options,
                    )
                )
            )

        self.flash = None
        self.flash_color = "brand"
        return items

    async def _entry_line(self, index: int, gif: dict[str, str]) -> str:
        """One numbered list line: the url, or the api category tag."""
        url = (gif.get("url") or "").strip()
        if url:
            mode = await self._t("roleplay.manager.mode_url")
            return f"{index + 1}. {mode}: {url}"
        category = (gif.get("api") or "").strip()
        mode = await self._t("roleplay.manager.mode_api")
        return f"{index + 1}. {mode}: {category}"
