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
from rosemary.ui.containers import TextDisplay, designer_container, divider
from rosemary.ui.menu import MenuView

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


class ColorsManagerView(MenuView):
    """List/add/attach/reorder/remove screens for one guild's colors."""

    def __init__(self, bot, guild_id: int, author_id: int) -> None:
        super().__init__(author_id=author_id)
        self.bot = bot
        self.guild_id = guild_id
        self.store = ColorStore(bot.storage.data_dir)
        self.page = 0
        self.mode = "list"  # list | attach | delete
        self.deleting_id: str | None = None
        self._pending_roles: tuple[int, ...] = ()
        self.flash: str | None = None
        self.flash_color: str = "brand"
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

    async def _ack(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    async def _exit(self, interaction: discord.Interaction) -> None:
        """Back to /settings: swap this view out on the same message."""
        from rosemary.ui.settings_menu import SettingsMenuView

        settings_view = SettingsMenuView(
            self.bot, self.guild_id, owner_id=self.author_id
        )
        await settings_view.prepare()
        await interaction.edit(view=settings_view)
        self.stop()

    async def _back(self, interaction: discord.Interaction) -> None:
        await self._ack(interaction)
        self.mode = "list"
        self.deleting_id = None
        self._pending_roles = ()
        await self.rerender(interaction)

    # helpers ====================

    async def _entries(self):
        return await self.store.list_colors(self.guild_id)

    def _flash_in(self, parts: list) -> None:
        if self.flash:
            parts.append(TextDisplay(self.flash))
            self.flash = None

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
        if self.mode == "attach":
            return await self._build_attach()
        if self.mode == "delete":
            return await self._build_delete()
        return await self._build_list()

    async def _build_list(self) -> list[discord.ui.ViewItem]:
        theme = self.bot.theme
        entries = await self._entries()
        guild = self.bot.get_guild(self.guild_id)
        live = await self._role_colors(guild) if guild else {}
        from rosemary.ui.colors_panel import entry_number

        parts = [
            TextDisplay(theme.md("title", title=await self._t("colors.manager.title"))),
            TextDisplay(await self._t("colors.manager.hint", count=len(entries), max=MAX_COLORS)),
        ]
        self._flash_in(parts)
        container = designer_container(theme.color("brand"), *parts)

        items: list[discord.ui.ViewItem] = [container, divider()]
        window, total_pages = self._page_window(entries)
        offset = self.page * LIST_ITEMS_PER_PAGE
        for position, entry in enumerate(window, start=offset + 1):
            hex_value = resolve_color(live, entry)
            role_part = (
                f" <@&{entry.role_id}>"
                if entry.role_id
                else await self._t("colors.manager.no_role")
            )
            row = discord.ui.ActionRow(
                self.make_button(
                    custom_id=f"colors_mgr_up:{entry.id}",
                    label="↑",
                    disabled=position == 1,
                ),
                self.make_button(
                    custom_id=f"colors_mgr_down:{entry.id}",
                    label="↓",
                    disabled=position == len(entries),
                ),
                self.make_button(
                    custom_id=f"colors_mgr_edit:{entry.id}",
                    label=await self._t("colors.manager.edit"),
                    emoji=theme.emojis.get("gear", ""),
                ),
                self.make_button(
                    custom_id=f"colors_mgr_remove:{entry.id}",
                    label=await self._t("colors.manager.remove"),
                    emoji=theme.emojis.get("trash", ""),
                    style=discord.ButtonStyle.danger,
                ),
            )
            # Section accessories accept a single button or thumbnail only
            # (Discord 50035 otherwise), so each entry is a text line plus a
            # top-level ActionRow, not a Section with a row accessory.
            items.append(
                TextDisplay(
                    f"**{entry_number(position)}** {entry.name} · `{hex_value}`{role_part}"
                )
            )
            items.append(row)
        items.append(divider())

        nav = [
            self.make_button(
                custom_id="colors_mgr_add",
                label=await self._t("colors.manager.add"),
                emoji=theme.emojis.get("plus", ""),
                style=discord.ButtonStyle.success,
                disabled=len(entries) >= MAX_COLORS,
            ),
            self.make_button(
                custom_id="colors_mgr_create_role",
                label=await self._t("colors.manager.role_add"),
                emoji=theme.emojis.get("register", ""),
                style=discord.ButtonStyle.success,
                disabled=len(entries) >= MAX_COLORS,
            ),
            self.make_button(
                custom_id="colors_mgr_attach",
                label=await self._t("colors.manager.attach"),
                style=discord.ButtonStyle.primary,
            ),
        ]
        if total_pages > 1:
            if self.page > 0:
                nav.append(
                    self.make_button(
                        custom_id="colors_mgr_page_prev", label=await self._t("settings.prev")
                    )
                )
            if self.page < total_pages - 1:
                nav.append(
                    self.make_button(
                        custom_id="colors_mgr_page_next", label=await self._t("settings.next")
                    )
                )
        items.append(discord.ui.ActionRow(*nav))
        # Nav row follows the settings convention: Back pinned at index 0,
        # returning to /settings on the same message (no dead-end Close).
        items.append(
            discord.ui.ActionRow(
                self.make_button(
                    custom_id="colors_mgr_exit",
                    label=await self._t("settings.back"),
                    emoji=theme.emojis.get("back", ""),
                ),
            )
        )
        return items

    async def _build_attach(self) -> list[discord.ui.ViewItem]:
        theme = self.bot.theme
        pending = self._pending_roles
        parts = [
            TextDisplay(theme.md("title", title=await self._t("colors.manager.attach_title"))),
            TextDisplay(await self._t("colors.manager.attach_hint")),
        ]
        if pending:
            shown = " ".join(f"<@&{p}>" for p in pending[:25])
            parts.append(
                TextDisplay(
                    await self._t(
                        "colors.manager.pending", count=len(pending), roles=shown
                    )
                )
            )
        self._flash_in(parts)
        container = designer_container(theme.color("brand"), *parts)
        select = discord.ui.Select(
            select_type=discord.ComponentType.role_select,
            custom_id="colors_mgr_pick",
            placeholder=await self._t("colors.manager.attach_placeholder"),
            min_values=1,
            max_values=25,
        )
        select.callback = self._handlers["colors_mgr_pick"]
        return [
            container,
            discord.ui.ActionRow(select),
            discord.ui.ActionRow(
                self.make_button(
                    custom_id="colors_mgr_attach_confirm",
                    label=await self._t("settings.list.confirm"),
                    emoji=theme.emojis.get("check", ""),
                    style=discord.ButtonStyle.success,
                    disabled=not pending,
                ),
                self.make_button(
                    custom_id="colors_mgr_back",
                    label=await self._t("settings.cancel"),
                    emoji=theme.emojis.get("back", ""),
                ),
            ),
        ]

    async def _build_delete(self) -> list[discord.ui.ViewItem]:
        theme = self.bot.theme
        entry = await self.store.get_color(self.guild_id, self.deleting_id or "")
        name = entry.name if entry else "?"
        container = designer_container(
            theme.color("danger"),
            TextDisplay(theme.md("title", title=await self._t("colors.manager.delete_title"))),
            TextDisplay(await self._t("colors.manager.delete_text", name=name)),
        )
        return [
            container,
            discord.ui.ActionRow(
                self.make_button(
                    custom_id="colors_mgr_delete_yes",
                    label=await self._t("colors.manager.delete_confirm"),
                    emoji=theme.emojis.get("trash", ""),
                    style=discord.ButtonStyle.danger,
                ),
                self.make_button(
                    custom_id="colors_mgr_delete_no",
                    label=await self._t("settings.cancel"),
                    emoji=theme.emojis.get("back", ""),
                ),
            ),
        ]

    # mutations ====================

    async def _page(self, interaction: discord.Interaction, delta: int) -> None:
        await self._ack(interaction)
        self.page = max(0, self.page + delta)
        await self.rerender(interaction)

    async def _move(self, interaction: discord.Interaction, color_id: str, delta: int) -> None:
        await self._ack(interaction)
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
            self.flash_color = "brand"
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
        await self._ack(interaction)
        self.mode = "attach"
        await self.rerender(interaction)

    async def _attach_pick(self, interaction: discord.Interaction) -> None:
        """Stage role picks in the select itself; Concluir applies them."""
        await self._ack(interaction)
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
        await self._ack(interaction)
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
        await self._ack(interaction)
        self.mode = "delete"
        self.deleting_id = _suffix(interaction.custom_id)
        await self.rerender(interaction)

    async def _delete_yes(self, interaction: discord.Interaction) -> None:
        await self._ack(interaction)
        removed = await self.store.remove_color(self.guild_id, self.deleting_id or "")
        if removed is not None:
            await self._panel_repaint()
            self.flash = await self._t("colors.manager.removed", name=removed.name)
        self.mode = "list"
        self.deleting_id = None
        await self.rerender(interaction)

    async def _delete_no(self, interaction: discord.Interaction) -> None:
        await self._ack(interaction)
        self.mode = "list"
        self.deleting_id = None
        await self.rerender(interaction)


def _suffix(custom_id: str) -> str:
    return custom_id.split(":", 1)[-1] if ":" in custom_id else custom_id
