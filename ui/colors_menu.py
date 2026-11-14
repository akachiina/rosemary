"""Color panel manager: the /settings "Gerenciar cores" screens.

State machine over one :class:`MenuView`: list (paginated, reorder/remove),
attach (batch role multi-select, the anti-invite pattern) and a delete
confirm (:mod:`rosemary.ui.confirm`). Edits and creation run through modals
(:mod:`rosemary.ui.modals`). Every mutation ACKs, persists through
:class:`rosemary.core.colors.ColorStore`, then repaints the posted panel and
re-renders - the same ordering the ACK tests pin everywhere else.

Role color resolution: an attached role's **live color** replaces the stored
hex for rendering (admins recoloring the role see the panel catch up on the
next repaint); the stored hex is only the fallback and the value used before
a role is attached.
"""

from __future__ import annotations

import discord

from rosemary.core.colors import MAX_COLORS, ColorStore
from rosemary.ui.kit import ActionRow, Panel, TextDisplay, divider

# Discord caps a message at 40 components total (nested items count). Each
# entry costs two top-level items (text line + action row) plus its four
# buttons, so the page holds at most 4 entries alongside header/nav rows.
LIST_ITEMS_PER_PAGE = 4


def resolve_color(member_colors: dict[int, int | None], entry) -> str:
    """Display hex for ``entry``: the attached role's live color wins.

    ``member_colors`` maps role_id -> raw role color int (0/None = default
    color, which falls back to the stored hex).
    """
    if entry.role_id:
        live = member_colors.get(entry.role_id)
        if live:
            return f"#{live:06X}"
    return entry.color


