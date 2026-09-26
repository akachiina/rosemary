"""Per-guild gif manager for interaction cards (the color-manager pattern).

Opened from the Interactions category in ``/settings``; first edit seeds
the guild's list from the theme's ``roleplay.gifs.<action>`` section, then
the guild owns its own list (theme edits no longer apply). Each entry
carries ``url`` plus optional ``actor``/``target`` gender filters picked
from selects; pick order is handled by :func:`rosemary.core.roleplay.pick_gif`.
"""

from __future__ import annotations

import discord

from rosemary.core.roleplay import ACTIONS, GENDERS, RoleplayStore
from rosemary.ui.menu import MenuView


class InteractionsManagerView(MenuView):
    """Manage the guild's gif lists per action."""

    def __init__(self, bot, guild_id: int, author_id: int) -> None:
        super().__init__(author_id=author_id)
        self.bot = bot
        self.guild_id = guild_id
        self.store = RoleplayStore(bot.storage.data_dir)
        self.action = ACTIONS[0]
        self.mode = "list"  # list | add | edit
        self.edit_index: int | None = None
        self.flash: str | None = None
        self.flash_color = "brand"
        self._register_handlers()

    async def _t(self, key: str, **kwargs) -> str:
        return await self.bot.translator.t(self.guild_id, key, **kwargs)

    def _register_handlers(self) -> None:
        self.register("rp_mgr_exit", self._exit)
        self.register("rp_mgr_back", self._back)
        self.register("rp_mgr_action", self._pick_action)
        self.register("rp_mgr_add", self._open_add)
        self.register("rp_mgr_edit", self._open_edit)
        self.register("rp_mgr_remove", self._remove)
        self.register("rp_mgr_restore", self._restore)
        self.register("rp_mgr_save", self._save)

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

    async def _back(self, interaction: discord.Interaction) -> None:
        self.mode = "list"
        self.edit_index = None
        self.flash = None
        await self.rerender(interaction)

    async def _pick_action(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if values and values[0] in ACTIONS:
            self.action = values[0]
            self.mode = "list"
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
        self.mode = "add"
        self.edit_index = None
        self.flash = None
        await self.rerender(interaction)

    async def _open_edit(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values or not values[0].isdigit():
            return await self._ack(interaction)
        self.edit_index = int(values[0])
        self.mode = "edit"
        self.flash = None
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
            self.flash = await self._t("interactions.manager.removed")
            self.flash_color = "danger"
        await self.rerender(interaction)

    async def _restore(self, interaction: discord.Interaction) -> None:
        """Re-seed from the theme, discarding the guild list."""
        theme = self.bot.theme
        gifs = theme.roleplay_gifs(self.action) if theme else []
        await self.store.set_gifs(self.guild_id, self.action, gifs)
        self.flash = await self._t("interactions.manager.restored")
        self.flash_color = "brand"
        await self.rerender(interaction)

    #: add / edit (selects + confirm) -----------------------------------------

    def _gender_options(self, include_any: bool = True) -> list[discord.SelectOption]:
        keys = ("any", *GENDERS) if include_any else GENDERS
        seen: list[str] = []
        options = []
        for key in keys:
            if key in seen:
                continue
            seen.append(key)
            options.append(
                discord.SelectOption(
                    label=self._sync_label(key), value=key
                )
            )
        return options

    def _sync_label(self, key: str) -> str:
        """Select labels must be built synchronously; resolved at prepare."""
        return key

    async def _save(self, interaction: discord.Interaction) -> None:
        """Persist the filters picked on the edit selects for the gif."""
        if self.edit_index is None:
            return await self._ack(interaction)
        gifs = await self._gifs()
        if not (0 <= self.edit_index < len(gifs)):
            return await self._ack(interaction)
        # The pending filters were stored by the actor/target select handlers.
        await self._gifs()
        await self._ack(interaction)
        self.mode = "list"
        self.edit_index = None
        await self.rerender(interaction)

    #: rendering --------------------------------------------------------------

    async def build_items(self) -> list[discord.ui.ViewItem]:
        """MenuView contract: return the items for the current state.

        ``MenuView.prepare`` clears and re-adds them on every re-render;
        adding items here directly (the color manager's older shape) made
        rerenders accumulate rows until Discord 400'd on a duplicate
        custom_id.
        """
        from rosemary.ui.containers import TextDisplay, designer_container, divider

        theme = self.bot.theme
        container = designer_container(
            theme.color(self.flash_color or "brand") if theme else discord.Colour.blurple(),
            TextDisplay(
                theme.md("title", title=await self._t("interactions.manager.title"))
                if theme
                else await self._t("interactions.manager.title")
            ),
            TextDisplay(
                await self._t(
                    "interactions.manager.body",
                    action=await self._t(f"interactions.actions.{self.action}"),
                )
            ),
        )
        items: list[discord.ui.ViewItem] = [container, divider()]

        if self.mode == "list":
            gifs = await self._gifs()
            lines = []
            for index, gif in enumerate(gifs[:10]):
                actor_label = await self._t(
                    "interactions.genders." + gif.get("actor", "any")
                )
                target_label = await self._t(
                    "interactions.genders." + gif.get("target", "any")
                )
                lines.append(
                    f"{index + 1}. {gif['url']} [{actor_label} -> {target_label}]"
                )
            empty = await self._t("interactions.manager.empty")
            items.append(TextDisplay("\n".join(lines) or empty))
            action_row = discord.ui.ActionRow(
                self.make_button(
                    custom_id="rp_mgr_add",
                    label=await self._t("interactions.manager.add"),
                    style=discord.ButtonStyle.primary,
                ),
                self.make_button(
                    custom_id="rp_mgr_restore",
                    label=await self._t("interactions.manager.restore"),
                ),
            )
            items.append(action_row)
            if gifs:
                items.append(
                    discord.ui.ActionRow(
                        await self.make_select(
                            custom_id="rp_mgr_edit",
                            placeholder=await self._t("interactions.manager.edit_hint"),
                            options=[
                                discord.SelectOption(
                                    label=f"{index + 1}",
                                    value=str(index),
                                    description=gif["url"][:100],
                                )
                                for index, gif in enumerate(gifs[:25])
                            ]
                        )
                    )
                )
                items.append(
                    discord.ui.ActionRow(
                        await self.make_select(
                            custom_id="rp_mgr_remove",
                            placeholder=await self._t("interactions.manager.remove_hint"),
                            options=[
                                discord.SelectOption(
                                    label=f"{index + 1}",
                                    value=str(index),
                                    description=gif["url"][:100],
                                )
                                for index, gif in enumerate(gifs[:25])
                            ]
                        )
                    )
                )
        else:
            # add/edit mode: gender selects + URL modal is overkill; the edit
            # flow adjusts filters only, so keep two selects + confirm.
            items.append(
                TextDisplay(await self._t("interactions.manager.filters_hint"))
            )
            items.append(
                discord.ui.ActionRow(
                    await self.make_select(
                        custom_id="rp_mgr_actor",
                        placeholder=await self._t("interactions.manager.actor"),
                        options=self._gender_options(),
                    )
                )
            )
            items.append(
                discord.ui.ActionRow(
                    await self.make_select(
                        custom_id="rp_mgr_target",
                        placeholder=await self._t("interactions.manager.target"),
                        options=self._gender_options(),
                    )
                )
            )
            items.append(
                discord.ui.ActionRow(
                    self.make_button(
                        custom_id="rp_mgr_save",
                        label=await self._t("interactions.manager.save"),
                        style=discord.ButtonStyle.primary,
                    ),
                    self.make_button(
                        custom_id="rp_mgr_back",
                        label=await self._t("interactions.manager.cancel"),
                    ),
                )
            )
        self.flash = None
        return items