class ColorsManagerView(Panel):
    """List/add/attach/reorder/remove screens for one guild's colors."""

    #: The navigation keeps the ids of the menu: a message posted before the
    #: migration must keep working. ``back`` on the list swaps the settings
    #: menu onto the same message; on the sub-screens it returns to the list.
    nav_ids = {
        "back": "colors_mgr_exit",
        "previous": "colors_mgr_page_prev",
        "next": "colors_mgr_page_next",
    }
    nav_labels = {
        "back": "settings.back",
        "previous": "settings.prev",
        "next": "settings.next",
    }
    #: The support accent (the family of the panels that help).
    color: str = "support"

    def __init__(self, bot, guild_id: int, author_id: int) -> None:
        super().__init__(bot, guild_id, author_id=author_id)
        self.store = ColorStore(bot.storage.data_dir)
        self.page = 0
        self.mode = "list"  # list | attach | delete
        self.deleting_id: str | None = None
        self._pending_roles: tuple[int, ...] = ()
        self._page_total = 1
        self._register_handlers()
        # First open seeds the pastel defaults so the manager never shows an
        # empty list; store keeps a flag, so this is a no-op after the first
        # time even if the admin removes every color.
        self.seeded_entries: list | None = None

    async def ensure_seeded(self) -> None:
        """Seed the pastel defaults once (cog resolves translated names)."""
        if self.seeded_entries is not None:
            return
        self.seeded_entries = []
        cog = self.bot.get_cog("ColorsCog")
        if cog is not None:
            self.seeded_entries = await cog.seed_if_needed(self.guild_id)

    # handlers ====================

    def _register_handlers(self) -> None:
        self.register("colors_mgr_exit", self._exit)
        self.register("colors_mgr_back", self._back)
        self.register("colors_mgr_add", self._add)
        self.register("colors_mgr_attach", self._open_attach)
        self.register("colors_mgr_pick", self._attach_pick)
        self.register("colors_mgr_attach_confirm", self._attach_confirm)
        self.register("colors_mgr_page_prev", lambda i: self._page(i, -1))
        self.register("colors_mgr_page_next", lambda i: self._page(i, 1))
        self.register("colors_mgr_up", self._move_up)
        self.register("colors_mgr_down", self._move_down)
        self.register("colors_mgr_edit", self._edit)
        self.register("colors_mgr_create_role", self._create_role)
        self.register("colors_mgr_remove", self._open_delete)
        self.register("colors_mgr_delete_yes", self._delete_yes)
        self.register("colors_mgr_delete_no", self._delete_no)

    async def _t(self, key: str, **kwargs) -> str:
        return await self.bot.translator.t(self.guild_id, key, **kwargs)

    async def _exit(self, interaction: discord.Interaction) -> None:
        """Back to /settings: swap this view out on the same message."""
        from rosemary.ui.settings_menu import SettingsMenuView

        if not await self._ack(interaction):
            return
        settings_view = SettingsMenuView(
            self.bot, self.guild_id, owner_id=self.author_id
        )
        await settings_view.prepare()
        await interaction.edit(view=settings_view)
        self.stop()

    async def _back(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        self.mode = "list"
        self.deleting_id = None
        self._pending_roles = ()
        await self.rerender(interaction)

    # helpers ====================

    async def _entries(self):
        return await self.store.list_colors(self.guild_id)

    def _page_window(self, entries: list):
        total_pages = max(1, -(-len(entries) // LIST_ITEMS_PER_PAGE))
        self.page = min(self.page, total_pages - 1)
        start = self.page * LIST_ITEMS_PER_PAGE
        return entries[start : start + LIST_ITEMS_PER_PAGE], total_pages

    async def _role_colors(self, guild: discord.Guild) -> dict[int, int | None]:
        return {
            role.id: getattr(role, "color", None).value if getattr(role, "color", None) else 0
            for role in guild.roles
        }

    async def _panel_repaint(self) -> None:
        cog = self.bot.get_cog("ColorsCog")
        if cog is not None:
            await cog.repaint_panel(self.bot, self.guild_id)

    # screens ====================

    async def build_items(self) -> list[discord.ui.ViewItem]:
        await self.ensure_seeded()
        if self.expired:
            return await self.expired_items()
        if self.mode == "attach":
            return await self._build_attach()
        if self.mode == "delete":
            return await self._build_delete()
        return await self._build_list()

    async def _build_list(self) -> list[discord.ui.ViewItem]:
        entries = await self._entries()
        guild = self.bot.get_guild(self.guild_id)
        live = await self._role_colors(guild) if guild else {}
        from rosemary.ui.colors_panel import entry_number

        await self.set_screen(
            "colors.manager.title",
            "colors.manager.hint",
            heading="colors.manager",
            count=len(entries),
            max=MAX_COLORS,
        )
        window, total_pages = self._page_window(entries)
        self._page_total = total_pages
        self.nav_ids["back"] = "colors_mgr_exit"
        offset = self.page * LIST_ITEMS_PER_PAGE

        sections: list[discord.ui.ViewItem] = [divider()]
        for position, entry in enumerate(window, start=offset + 1):
            hex_value = resolve_color(live, entry)
            role_part = (
                f" <@&{entry.role_id}>"
                if entry.role_id
                else await self.t("colors.manager.no_role")
            )
            # Section accessories accept a single button or thumbnail only
            # (Discord 50035 otherwise), so each entry is a text line plus a
            # top-level ActionRow, not a Section with a row accessory.
            sections.append(
                TextDisplay(
                    f"**{entry_number(position)}** {entry.name} · `{hex_value}`{role_part}"
                )
            )
            sections.append(
                ActionRow(
                    await self.button(
                        f"colors_mgr_up:{entry.id}",
                        "colors.manager.move_up",
                        emoji="up",
                        disabled=position == 1,
                    ),
                    await self.button(
                        f"colors_mgr_down:{entry.id}",
                        "colors.manager.move_down",
                        emoji="down",
                        disabled=position == len(entries),
                    ),
                    await self.button(
                        f"colors_mgr_edit:{entry.id}",
                        "colors.manager.edit",
                        emoji="gear",
                    ),
                    await self.button(
                        f"colors_mgr_remove:{entry.id}",
                        "colors.manager.remove",
                        emoji="trash",
                        style="danger",
                    ),
                )
            )
        sections.append(divider())

        actions = [
            ActionRow(
                await self.button(
                    "colors_mgr_add",
                    "colors.manager.add",
                    emoji="plus",
                    style="success",
                    disabled=len(entries) >= MAX_COLORS,
                ),
                await self.button(
                    "colors_mgr_create_role",
                    "colors.manager.role_add",
                    emoji="register",
                    style="success",
                    disabled=len(entries) >= MAX_COLORS,
                ),
                await self.button(
                    "colors_mgr_attach",
                    "colors.manager.attach",
                    emoji="tag",
                    style="primary",
                ),
            )
        ]
        nav = ["back"]
        if total_pages > 1:
            if self.page > 0:
                nav.append("previous")
            if self.page < total_pages - 1:
                nav.append("next")
        self.nav(*nav)
        return await self.compose(sections, actions)

    async def _build_attach(self) -> list[discord.ui.ViewItem]:
        pending = self._pending_roles
        await self.set_screen(
            "colors.manager.attach_title",
            "colors.manager.attach_hint",
            color="support",
        )
        if pending:
            shown = " ".join(f"<@&{p}>" for p in pending[:25])
            self.lines = [
                TextDisplay(
                    await self.t(
                        "colors.manager.pending", count=len(pending), roles=shown
                    )
                )
            ]
        self.nav_ids["back"] = "colors_mgr_back"
        self.nav("back", back="settings.cancel")
        select = self.make_role_select(
            custom_id="colors_mgr_pick",
            placeholder=await self.t("colors.manager.attach_placeholder"),
            max_values=25,
        )
        confirm = await self.button(
            "colors_mgr_attach_confirm",
            "settings.list.confirm",
            emoji="check",
            style="success",
            disabled=not pending,
        )
        return await self.compose([], [ActionRow(select), ActionRow(confirm)])

    async def _build_delete(self) -> list[discord.ui.ViewItem]:
        entry = await self.store.get_color(self.guild_id, self.deleting_id or "")
        name = entry.name if entry else "?"
        await self.set_screen(
            "colors.manager.delete_title",
            "colors.manager.delete_text",
            color="danger",
            name=name,
        )
        self.nav_ids["back"] = "colors_mgr_back"
        self.nav("back", back="settings.cancel")
        confirm = await self.button(
            "colors_mgr_delete_yes",
            "colors.manager.delete_confirm",
            emoji="trash",
            style="danger",
        )
        return await self.compose([], [ActionRow(confirm)])

    # mutations ====================

    async def _page(self, interaction: discord.Interaction, delta: int) -> None:
        if not await self._ack(interaction):
            return
        self.page = max(0, self.page + delta)
        await self.rerender(interaction)

    async def _move(self, interaction: discord.Interaction, color_id: str, delta: int) -> None:
        if not await self._ack(interaction):
            return
        if await self.store.move(self.guild_id, color_id, delta):
            await self._panel_repaint()
        await self.rerender(interaction)

    async def _move_up(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, _suffix(interaction.custom_id), -1)

    async def _move_down(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, _suffix(interaction.custom_id), 1)

    async def _edit(self, interaction: discord.Interaction) -> None:
        entry = await self.store.get_color(self.guild_id, _suffix(interaction.custom_id))
        if entry is None:
            return await self._ack(interaction)
        from rosemary.ui.modals import make_two_field_modal

        await interaction.response.send_modal(
            make_two_field_modal(
                title=(await self._t("colors.manager.edit_modal_title"))[:45],
                custom_id="colors_mgr_edit_modal",
                first_label=await self._t("colors.manager.field_name"),
                first_value=entry.name,
                first_max_length=50,
                second_label=await self._t("colors.manager.field_hex"),
                second_value=entry.color,
                second_max_length=7,
                on_submit=lambda inner, name, hex_value: self._edit_submit(
                    inner, entry.id, name, hex_value
                ),
            )
        )

    async def _edit_submit(
        self, interaction: discord.Interaction, color_id: str, name: str, hex_value: str
    ) -> None:
        from rosemary.core.colors import _HEX_OK

        name = (name or "").strip()
        hex_value = (hex_value or "").strip()
        updates: dict = {}
        if hex_value and not _HEX_OK(hex_value):
            self.flash = await self._t("colors.error_bad_hex")
            self.flash_color = "danger"
        else:
            if name:
                updates["name"] = name
            if hex_value:
                if not hex_value.startswith("#"):
                    hex_value = f"#{hex_value}"
                updates["color"] = hex_value
        if updates:
            await self.store.update_color(self.guild_id, color_id, **updates)
            await self._panel_repaint()
            self.flash = await self._t("colors.manager.saved")
            self.flash_color = "support"
        await self._ack(interaction)
        await self.rerender(interaction)

    async def _add(self, interaction: discord.Interaction) -> None:
        from rosemary.ui.modals import make_two_field_modal

        await interaction.response.send_modal(
            make_two_field_modal(
                title=(await self._t("colors.manager.add_modal_title"))[:45],
                custom_id="colors_mgr_add_modal",
                first_label=await self._t("colors.manager.field_name"),
                first_max_length=50,
                second_label=await self._t("colors.manager.field_hex"),
                second_max_length=7,
                on_submit=lambda inner, name, hex_value: self._add_submit(inner, name, hex_value),
            )
        )

    async def _add_submit(
        self, interaction: discord.Interaction, name: str, hex_value: str
    ) -> None:
        from rosemary.core.colors import _HEX_OK

        name = (name or "").strip()
        hex_value = (hex_value or "").strip()
        if not name or not _HEX_OK(hex_value):
            await interaction.response.send_message(
                await self._t("colors.error_bad_hex"), ephemeral=True
            )
            return
        if not hex_value.startswith("#"):
            hex_value = f"#{hex_value}"
        created = await self.store.add_color(self.guild_id, name, hex_value)
        if created is None:
            await interaction.response.send_message(
                await self._t("colors.error_full", max=MAX_COLORS), ephemeral=True
            )
            return
        await self._panel_repaint()
        self.flash = await self._t("colors.manager.added", name=created.name)
        await self._ack(interaction)
        await self.rerender(interaction)

    async def _create_role(self, interaction: discord.Interaction) -> None:
        """Create a new guild role with the chosen color, then attach it."""
        from rosemary.ui.modals import make_two_field_modal

        await interaction.response.send_modal(
            make_two_field_modal(
                title=(await self._t("colors.manager.role_modal_title"))[:45],
                custom_id="colors_mgr_role_modal",
                first_label=await self._t("colors.manager.field_name"),
                first_max_length=50,
                second_label=await self._t("colors.manager.field_hex"),
                second_max_length=7,
                on_submit=lambda inner, name, hex_value: self._create_role_submit(
                    inner, name, hex_value
                ),
            )
        )

    async def _create_role_submit(
        self, interaction: discord.Interaction, name: str, hex_value: str
    ) -> None:
        from rosemary.core.colors import _HEX_OK

        guild = self.bot.get_guild(self.guild_id)
        name = (name or "").strip()
        hex_value = (hex_value or "").strip()
        if guild is None or not name or not _HEX_OK(hex_value):
            await interaction.response.send_message(
                await self._t("colors.error_bad_hex"), ephemeral=True
            )
            return
        if not guild.me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                await self._t("colors.error_no_perms"), ephemeral=True
            )
            return
        if not hex_value.startswith("#"):
            hex_value = f"#{hex_value}"
        role = await guild.create_role(
            name=name,
            colour=discord.Colour(int(hex_value.lstrip("#"), 16)),
            reason="Color panel",
        )
        created = await self.store.add_color(self.guild_id, name, hex_value, role.id)
        if created is None:
            await interaction.response.send_message(
                await self._t("colors.error_full", max=MAX_COLORS), ephemeral=True
            )
            return
        await self._panel_repaint()
        self.flash = await self._t("colors.manager.role_created", role=role.name)
        await self._ack(interaction)
        self.mode = "list"
        await self.rerender(interaction)

    async def _open_attach(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        self.mode = "attach"
        await self.rerender(interaction)

    async def _attach_pick(self, interaction: discord.Interaction) -> None:
        """Stage role picks in the select itself; Concluir applies them."""
        if not await self._ack(interaction):
            return
        values = (interaction.data or {}).get("values") or []
        picks: list[int] = []
        for raw in values:
            try:
                picks.append(int(raw))
            except (TypeError, ValueError):
                continue
        merged = list(self._pending_roles)
        merged.extend(p for p in picks if p not in merged)
        self._pending_roles = tuple(merged)
        await self.rerender(interaction)

    async def _attach_confirm(self, interaction: discord.Interaction) -> None:
        """Attach every staged role, inheriting its live color."""
        if not await self._ack(interaction):
            return
        pending = self._pending_roles
        self._pending_roles = ()
        guild = self.bot.get_guild(self.guild_id)
        if not pending or guild is None:
            return await self._back(interaction)
        entries = await self._entries()
        taken = {entry.role_id for entry in entries if entry.role_id}
        from rosemary.core.colors import ColorEntry, new_color_id

        attached = 0
        for role_id in pending:
            if len(entries) >= MAX_COLORS:
                break
            role = guild.get_role(role_id)
            if role is None or role_id in taken:
                continue
            live = getattr(getattr(role, "color", None), "value", 0) or 0
            hex_value = f"#{live:06X}" if live else "#99AAB5"
            entries.append(ColorEntry(new_color_id(), role.name, hex_value, role_id))
            attached += 1
        if attached:
            await self.store.set_colors(self.guild_id, entries)
            await self._panel_repaint()
            self.flash = await self._t("colors.manager.attached", count=attached)
        await self._back(interaction)

    async def _open_delete(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        self.mode = "delete"
        self.deleting_id = _suffix(interaction.custom_id)
        await self.rerender(interaction)

    async def _delete_yes(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        removed = await self.store.remove_color(self.guild_id, self.deleting_id or "")
        if removed is not None:
            await self._panel_repaint()
            self.flash = await self._t("colors.manager.removed", name=removed.name)
        self.mode = "list"
        self.deleting_id = None
        await self.rerender(interaction)

    async def _delete_no(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        self.mode = "list"
        self.deleting_id = None
        await self.rerender(interaction)


def _suffix(custom_id: str) -> str:
    return custom_id.split(":", 1)[-1] if ":" in custom_id else custom_id
