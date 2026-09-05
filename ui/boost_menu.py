"""Interactive boost-role menus for Rosemary (V2 Components).

Two menus share the same ``BoostRoleStore``:

* :class:`BoostMenuView`, the member-facing panel (/boost). It lets owners
  manage their roles (rename, color, emoji, icon, members, invites, transfer,
  remove) and members see/leave the roles they belong to.
* :class:`AdminBoostMenuView`, the moderation panel (/boost_admin). It lists
  every registered role and can register, transfer or remove any of them.

State is kept in plain attributes and screens rebuild through
``MenuView.build_items``. Every user-facing string comes from the catalogs via
``t()`` and every emoji from ``theme.emojis``; nothing is hardcoded here.
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import discord

from rosemary.core.boost_roles import BoostRoleStore, parse_datetime
from rosemary.core.cards import text_or
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting
from rosemary.ui import boost_dm as dm
from rosemary.ui.color_palette import COLOR_PALETTE
from rosemary.ui.containers import (
    ActionRow,
    Container,
    Section,
    TextDisplay,
    designer_container,
    divider,
)
from rosemary.ui.menu import MenuView
from rosemary.ui.modals import make_by_id_modal, make_file_modal, make_text_modal
from rosemary.ui.pagination import page_count, paginate

log = logging.getLogger(__name__)

_LIST_PAGE_SIZE = 5

_EMOJI_RE = re.compile(
    r"\A[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    r"\U00002B00-\U00002BFF\U0000FE0F\u200D]+\Z"
)
_HEX_RE = re.compile(r"\A#?([0-9A-Fa-f]{6})\Z")


class _Screen(StrEnum):
    """Internal state machine for the menus."""

    HOME = "home"
    MY_ROLES = "my_roles"
    PENDING = "pending"
    REGISTER = "register"
    REGISTER_OWNER = "register_owner"
    LEAVE = "leave"
    MANAGE = "manage"
    COLOR = "color"
    MEMBERS = "members"
    INVITES = "invites"
    INVITE_TARGET = "invite_target"
    LEAVE_CONFIRM = "leave_confirm"
    REMOVE_CONFIRM = "remove_confirm"


def _suffix(custom_id: str, prefix: str) -> str:
    """Return the part of ``custom_id`` after ``prefix:``."""
    return custom_id.split(f"{prefix}:", 1)[1]


class _BaseMenu(MenuView):
    """Shared plumbing for both boost menus."""

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
        self.store = BoostRoleStore(bot.storage.data_dir)
        self.screen = _Screen.HOME
        self.page = 0
        self.role_id: int | None = None
        self.flash: str | None = None
        self.flash_color: str = "brand"
        self._register_handlers()

    # -- generic handlers ---------------------------------------------------

    def _register_handlers(self) -> None:
        self.register("boost_close", self._close)
        self.register("boost_back", self._back)
        self.register("boost_prev", self._prev)
        self.register("boost_next", self._next)
        self.register("boost_pick_register_owner", self._pick_register_owner)
        self.register("boost_by_id_register_owner", self._by_id_register_owner)

    async def _close(self, interaction: discord.Interaction) -> None:
        self.disable_all_items()
        await interaction.edit(view=self)
        self.stop()

    async def _back(self, interaction: discord.Interaction) -> None:
        if self.screen is _Screen.REGISTER_OWNER:
            self.screen = _Screen.REGISTER
        else:
            self.screen = _Screen.HOME
        self.role_id = None
        self.page = 0
        await self.rerender(interaction)

    async def _prev(self, interaction: discord.Interaction) -> None:
        if self.page > 0:
            self.page -= 1
        await self.rerender(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        if self.page < self._total_pages() - 1:
            self.page += 1
        await self.rerender(interaction)

    def _total_pages(self) -> int:
        raise NotImplementedError

    # -- small helpers ------------------------------------------------------

    async def _ack(self, interaction: discord.Interaction) -> None:
        """Acknowledge the component interaction immediately.

        Handlers below perform slow IO (role edits, channel logs, DMs) before
        their first ``edit``; Discord only accepts the initial response within
        3s or the token is invalidated with ``Unknown interaction`` (10062).
        """
        if not interaction.response.is_done():
            await interaction.response.defer()

    def _uid(self) -> int:
        """The id of the menu owner (whoever opened it)."""
        return self.author_id or 0

    async def _t(self, key: str, **kwargs: Any) -> str:
        return await self.bot.translator.t(self.guild_id, key, **kwargs)

    async def _flash(self, key: str, *, color: str = "success", **kwargs: Any) -> None:
        self.flash = await self._t(key, **kwargs)
        self.flash_color = color

    async def _log(self, key: str, *, color: str = "info", **kwargs: Any) -> None:
        """Send a boost action to the configured log channel."""
        description_key = f"boost.logs.{key}.description"
        await send_channel_log(
            self.bot,
            self.guild_id,
            await self._t(f"boost.logs.{key}.title"),
            await text_or(
                self.bot,
                self.guild_id,
                description_key,
                await self._t(description_key, **kwargs),
                **kwargs,
            ),
            color=color,
            card_key=description_key,
        )

    async def _actor_mention(self) -> str:
        return f"<@{self._uid()}>"

    def _container(self, color: str, *parts: discord.ui.ViewItem) -> discord.ui.Container:
        items: list[discord.ui.ViewItem] = list(parts)
        if self.flash:
            items.append(TextDisplay(self.flash))
            color = self.flash_color or color
            self.flash = None
            self.flash_color = "brand"
        return designer_container(self.bot.theme.color(color), *items)

    async def _resolve_role(self, interaction: discord.Interaction) -> discord.Role | None:
        """Resolve the guild role from a native role-select interaction."""
        guild = interaction.guild or self.bot.get_guild(self.guild_id)
        if guild is None:
            return None
        values = (interaction.data or {}).get("values") or []
        if not values:
            return None
        try:
            role_id = int(values[0])
        except (TypeError, ValueError):
            return None
        return guild.get_role(role_id)

    def _guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.guild_id)

    def _current_role(self) -> discord.Role | None:
        guild = self._guild()
        if guild is None or self.role_id is None:
            return None
        return guild.get_role(self.role_id)

    def _role_by_id(self, raw: str) -> discord.Role | None:
        try:
            role_id = int(raw.strip())
        except ValueError:
            return None
        guild = self._guild()
        if guild is None:
            return None
        return guild.get_role(role_id)

    def _member(self, member_id: int) -> discord.Member | None:
        guild = self._guild()
        if guild is None:
            return None
        return guild.get_member(member_id)

    async def _dm_transferred(self, role: discord.Role, member: discord.Member) -> None:
        data = await self.store.get_role(self.guild_id, role.id) or {}
        max_members = await get_setting(self.bot.storage, self.guild_id, "boost.max_members")
        await dm.send(
            self.bot,
            member,
            self.guild_id,
            "boost.dm.transferred",
            mention=member.mention,
            role_name=role.name,
            server_name=role.guild.name,
            max_members=max_members,
        )
        await dm.send_preview(self.bot, member, self.guild_id, role, data)

    async def _dm_owner_registered(self, role: discord.Role, member: discord.Member) -> None:
        data = await self.store.get_role(self.guild_id, role.id) or {}
        max_members = await get_setting(self.bot.storage, self.guild_id, "boost.max_members")
        await dm.send(
            self.bot,
            member,
            self.guild_id,
            "boost.dm.registered",
            mention=member.mention,
            role_name=role.name,
            server_name=role.guild.name,
            max_members=max_members,
        )
        await dm.send_preview(self.bot, member, self.guild_id, role, data)

    # -- register owner selection -------------------------------------------

    async def _pick_register_owner(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        try:
            member_id = int(values[0])
        except (TypeError, ValueError):
            return
        await self._finalize_register(interaction, member_id)

    async def _by_id_register_owner(self, interaction: discord.Interaction) -> None:
        modal = make_by_id_modal(
            title=await self._t("boost.modals.register_owner.title"),
            custom_id=f"boost_by_id_register_owner_modal:{interaction.id}",
            label=await self._t("boost.modals.register_owner.label"),
            placeholder=await self._t("boost.modals.register_owner.placeholder"),
            on_submit=self._register_owner_by_id,
        )
        await interaction.response.send_modal(modal)

    async def _register_owner_by_id(self, interaction: discord.Interaction, value: str) -> None:
        try:
            member_id = int(value.strip())
        except ValueError:
            await self._flash("boost.errors.member_not_found", color="danger")
            return await interaction.edit(view=self)
        await self._finalize_register(interaction, member_id)

    async def _finalize_register(self, interaction: discord.Interaction, member_id: int) -> None:
        await self._ack(interaction)
        role = self._current_role()
        member = self._member(member_id)
        if role is None or member is None:
            await self._flash("boost.errors.member_not_found", color="danger")
            return await self.rerender(interaction)
        if member.bot:
            await self._flash("boost.errors.bot_not_allowed", color="danger")
            return await self.rerender(interaction)
        if await self.store.role_exists(self.guild_id, role.id):
            await self._flash("boost.errors.role_already_registered", color="danger")
            return await self.rerender(interaction)
        try:
            await role.edit(mentionable=True)
        except discord.Forbidden:
            await self._flash("boost.errors.forbidden_manage_role", color="danger")
            return await self.rerender(interaction)
        except discord.HTTPException:
            pass
        await self.store.add_role(self.guild_id, role.id, member.id, interaction.user.id)
        await self._log(
            "register",
            color="success",
            role=f"<@&{role.id}>",
            owner=member.mention,
            actor=await self._actor_mention(),
        )
        with contextlib.suppress(discord.Forbidden):
            await self._dm_owner_registered(role, member)
        self.role_id = None
        self.screen = _Screen.HOME
        await self._flash("boost.success.role_registered", role=f"@{role.name}")
        await self.rerender(interaction)

    async def _build_register_owner(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        role = self._current_role()
        parts: list[discord.ui.ViewItem] = [
            TextDisplay(
                theme.md("title", title=await t(self.guild_id, "boost.titles.register_owner"))
            ),
        ]
        if role is None:
            parts.append(TextDisplay(await t(self.guild_id, "boost.errors.role_not_found")))
            return [
                self._container("danger", *parts),
                ActionRow(
                    self.make_button(
                        custom_id="boost_back",
                        label=await t(self.guild_id, "boost.buttons.back"),
                    )
                ),
            ]
        parts.append(
            TextDisplay(
                await t(
                    self.guild_id,
                    "boost.descriptions.register_owner",
                    role=f"@{role.name}",
                )
            )
        )
        container = self._container(
            "brand",
            *self._role_items(
                role,
                *parts,
            ),
        )
        member_select = self.make_user_select(
            custom_id="boost_pick_register_owner",
            placeholder=await t(self.guild_id, "boost.placeholders.select_member"),
        )
        by_id = self.make_button(
            custom_id="boost_by_id_register_owner",
            label=await t(self.guild_id, "boost.buttons.add_by_id"),
            emoji=theme.emojis.get("numbers", ""),
        )
        back = self.make_button(
            custom_id="boost_back", label=await t(self.guild_id, "boost.buttons.back")
        )
        return [
            container,
            divider(),
            ActionRow(member_select),
            ActionRow(by_id, back),
        ]

    @staticmethod
    def _role_label(role: discord.Role) -> str:
        return f"@{role.name}"

    @staticmethod
    def _role_thumbnail(role: discord.Role) -> discord.ui.Thumbnail | None:
        """Return a small icon thumbnail for the role, if it has a custom icon.

        Thumbnails may only be used as a ``Section`` accessory, so callers must
        pass the result to ``Section(..., accessory=thumbnail)`` — never add it
        as a bare container/section item.
        """
        if role.icon is not None:
            return discord.ui.Thumbnail(role.icon.url)
        return None

    @classmethod
    def _role_items(
        cls, role: discord.Role, *items: discord.ui.ViewItem
    ) -> list[discord.ui.ViewItem]:
        """Return ``items`` for a container, showing the role icon as a ``Section``
        accessory on the first text item when the role has a custom icon."""
        thumbnail = cls._role_thumbnail(role)
        if thumbnail is None or not items:
            return list(items)
        first, *rest = items
        return [Section(first, accessory=thumbnail), *rest]

    @classmethod
    def _role_entry(
        cls,
        role: discord.Role,
        text: discord.ui.ViewItem,
        button: discord.ui.Button,
    ) -> discord.ui.ViewItem:
        """Build one paginated list entry: role text beside a ``Section``
        thumbnail accessory (when the role has a custom icon) and the action
        button below it. A ``Section`` accepts a single accessory, so when both
        an icon and a button are present they are grouped in a ``Container``."""
        thumbnail = cls._role_thumbnail(role)
        if thumbnail is not None:
            return Container(
                Section(text, accessory=thumbnail),
                ActionRow(button),
            )
        return Section(text, accessory=button)


class BoostMenuView(_BaseMenu):
    """Member-facing /boost panel."""

    def _register_handlers(self) -> None:
        super()._register_handlers()
        self.register("boost_my_roles", self._open_my_roles)
        self.register("boost_pending", self._open_pending)
        self.register("boost_leave", self._open_leave)
        self.register("boost_pick_role_leave", self._pick_leave)
        self.register("boost_manage", self._open_manage)
        self.register("boost_manage_members", self._open_members)
        self.register("boost_invites", self._open_invites)
        self.register("boost_rename", self._open_rename)
        self.register("boost_color", self._open_color)
        self.register("boost_emoji", self._open_emoji)
        self.register("boost_icon", self._open_icon)
        self.register("boost_transfer", self._open_transfer)
        self.register("boost_transfer_owner", self._pick_transfer_owner)
        self.register("boost_by_id_transfer_owner", self._by_id_transfer_owner)
        self.register("boost_remove_role", self._open_remove_confirm)
        self.register("boost_remove_confirm", self._remove_confirm)
        self.register("boost_leave_confirm", self._open_leave_confirm)
        self.register("boost_leave_confirm_yes", self._leave_role)
        self.register("boost_pick_color", self._pick_color)
        self.register("boost_custom_color", self._open_custom_color)
        self.register("boost_pick_member", self._add_member)
        self.register("boost_by_id_member", self._by_id_member)
        self.register("boost_member_remove", self._remove_member)
        self.register("boost_add_invite", self._open_invite_target)
        self.register("boost_pick_invitee", self._send_invite)
        self.register("boost_by_id_invitee", self._by_id_invitee)
        self.register("boost_cancel_invite", self._cancel_invite)

    def _total_pages(self) -> int:
        return 1

    # -- navigation ---------------------------------------------------------

    async def _open_my_roles(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.MY_ROLES
        self.page = 0
        await self.rerender(interaction)

    async def _open_pending(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.PENDING
        self.page = 0
        await self.rerender(interaction)

    async def _open_leave(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.LEAVE
        self.page = 0
        await self.rerender(interaction)

    async def _open_manage(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.MANAGE
        self.role_id = int(_suffix(interaction.custom_id, "boost_manage"))
        await self.rerender(interaction)

    async def _open_members(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.MEMBERS
        self.role_id = int(_suffix(interaction.custom_id, "boost_manage_members"))
        self.page = 0
        await self.rerender(interaction)

    async def _open_invites(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.INVITES
        self.role_id = int(_suffix(interaction.custom_id, "boost_invites"))
        self.page = 0
        await self.rerender(interaction)

    async def _open_invite_target(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.INVITE_TARGET
        self.role_id = int(_suffix(interaction.custom_id, "boost_add_invite"))
        self._transfer_mode = False
        await self.rerender(interaction)

    async def _open_transfer(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.INVITE_TARGET
        self.role_id = int(_suffix(interaction.custom_id, "boost_transfer"))
        self._transfer_mode = True
        await self.rerender(interaction)

    async def _open_remove_confirm(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.REMOVE_CONFIRM
        self.role_id = int(_suffix(interaction.custom_id, "boost_remove_role"))
        await self.rerender(interaction)

    async def _open_leave_confirm(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.LEAVE_CONFIRM
        self.role_id = int(_suffix(interaction.custom_id, "boost_leave_confirm"))
        await self.rerender(interaction)

    # -- leave ---------------------------------------------------------------

    async def _pick_leave(self, interaction: discord.Interaction) -> None:
        role = await self._resolve_role(interaction)
        if role is None:
            await self._flash("boost.errors.role_not_found", color="danger")
            return await self.rerender(interaction)
        self.role_id = role.id
        self.screen = _Screen.LEAVE_CONFIRM
        await self.rerender(interaction)

    async def _leave_role(self, interaction: discord.Interaction) -> None:
        await self._ack(interaction)
        role_id = int(_suffix(interaction.custom_id, "boost_leave_confirm_yes"))
        guild = self._guild()
        member = guild.get_member(interaction.user.id) if guild else None
        role = guild.get_role(role_id) if guild else None
        data = await self.store.get_role(self.guild_id, role_id)
        if member is None or role is None or data is None:
            await self._flash("boost.errors.generic_error", color="danger")
            return await self.rerender(interaction)
        if member.id == data.get("owner_id"):
            await self._flash("boost.errors.cannot_leave_as_owner", color="danger")
            return await self.rerender(interaction)
        try:
            await member.remove_roles(role, reason="Left boost role")
        except discord.Forbidden:
            await self._flash("boost.errors.forbidden_remove_role", color="danger")
            return await self.rerender(interaction)
        await self.store.remove_member(self.guild_id, role_id, member.id)
        owner = guild.get_member(data.get("owner_id"))
        await self._log(
            "member_left",
            color="warning",
            role=f"<@&{role_id}>",
            member=member.mention,
            owner=owner.mention if owner is not None else "-",
        )
        if owner is not None and owner.id != member.id:
            with contextlib.suppress(discord.Forbidden):
                await dm.send(
                    self.bot,
                    owner,
                    self.guild_id,
                    "boost.dm.owner_member_left",
                    member_mention=member.mention,
                    role_name=role.name,
                )
        with contextlib.suppress(discord.Forbidden):
            await dm.send(
                self.bot,
                member,
                self.guild_id,
                "boost.dm.member_left",
                role_name=role.name,
            )
        self.screen = _Screen.HOME
        await self._flash("boost.success.left_role", role=f"@{role.name}")
        await self.rerender(interaction)

    # -- management ---------------------------------------------------------

    async def _open_rename(self, interaction: discord.Interaction) -> None:
        self.role_id = int(_suffix(interaction.custom_id, "boost_rename"))
        role = self._current_role()
        modal = make_text_modal(
            title=await self._t("boost.modals.rename.title"),
            custom_id=f"boost_rename_modal:{self.role_id}",
            label=await self._t("boost.modals.rename.label"),
            placeholder=await self._t("boost.modals.rename.placeholder"),
            value=role.name if role else "",
            max_length=100,
            on_submit=self._rename_submit,
        )
        await interaction.response.send_modal(modal)

    async def _rename_submit(self, interaction: discord.Interaction, value: str) -> None:
        name = value.strip()
        if not name:
            await self._flash("boost.errors.empty_name", color="danger")
            return await interaction.edit(view=self)
        role = self._current_role()
        if role is None:
            await self._flash("boost.errors.role_not_found", color="danger")
            return await interaction.edit(view=self)
        old = role.name
        try:
            await role.edit(name=name, reason="Boost role rename")
        except discord.Forbidden:
            await self._flash("boost.errors.forbidden_manage_role", color="danger")
            return await interaction.edit(view=self)
        except discord.HTTPException as exc:
            log.error("Role rename failed: %s", exc)
            await self._flash("boost.errors.generic_error", color="danger")
            return await interaction.edit(view=self)
        await self._log(
            "rename",
            color="brand",
            role=f"<@&{role.id}>",
            old_name=old,
            new_name=name,
            actor=await self._actor_mention(),
        )
        await self._flash("boost.success.role_renamed", old=old, new=name)
        await interaction.edit(view=self)

    async def _open_emoji(self, interaction: discord.Interaction) -> None:
        self.role_id = int(_suffix(interaction.custom_id, "boost_emoji"))
        modal = make_text_modal(
            title=await self._t("boost.modals.emoji.title"),
            custom_id=f"boost_emoji_modal:{self.role_id}",
            label=await self._t("boost.modals.emoji.label"),
            placeholder=await self._t("boost.modals.emoji.placeholder"),
            max_length=32,
            on_submit=self._emoji_submit,
        )
        await interaction.response.send_modal(modal)

    async def _emoji_submit(self, interaction: discord.Interaction, value: str) -> None:
        emoji = value.strip()
        if not _EMOJI_RE.match(emoji):
            await self._flash("boost.errors.invalid_emoji", color="danger")
            return await interaction.edit(view=self)
        role = self._current_role()
        if role is None:
            await self._flash("boost.errors.role_not_found", color="danger")
            return await interaction.edit(view=self)
        try:
            await role.edit(unicode_emoji=emoji, reason="Boost role emoji")
        except discord.Forbidden:
            await self._flash("boost.errors.requires_boost_level_2", color="danger")
            return await interaction.edit(view=self)
        except discord.HTTPException as exc:
            log.error("Role emoji edit failed: %s", exc)
            await self._flash("boost.errors.invalid_emoji", color="danger")
            return await interaction.edit(view=self)
        await self._log(
            "emoji_change",
            color="brand",
            role=f"<@&{role.id}>",
            emoji=emoji,
            actor=await self._actor_mention(),
        )
        await self._flash("boost.success.emoji_set", emoji=emoji)
        await interaction.edit(view=self)

    async def _open_icon(self, interaction: discord.Interaction) -> None:
        self.role_id = int(_suffix(interaction.custom_id, "boost_icon"))
        modal = make_file_modal(
            title=await self._t("boost.modals.icon.title"),
            custom_id=f"boost_icon_modal:{self.role_id}",
            label=await self._t("boost.modals.icon.label"),
            description=await self._t("boost.modals.icon.description"),
            on_submit=self._icon_submit,
        )
        await interaction.response.send_modal(modal)

    async def _icon_submit(
        self, interaction: discord.Interaction, attachments: list[discord.Attachment]
    ) -> None:
        if not attachments:
            await self._flash("boost.errors.invalid_image", color="danger")
            return await interaction.edit(view=self)
        attachment = attachments[0]
        if not (attachment.content_type or "").startswith("image/"):
            await self._flash("boost.errors.invalid_image", color="danger")
            return await interaction.edit(view=self)
        try:
            data = await attachment.read()
        except Exception as exc:
            log.error("Icon download failed: %s", exc)
            await self._flash("boost.errors.image_download_failed", color="danger")
            return await interaction.edit(view=self)
        if len(data) > 1_048_576:
            await self._flash("boost.errors.icon_too_large", color="danger")
            return await interaction.edit(view=self)
        role = self._current_role()
        if role is None:
            await self._flash("boost.errors.role_not_found", color="danger")
            return await interaction.edit(view=self)
        try:
            await role.edit(icon=data, reason="Boost role icon")
        except discord.Forbidden:
            await self._flash("boost.errors.requires_boost_level_2", color="danger")
            return await interaction.edit(view=self)
        except discord.HTTPException as exc:
            log.error("Role icon edit failed: %s", exc)
            await self._flash("boost.errors.generic_error", color="danger")
            return await interaction.edit(view=self)
        await self._log(
            "icon",
            color="brand",
            role=f"<@&{role.id}>",
            actor=await self._actor_mention(),
        )
        await self._flash("boost.success.icon_set")
        await interaction.edit(view=self)

    async def _open_color(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.COLOR
        self.role_id = int(_suffix(interaction.custom_id, "boost_color"))
        await self.rerender(interaction)

    async def _pick_color(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        hex_value = values[0]
        role = self._current_role()
        if role is None:
            await self._flash("boost.errors.role_not_found", color="danger")
            return await self.rerender(interaction)
        try:
            await role.edit(colour=discord.Colour(int(hex_value, 16)), reason="Boost role color")
        except discord.Forbidden:
            await self._flash("boost.errors.forbidden_manage_role", color="danger")
            return await self.rerender(interaction)
        await self._log(
            "color_change",
            color="brand",
            role=f"<@&{role.id}>",
            new_color=f"#{hex_value.upper()}",
            actor=await self._actor_mention(),
        )
        self.screen = _Screen.MANAGE
        await self._flash("boost.success.color_changed")
        await self.rerender(interaction)

    async def _open_custom_color(self, interaction: discord.Interaction) -> None:
        self.role_id = int(_suffix(interaction.custom_id, "boost_custom_color"))
        modal = make_text_modal(
            title=await self._t("boost.modals.custom_color.title"),
            custom_id=f"boost_custom_color_modal:{self.role_id}",
            label=await self._t("boost.modals.custom_color.label"),
            placeholder=await self._t("boost.modals.custom_color.placeholder"),
            max_length=7,
            on_submit=self._custom_color_submit,
        )
        await interaction.response.send_modal(modal)

    async def _custom_color_submit(self, interaction: discord.Interaction, value: str) -> None:
        match = _HEX_RE.match(value.strip())
        if match is None:
            await self._flash("boost.errors.invalid_hex_color", color="danger")
            return await interaction.edit(view=self)
        role = self._current_role()
        if role is None:
            await self._flash("boost.errors.role_not_found", color="danger")
            return await interaction.edit(view=self)
        try:
            await role.edit(
                colour=discord.Colour(int(match.group(1), 16)), reason="Boost role color"
            )
        except discord.Forbidden:
            await self._flash("boost.errors.forbidden_manage_role", color="danger")
            return await interaction.edit(view=self)
        await self._log(
            "color_change",
            color="brand",
            role=f"<@&{role.id}>",
            new_color=f"#{match.group(1).upper()}",
            actor=await self._actor_mention(),
        )
        await self._flash("boost.success.color_changed")
        await interaction.edit(view=self)

    async def _add_member(self, interaction: discord.Interaction) -> None:
        role_id = int(_suffix(interaction.custom_id, "boost_pick_member"))
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        try:
            member_id = int(values[0])
        except (TypeError, ValueError):
            return
        await self._add_member_by_id(interaction, role_id, member_id)

    async def _by_id_member(self, interaction: discord.Interaction) -> None:
        role_id = int(_suffix(interaction.custom_id, "boost_by_id_member"))
        modal = make_by_id_modal(
            title=await self._t("boost.modals.add_member.title"),
            custom_id=f"boost_by_id_member_modal:{role_id}:{interaction.id}",
            label=await self._t("boost.modals.add_member.label"),
            placeholder=await self._t("boost.modals.add_member.placeholder"),
            on_submit=self._member_by_id_submit,
        )
        await interaction.response.send_modal(modal)

    async def _member_by_id_submit(self, interaction: discord.Interaction, value: str) -> None:
        try:
            member_id = int(value.strip())
        except ValueError:
            await self._flash("boost.errors.member_not_found", color="danger")
            return await interaction.edit(view=self)
        role_id = int(_suffix(interaction.custom_id, "boost_by_id_member_modal").split(":")[0])
        await self._add_member_by_id(interaction, role_id, member_id)

    async def _add_member_by_id(
        self, interaction: discord.Interaction, role_id: int, member_id: int
    ) -> None:
        await self._ack(interaction)
        role = self._guild().get_role(role_id) if self._guild() else None
        member = self._member(member_id)
        if role is None or member is None:
            await self._flash("boost.errors.member_not_found", color="danger")
            return await self.rerender(interaction)
        data = await self.store.get_role(self.guild_id, role_id)
        if data is None:
            await self._flash("boost.errors.role_not_registered", color="danger")
            return await self.rerender(interaction)
        if member.id == data.get("owner_id"):
            await self._flash("boost.errors.member_is_owner", color="danger")
            return await self.rerender(interaction)
        if member.id in data.get("members", []):
            await self._flash("boost.errors.member_already_added", color="danger")
            return await self.rerender(interaction)
        max_members = await get_setting(self.bot.storage, self.guild_id, "boost.max_members")
        if len(data.get("members", [])) >= max_members:
            await self._flash(
                "boost.errors.max_members_reached", max_members=max_members, color="danger"
            )
            return await self.rerender(interaction)
        try:
            await member.add_roles(role, reason="Added to boost role")
        except discord.Forbidden:
            await self._flash("boost.errors.forbidden_add_role", color="danger")
            return await self.rerender(interaction)
        if not await self.store.add_member(self.guild_id, role_id, member.id, max_members):
            await self._flash(
                "boost.errors.max_members_reached", max_members=max_members, color="danger"
            )
            return await self.rerender(interaction)
        await self._log(
            "member_add",
            color="success",
            role=f"<@&{role_id}>",
            member=member.mention,
            actor=await self._actor_mention(),
        )
        await self._flash("boost.success.member_added", member=member.mention, role=f"@{role.name}")
        await self.rerender(interaction)

    async def _remove_member(self, interaction: discord.Interaction) -> None:
        await self._ack(interaction)
        _, role_id, member_id = interaction.custom_id.split(":")
        role_id = int(role_id)
        member_id = int(member_id)
        role = self._guild().get_role(role_id) if self._guild() else None
        member = self._member(member_id)
        if role is not None and member is not None:
            try:
                await member.remove_roles(role, reason="Removed from boost role")
            except discord.Forbidden:
                await self._flash("boost.errors.forbidden_remove_role", color="danger")
                return await self.rerender(interaction)
        await self.store.remove_member(self.guild_id, role_id, member_id)
        await self._log(
            "member_remove",
            color="danger",
            role=f"<@&{role_id}>",
            member=f"<@{member_id}>",
            actor=await self._actor_mention(),
        )
        self.screen = _Screen.MEMBERS
        self.role_id = role_id
        await self._flash("boost.success.member_removed", member=f"<@{member_id}>")
        await self.rerender(interaction)

    async def _send_invite(self, interaction: discord.Interaction) -> None:
        role_id = int(_suffix(interaction.custom_id, "boost_pick_invitee"))
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        try:
            member_id = int(values[0])
        except (TypeError, ValueError):
            return
        await self._send_invite_to(interaction, role_id, member_id)

    async def _by_id_invitee(self, interaction: discord.Interaction) -> None:
        role_id = int(_suffix(interaction.custom_id, "boost_by_id_invitee"))
        modal = make_by_id_modal(
            title=await self._t("boost.modals.add_member.title"),
            custom_id=f"boost_by_id_invitee_modal:{role_id}:{interaction.id}",
            label=await self._t("boost.modals.add_member.label"),
            placeholder=await self._t("boost.modals.add_member.placeholder"),
            on_submit=self._invitee_by_id_submit,
        )
        await interaction.response.send_modal(modal)

    async def _invitee_by_id_submit(self, interaction: discord.Interaction, value: str) -> None:
        try:
            member_id = int(value.strip())
        except ValueError:
            await self._flash("boost.errors.member_not_found", color="danger")
            return await interaction.edit(view=self)
        role_id = int(_suffix(interaction.custom_id, "boost_by_id_invitee_modal").split(":")[0])
        await self._send_invite_to(interaction, role_id, member_id)

    async def _send_invite_to(
        self, interaction: discord.Interaction, role_id: int, member_id: int
    ) -> None:
        await self._ack(interaction)
        role = self._guild().get_role(role_id) if self._guild() else None
        member = self._member(member_id)
        if role is None or member is None:
            await self._flash("boost.errors.member_not_found", color="danger")
            return await self.rerender(interaction)
        data = await self.store.get_role(self.guild_id, role_id)
        if data is None:
            await self._flash("boost.errors.role_not_registered", color="danger")
            return await self.rerender(interaction)
        if member.id == data.get("owner_id"):
            await self._flash("boost.errors.member_is_owner", color="danger")
            return await self.rerender(interaction)
        if member.id in data.get("members", []):
            await self._flash("boost.errors.member_already_added", color="danger")
            return await self.rerender(interaction)
        if await self.store.has_pending_invite(self.guild_id, role_id, member.id):
            await self._flash("boost.errors.member_already_invited", color="danger")
            return await self.rerender(interaction)
        max_invites = await get_setting(
            self.bot.storage, self.guild_id, "boost.max_pending_invites"
        )
        if await self.store.pending_invite_count(self.guild_id, role_id) >= max_invites:
            await self._flash(
                "boost.errors.max_pending_invites", max_invites=max_invites, color="danger"
            )
            return await self.rerender(interaction)
        invite_id = f"{role_id}-{member_id}-{interaction.id}"
        expires_at = datetime.now(UTC) + timedelta(
            seconds=await get_setting(self.bot.storage, self.guild_id, "boost.invite_seconds")
        )
        try:
            await self._dm_invite(role, member, data, invite_id, expires_at)
        except discord.Forbidden:
            await self._flash("boost.errors.dm_failed", color="danger")
            return await self.rerender(interaction)
        await self.store.add_invite(
            self.guild_id, invite_id, role_id, interaction.user.id, member.id, expires_at
        )
        await self._log(
            "invite_sent",
            color="info",
            role=f"<@&{role_id}>",
            invitee=member.mention,
            inviter=await self._actor_mention(),
            expires=f"<t:{int(expires_at.timestamp())}:R>",
        )
        self.screen = _Screen.INVITES
        self.role_id = role_id
        await self._flash("boost.success.invite_sent", member=member.mention)
        await self.rerender(interaction)

    async def _dm_invite(
        self,
        role: discord.Role,
        member: discord.Member,
        data: dict[str, Any],
        invite_id: str,
        expires_at: datetime,
    ) -> None:
        from rosemary.ui.boost_invite import BoostInviteView

        guild = role.guild
        inviter_mention = f"<@{data.get('owner_id', 0)}>"
        expires = f"<t:{int(expires_at.timestamp())}:R>"
        invite_key = "boost.dm.invite"
        content = await text_or(
            self.bot,
            self.guild_id,
            invite_key,
            await self._t(
                invite_key,
                mention=member.mention,
                server_name=guild.name,
                role_name=role.name,
                inviter_mention=inviter_mention,
                expires=expires,
            ),
            mention=member.mention,
            server_name=guild.name,
            role_name=role.name,
            inviter_mention=inviter_mention,
            expires=expires,
        )
        view = BoostInviteView(
            self.bot,
            guild_id=guild.id,
            invite_id=invite_id,
            accept_label=await self._t("boost.buttons.accept_invite"),
            decline_label=await self._t("boost.buttons.decline_invite"),
        )
        await member.send(content=content)
        await member.send(view=view)
        await dm.send_preview(self.bot, member, guild.id, role, data)

    async def _cancel_invite(self, interaction: discord.Interaction) -> None:
        await self._ack(interaction)
        parts = interaction.custom_id.split(":")
        role_id = int(parts[1])
        invite_id = parts[2]
        await self.store.remove_invite(self.guild_id, invite_id)
        await self._log(
            "invite_cancelled",
            color="warning",
            role=f"<@&{role_id}>",
            invitee=f"<@{invite_id.split('-')[1]}>" if "-" in invite_id else "-",
            actor=await self._actor_mention(),
        )
        self.screen = _Screen.INVITES
        self.role_id = role_id
        await self._flash("boost.success.invite_cancelled")
        await self.rerender(interaction)

    async def _remove_confirm(self, interaction: discord.Interaction) -> None:
        await self._ack(interaction)
        _, role_id, raw = interaction.custom_id.split(":", 2)
        role_id = int(role_id)
        role = self._guild().get_role(role_id) if self._guild() else None
        data = await self.store.get_role(self.guild_id, role_id) or {}
        await self.store.remove_role(self.guild_id, role_id)
        for invite_id in list(
            (await self.store.get_invites_by_role(self.guild_id, role_id)).keys()
        ):
            await self.store.remove_invite(self.guild_id, invite_id)
        if raw == "delete" and role is not None:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await role.delete(reason="Boost role removed")
        await self._log(
            "remove",
            color="danger",
            role=f"<@&{role_id}>",
            owner=f"<@{data.get('owner_id', 0)}>",
            actor=await self._actor_mention(),
            deleted=await self._t(
                "boost.buttons.delete_role_yes"
                if raw == "delete"
                else "boost.buttons.delete_role_no"
            ),
        )
        self.screen = _Screen.HOME
        await self._flash("boost.success.role_removed")
        await self.rerender(interaction)

    async def _pick_transfer_owner(self, interaction: discord.Interaction) -> None:
        role_id = int(_suffix(interaction.custom_id, "boost_transfer_owner"))
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        try:
            member_id = int(values[0])
        except (TypeError, ValueError):
            return
        await self._transfer_to(interaction, role_id, member_id)

    async def _by_id_transfer_owner(self, interaction: discord.Interaction) -> None:
        role_id = int(_suffix(interaction.custom_id, "boost_by_id_transfer_owner"))
        modal = make_by_id_modal(
            title=await self._t("boost.modals.transfer.title"),
            custom_id=f"boost_by_id_transfer_owner_modal:{role_id}:{interaction.id}",
            label=await self._t("boost.modals.transfer.label"),
            placeholder=await self._t("boost.modals.transfer.placeholder"),
            on_submit=self._transfer_owner_by_id,
        )
        await interaction.response.send_modal(modal)

    async def _transfer_owner_by_id(self, interaction: discord.Interaction, value: str) -> None:
        try:
            member_id = int(value.strip())
        except ValueError:
            await self._flash("boost.errors.member_not_found", color="danger")
            return await interaction.edit(view=self)
        role_id = int(
            _suffix(interaction.custom_id, "boost_by_id_transfer_owner_modal").split(":")[0]
        )
        await self._transfer_to(interaction, role_id, member_id)

    async def _transfer_to(
        self, interaction: discord.Interaction, role_id: int, member_id: int
    ) -> None:
        await self._ack(interaction)
        role = self._guild().get_role(role_id) if self._guild() else None
        member = self._member(member_id)
        if role is None or member is None:
            await self._flash("boost.errors.member_not_found", color="danger")
            return await self.rerender(interaction)
        if member.bot:
            await self._flash("boost.errors.bot_not_allowed", color="danger")
            return await self.rerender(interaction)
        data = await self.store.get_role(self.guild_id, role_id) or {}
        if not await self.store.transfer_ownership(self.guild_id, role_id, member.id):
            await self._flash("boost.errors.role_not_registered", color="danger")
            return await self.rerender(interaction)
        await self._log(
            "transfer",
            color="info",
            role=f"<@&{role_id}>",
            old_owner=f"<@{data.get('owner_id', 0)}>",
            new_owner=member.mention,
            actor=await self._actor_mention(),
        )
        with contextlib.suppress(discord.Forbidden):
            await self._dm_transferred(role, member)
        self.screen = _Screen.HOME
        await self._flash("boost.success.ownership_transferred", role=f"@{role.name}")
        await self.rerender(interaction)

    # -- rendering ----------------------------------------------------------

    async def build_items(self) -> list[discord.ui.ViewItem]:
        t = self.bot.translator.t
        theme = self.bot.theme
        screen = self.screen

        if screen is _Screen.LEAVE:
            return await self._build_leave(t, theme)
        if screen is _Screen.MY_ROLES:
            return await self._build_my_roles(t, theme)
        if screen is _Screen.PENDING:
            return await self._build_pending(t, theme)
        if screen is _Screen.MANAGE:
            return await self._build_manage(t, theme)
        if screen is _Screen.COLOR:
            return await self._build_color(t, theme)
        if screen is _Screen.MEMBERS:
            return await self._build_members(t, theme)
        if screen is _Screen.INVITES:
            return await self._build_invites(t, theme)
        if screen is _Screen.INVITE_TARGET:
            return await self._build_invite_target(t, theme)
        if screen is _Screen.LEAVE_CONFIRM:
            return await self._build_leave_confirm(t, theme)
        if screen is _Screen.REMOVE_CONFIRM:
            return await self._build_remove_confirm(t, theme)
        return await self._build_home(t, theme)

    async def _build_home(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        container = self._container(
            "brand",
            TextDisplay(theme.md("title", title=await t(self.guild_id, "boost.titles.home"))),
            TextDisplay(await t(self.guild_id, "boost.descriptions.user_panel")),
        )
        row = ActionRow(
            self.make_button(
                custom_id="boost_my_roles",
                label=await t(self.guild_id, "boost.buttons.my_roles"),
                style=discord.ButtonStyle.primary,
                emoji=theme.emojis.get("crown", ""),
            ),
            self.make_button(
                custom_id="boost_pending",
                label=await t(self.guild_id, "boost.buttons.invites"),
                emoji=theme.emojis.get("love_letter", ""),
            ),
            self.make_button(
                custom_id="boost_leave",
                label=await t(self.guild_id, "boost.buttons.leave_role"),
                emoji=theme.emojis.get("door", ""),
            ),
        )
        row2 = ActionRow(
            self.make_button(
                custom_id="boost_close",
                label=await t(self.guild_id, "boost.buttons.close"),
                style=discord.ButtonStyle.danger,
            )
        )
        return [container, row, row2]

    async def _build_my_roles(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        uid = self._uid()
        owned = await self.store.get_roles_by_owner(self.guild_id, uid)
        joined = await self.store.get_roles_as_member(self.guild_id, uid)
        guild = self._guild()
        entries: list[discord.ui.ViewItem] = []
        for role_id_str, _data in owned.items():
            role = guild.get_role(int(role_id_str)) if guild else None
            if role is None:
                continue
            entries.append(
                self._role_entry(
                    role,
                    TextDisplay(
                        theme.md(
                            "entry",
                            label=self._role_label(role),
                            value=await t(self.guild_id, "boost.descriptions.as_owner"),
                        )
                    ),
                    self.make_button(
                        custom_id=f"boost_manage:{role_id_str}",
                        label=await t(self.guild_id, "boost.buttons.manage_roles"),
                        emoji=theme.emojis.get("gear", ""),
                    ),
                )
            )
        for role_id_str, _data in joined.items():
            role = guild.get_role(int(role_id_str)) if guild else None
            if role is None:
                continue
            entries.append(
                self._role_entry(
                    role,
                    TextDisplay(
                        theme.md(
                            "entry",
                            label=self._role_label(role),
                            value=await t(self.guild_id, "boost.descriptions.as_member"),
                        )
                    ),
                    self.make_button(
                        custom_id=f"boost_leave_confirm:{role_id_str}",
                        label=await t(self.guild_id, "boost.buttons.leave_role"),
                        emoji=theme.emojis.get("door", ""),
                    ),
                )
            )
        parts: list[discord.ui.ViewItem] = [
            TextDisplay(theme.md("title", title=await t(self.guild_id, "boost.titles.my_roles"))),
        ]
        if not entries:
            parts.append(TextDisplay(await t(self.guild_id, "boost.descriptions.my_roles_empty")))
        container = self._container("brand", *parts)
        return await self._paginated_screen(t, container, entries)

    async def _build_pending(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        uid = self._uid()
        sent = await self.store.get_invites_by_inviter(self.guild_id, uid)
        guild = self._guild()
        entries: list[discord.ui.ViewItem] = []
        for invite_id, data in sent.items():
            role = guild.get_role(data.get("role_id", 0)) if guild else None
            if role is None:
                continue
            expires = parse_datetime(data.get("expires_at"))
            entries.append(
                self._role_entry(
                    role,
                    TextDisplay(
                        theme.md(
                            "entry",
                            label=self._role_label(role)
                            if role
                            else str(data.get("role_id")),
                            value=await t(
                                self.guild_id,
                                "boost.descriptions.pending_invite_entry",
                                invitee=f"<@{data.get('invitee_id', 0)}>",
                                expires=f"<t:{int(expires.timestamp())}:R>"
                                if expires
                                else "-",
                            ),
                        )
                    ),
                    self.make_button(
                        custom_id=f"boost_cancel_invite:{data.get('role_id', 0)}:{invite_id}",
                        label=await t(self.guild_id, "boost.buttons.cancel_invite"),
                        style=discord.ButtonStyle.danger,
                        emoji=theme.emojis.get("trash", ""),
                    ),
                )
            )
        parts: list[discord.ui.ViewItem] = [
            TextDisplay(
                theme.md("title", title=await t(self.guild_id, "boost.titles.pending_invites"))
            ),
        ]
        if not entries:
            parts.append(
                TextDisplay(await t(self.guild_id, "boost.descriptions.pending_invites_empty"))
            )
        container = self._container("info", *parts)
        return await self._paginated_screen(t, container, entries)

    async def _build_leave(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        joined = await self.store.get_roles_as_member(self.guild_id, self._uid())
        guild = self._guild()
        entries: list[discord.ui.ViewItem] = []
        for role_id_str, data in joined.items():
            role = guild.get_role(int(role_id_str)) if guild else None
            if role is None:
                continue
            entries.append(
                self._role_entry(
                    role,
                    TextDisplay(
                        theme.md(
                            "entry",
                            label=self._role_label(role),
                            value=f"<@{data.get('owner_id', 0)}>",
                        )
                    ),
                    self.make_button(
                        custom_id=f"boost_leave_confirm:{role_id_str}",
                        label=await t(self.guild_id, "boost.buttons.leave_role"),
                        emoji=theme.emojis.get("door", ""),
                    ),
                )
            )
        parts: list[discord.ui.ViewItem] = [
            TextDisplay(theme.md("title", title=await t(self.guild_id, "boost.titles.leave"))),
            TextDisplay(await t(self.guild_id, "boost.descriptions.leave")),
        ]
        if not entries:
            parts.append(TextDisplay(await t(self.guild_id, "boost.descriptions.leave_empty")))
        container = self._container("warning", *parts)
        return await self._paginated_screen(t, container, entries)

    async def _build_manage(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        role = self._current_role()
        if role is None:
            self.screen = _Screen.HOME
            return await self._build_home(t, theme)
        data = await self.store.get_role(self.guild_id, role.id) or {}
        members = list(data.get("members", []))
        max_members = await get_setting(self.bot.storage, self.guild_id, "boost.max_members")
        container = self._container(
            "brand",
            *self._role_items(
                role,
                TextDisplay(
                    theme.md(
                        "title",
                        title=await t(
                            self.guild_id,
                            "boost.titles.management_panel",
                            role_name=role.mention,
                        ),
                    )
                ),
                TextDisplay(
                    theme.md(
                        "entry",
                        label=await t(self.guild_id, "boost.emoji.owner"),
                        value=f"<@{data.get('owner_id', 0)}>",
                    )
                ),
                TextDisplay(
                    theme.md(
                        "entry",
                        label=await t(self.guild_id, "boost.emoji.members"),
                        value=await t(
                            self.guild_id,
                            "boost.descriptions.members_count",
                            count=len(members),
                            max_members=max_members,
                        ),
                    )
                ),
                TextDisplay(await t(self.guild_id, "boost.descriptions.manage_hint")),
            ),
        )
        row1 = ActionRow(
            self.make_button(
                custom_id=f"boost_rename:{role.id}",
                label=await t(self.guild_id, "boost.buttons.rename"),
                emoji=theme.emojis.get("pencil", ""),
            ),
            self.make_button(
                custom_id=f"boost_color:{role.id}",
                label=await t(self.guild_id, "boost.buttons.change_color"),
                emoji=theme.emojis.get("palette", ""),
            ),
            self.make_button(
                custom_id=f"boost_emoji:{role.id}",
                label=await t(self.guild_id, "boost.buttons.emoji"),
                emoji=theme.emojis.get("tag", ""),
            ),
            self.make_button(
                custom_id=f"boost_icon:{role.id}",
                label=await t(self.guild_id, "boost.buttons.icon"),
                emoji=theme.emojis.get("frame", ""),
            ),
        )
        row2 = ActionRow(
            self.make_button(
                custom_id=f"boost_manage_members:{role.id}",
                label=await t(self.guild_id, "boost.buttons.manage_members"),
                emoji=theme.emojis.get("members", ""),
            ),
            self.make_button(
                custom_id=f"boost_invites:{role.id}",
                label=await t(self.guild_id, "boost.buttons.invites"),
                emoji=theme.emojis.get("love_letter", ""),
            ),
            self.make_button(
                custom_id=f"boost_transfer:{role.id}",
                label=await t(self.guild_id, "boost.buttons.transfer"),
                emoji=theme.emojis.get("swap", ""),
            ),
            self.make_button(
                custom_id=f"boost_remove_role:{role.id}",
                label=await t(self.guild_id, "boost.buttons.remove"),
                style=discord.ButtonStyle.danger,
                emoji=theme.emojis.get("trash", ""),
            ),
        )
        row3 = ActionRow(
            self.make_button(
                custom_id="boost_back", label=await t(self.guild_id, "boost.buttons.back")
            )
        )
        return [container, row1, row2, row3]

    async def _build_color(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        role_id = self.role_id
        role = self._current_role()
        items: list[discord.ui.ViewItem] = [
            TextDisplay(
                theme.md("title", title=await t(self.guild_id, "boost.titles.select_color"))
            ),
            TextDisplay(await t(self.guild_id, "boost.descriptions.select_color")),
        ]
        if role is not None:
            items = self._role_items(role, *items)
        container = self._container("brand", *items)
        options = [
            discord.SelectOption(
                label=await t(self.guild_id, key),
                value=hex_value,
                emoji=theme.emojis.get("palette", ""),
            )
            for key, hex_value in COLOR_PALETTE
        ]
        color_select = self.make_select(
            custom_id=f"boost_pick_color:{role_id}",
            placeholder=await t(self.guild_id, "boost.placeholders.select_color"),
            options=options,
        )
        custom = self.make_button(
            custom_id=f"boost_custom_color:{role_id}",
            label=await t(self.guild_id, "boost.buttons.custom_color"),
            emoji=theme.emojis.get("palette", ""),
        )
        back = self.make_button(
            custom_id="boost_back", label=await t(self.guild_id, "boost.buttons.back")
        )
        return [
            container,
            divider(),
            ActionRow(color_select),
            ActionRow(custom, back),
        ]

    async def _build_members(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        role = self._current_role()
        if role is None:
            self.screen = _Screen.HOME
            return await self._build_home(t, theme)
        data = await self.store.get_role(self.guild_id, role.id) or {}
        members = list(data.get("members", []))
        max_members = await get_setting(self.bot.storage, self.guild_id, "boost.max_members")
        container = self._container(
            "brand",
            *self._role_items(
                role,
                TextDisplay(
                    theme.md(
                        "title",
                        title=await t(
                            self.guild_id, "boost.titles.manage_members", role_name=role.mention
                        ),
                    )
                ),
                TextDisplay(
                    await t(
                        self.guild_id, "boost.descriptions.manage_members", max_members=max_members
                    )
                ),
            ),
        )
        guild = self._guild()
        entries: list[discord.ui.ViewItem] = []
        for member_id in members:
            member = guild.get_member(member_id) if guild else None
            label = str(member) if member else str(member_id)
            entries.append(
                Section(
                    TextDisplay(theme.md("entry", label=f"<@{member_id}>", value=label)),
                    accessory=self.make_button(
                        custom_id=f"boost_member_remove:{role.id}:{member_id}",
                        label=await t(self.guild_id, "boost.buttons.remove_member"),
                        style=discord.ButtonStyle.danger,
                        emoji=theme.emojis.get("minus", ""),
                    ),
                )
            )
        if not entries:
            entries.append(TextDisplay(await t(self.guild_id, "boost.descriptions.members_empty")))
        items: list[discord.ui.ViewItem] = [container, divider()]
        page_entries = paginate(entries, self.page, _LIST_PAGE_SIZE)
        total_pages = page_count(len(entries), _LIST_PAGE_SIZE)
        items.extend(page_entries)
        items.append(divider())
        member_select = self.make_user_select(
            custom_id=f"boost_pick_member:{role.id}",
            placeholder=await t(self.guild_id, "boost.placeholders.select_member"),
        )
        items.append(ActionRow(member_select))
        nav: list[discord.ui.ViewItem] = [
            self.make_button(
                custom_id=f"boost_by_id_member:{role.id}",
                label=await t(self.guild_id, "boost.buttons.add_by_id"),
                emoji=theme.emojis.get("numbers", ""),
            )
        ]
        if self.page > 0:
            nav.append(
                self.make_button(
                    custom_id="boost_prev", label=await t(self.guild_id, "boost.buttons.previous")
                )
            )
        if self.page < total_pages - 1:
            nav.append(
                self.make_button(
                    custom_id="boost_next", label=await t(self.guild_id, "boost.buttons.next")
                )
            )
        nav.append(
            self.make_button(
                custom_id="boost_back", label=await t(self.guild_id, "boost.buttons.back")
            )
        )
        items.append(ActionRow(*nav))
        return items

    async def _build_invites(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        role = self._current_role()
        if role is None:
            self.screen = _Screen.HOME
            return await self._build_home(t, theme)
        invites = await self.store.get_invites_by_role(self.guild_id, role.id)
        container = self._container(
            "info",
            *self._role_items(
                role,
                TextDisplay(
                    theme.md(
                        "title",
                        title=await t(
                            self.guild_id, "boost.titles.invites", role_name=role.mention
                        ),
                    )
                ),
            ),
        )
        entries: list[discord.ui.ViewItem] = []
        for invite_id, data in invites.items():
            expires = parse_datetime(data.get("expires_at"))
            entries.append(
                Section(
                    TextDisplay(
                        theme.md(
                            "entry",
                            label=await t(self.guild_id, "boost.emoji.user"),
                            value=await t(
                                self.guild_id,
                                "boost.descriptions.pending_invite_entry",
                                invitee=f"<@{data.get('invitee_id', 0)}>",
                                expires=f"<t:{int(expires.timestamp())}:R>" if expires else "-",
                            ),
                        )
                    ),
                    accessory=self.make_button(
                        custom_id=f"boost_cancel_invite:{role.id}:{invite_id}",
                        label=await t(self.guild_id, "boost.buttons.cancel_invite"),
                        style=discord.ButtonStyle.danger,
                        emoji=theme.emojis.get("trash", ""),
                    ),
                )
            )
        if not entries:
            entries.append(
                TextDisplay(await t(self.guild_id, "boost.descriptions.pending_invites_empty"))
            )
        items: list[discord.ui.ViewItem] = [container, divider()]
        page_entries = paginate(entries, self.page, _LIST_PAGE_SIZE)
        total_pages = page_count(len(entries), _LIST_PAGE_SIZE)
        items.extend(page_entries)
        items.append(divider())
        nav: list[discord.ui.ViewItem] = [
            self.make_button(
                custom_id=f"boost_add_invite:{role.id}",
                label=await t(self.guild_id, "boost.buttons.add_member"),
                style=discord.ButtonStyle.success,
                emoji=theme.emojis.get("plus", ""),
            )
        ]
        if self.page > 0:
            nav.append(
                self.make_button(
                    custom_id="boost_prev", label=await t(self.guild_id, "boost.buttons.previous")
                )
            )
        if self.page < total_pages - 1:
            nav.append(
                self.make_button(
                    custom_id="boost_next", label=await t(self.guild_id, "boost.buttons.next")
                )
            )
        nav.append(
            self.make_button(
                custom_id="boost_back", label=await t(self.guild_id, "boost.buttons.back")
            )
        )
        items.append(ActionRow(*nav))
        return items

    async def _build_invite_target(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        role = self._current_role()
        if role is None:
            self.screen = _Screen.HOME
            return await self._build_home(t, theme)
        if getattr(self, "_transfer_mode", False):
            title = await t(self.guild_id, "boost.titles.transfer_target")
            description = await t(self.guild_id, "boost.descriptions.transfer_hint")
            select_id = f"boost_transfer_owner:{role.id}"
            by_id_id = f"boost_by_id_transfer_owner:{role.id}"
        else:
            title = await t(self.guild_id, "boost.titles.invite_target")
            description = await t(self.guild_id, "boost.descriptions.dm_hint")
            select_id = f"boost_pick_invitee:{role.id}"
            by_id_id = f"boost_by_id_invitee:{role.id}"
        container = self._container(
            "info",
            *self._role_items(
                role,
                TextDisplay(theme.md("title", title=title)),
                TextDisplay(description),
            ),
        )
        member_select = self.make_user_select(
            custom_id=select_id,
            placeholder=await t(self.guild_id, "boost.placeholders.select_member"),
        )
        by_id = self.make_button(
            custom_id=by_id_id,
            label=await t(self.guild_id, "boost.buttons.add_by_id"),
            emoji=theme.emojis.get("numbers", ""),
        )
        back = self.make_button(
            custom_id="boost_back", label=await t(self.guild_id, "boost.buttons.back")
        )
        return [
            container,
            divider(),
            ActionRow(member_select),
            ActionRow(by_id, back),
        ]

    async def _build_leave_confirm(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        role = self._current_role()
        role_mention = role.mention if role else str(self.role_id)
        items: list[discord.ui.ViewItem] = [
            TextDisplay(
                theme.md("title", title=await t(self.guild_id, "boost.titles.leave_confirm"))
            ),
            TextDisplay(
                await t(self.guild_id, "boost.descriptions.confirm_leave", role=role_mention)
            ),
        ]
        if role is not None:
            items = self._role_items(role, *items)
        container = self._container("warning", *items)
        row = ActionRow(
            self.make_button(
                custom_id=f"boost_leave_confirm_yes:{self.role_id}",
                label=await t(self.guild_id, "boost.buttons.confirm"),
                style=discord.ButtonStyle.danger,
            ),
            self.make_button(
                custom_id="boost_back", label=await t(self.guild_id, "boost.buttons.cancel")
            ),
        )
        return [container, row]

    async def _build_remove_confirm(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        role = self._current_role()
        role_mention = role.mention if role else str(self.role_id)
        items: list[discord.ui.ViewItem] = [
            TextDisplay(
                theme.md("title", title=await t(self.guild_id, "boost.titles.remove_confirm"))
            ),
            TextDisplay(
                await t(self.guild_id, "boost.descriptions.confirm_removal", role=role_mention)
            ),
            TextDisplay(await t(self.guild_id, "boost.descriptions.remove_hint")),
        ]
        if role is not None:
            items = self._role_items(role, *items)
        container = self._container("danger", *items)
        row = ActionRow(
            self.make_button(
                custom_id=f"boost_remove_confirm:{self.role_id}:delete",
                label=await t(self.guild_id, "boost.buttons.delete_role_yes"),
                style=discord.ButtonStyle.danger,
            ),
            self.make_button(
                custom_id=f"boost_remove_confirm:{self.role_id}:keep",
                label=await t(self.guild_id, "boost.buttons.delete_role_no"),
                style=discord.ButtonStyle.secondary,
            ),
        )
        row2 = ActionRow(
            self.make_button(
                custom_id="boost_back", label=await t(self.guild_id, "boost.buttons.cancel")
            )
        )
        return [container, row, row2]

    async def _paginated_screen(
        self, t: Any, container: discord.ui.Container, entries: list[discord.ui.ViewItem]
    ) -> list[discord.ui.ViewItem]:
        page_entries = paginate(entries, self.page, _LIST_PAGE_SIZE)
        total_pages = page_count(len(entries), _LIST_PAGE_SIZE)
        items: list[discord.ui.ViewItem] = [container, divider()]
        items.extend(page_entries)
        nav: list[discord.ui.ViewItem] = []
        if self.page > 0:
            nav.append(
                self.make_button(
                    custom_id="boost_prev", label=await t(self.guild_id, "boost.buttons.previous")
                )
            )
        if self.page < total_pages - 1:
            nav.append(
                self.make_button(
                    custom_id="boost_next", label=await t(self.guild_id, "boost.buttons.next")
                )
            )
        nav.append(
            self.make_button(
                custom_id="boost_back", label=await t(self.guild_id, "boost.buttons.back")
            )
        )
        items.append(divider("large"))
        items.append(ActionRow(*nav))
        return items


class AdminBoostMenuView(_BaseMenu):
    """Moderation panel for boost roles (/boost_admin)."""

    def _register_handlers(self) -> None:
        super()._register_handlers()
        self.register("boost_admin_register", self._open_register)
        self.register("boost_admin_transfer", self._open_transfer)
        self.register("boost_admin_remove", self._open_remove)
        self.register("boost_admin_list", self._open_list)
        self.register("boost_pick_role_admin", self._pick_admin_role)
        self.register("boost_by_id_admin", self._by_id_admin)
        self.register("boost_transfer_owner", self._pick_transfer_owner)
        self.register("boost_by_id_transfer_owner", self._by_id_transfer_owner)
        self.register("boost_remove_confirm_admin", self._remove_confirm)
        self.register("boost_admin_back", self._admin_back)

    def _total_pages(self) -> int:
        return 1

    async def _open_register(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.REGISTER
        self._admin_action = "register"
        await self.rerender(interaction)

    async def _open_transfer(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.REGISTER
        self._admin_action = "transfer"
        await self.rerender(interaction)

    async def _open_remove(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.REGISTER
        self._admin_action = "remove"
        await self.rerender(interaction)

    async def _open_list(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.MY_ROLES
        self.page = 0
        await self.rerender(interaction)

    async def _admin_back(self, interaction: discord.Interaction) -> None:
        self.screen = _Screen.HOME
        self.role_id = None
        await self.rerender(interaction)

    async def _pick_admin_role(self, interaction: discord.Interaction) -> None:
        role = await self._resolve_role(interaction)
        action = getattr(self, "_admin_action", "register")
        if role is None:
            await self._flash("boost.errors.role_not_found", color="danger")
            return await self.rerender(interaction)
        if action in ("transfer", "remove") and not await self.store.role_exists(
            self.guild_id, role.id
        ):
            await self._flash("boost.errors.role_not_registered", color="danger")
            return await self.rerender(interaction)
        if action == "register":
            if await self.store.role_exists(self.guild_id, role.id):
                await self._flash("boost.errors.role_already_registered", color="danger")
                return await self.rerender(interaction)
            self.role_id = role.id
            self.screen = _Screen.REGISTER_OWNER
            return await self.rerender(interaction)
        if action == "remove":
            self.role_id = role.id
            self.screen = _Screen.REMOVE_CONFIRM
            return await self.rerender(interaction)
        self.role_id = role.id
        self.screen = _Screen.INVITE_TARGET
        await self.rerender(interaction)

    async def _by_id_admin(self, interaction: discord.Interaction) -> None:
        action = getattr(self, "_admin_action", "register")
        modal = make_by_id_modal(
            title=await self._t("boost.modals.add_by_id.title"),
            custom_id=f"boost_by_id_admin_modal:{action}:{interaction.id}",
            label=await self._t("boost.modals.add_by_id.role_label"),
            placeholder=await self._t("boost.modals.add_by_id.role_placeholder"),
            on_submit=self._admin_role_by_id,
        )
        await interaction.response.send_modal(modal)

    async def _admin_role_by_id(self, interaction: discord.Interaction, value: str) -> None:
        action = _suffix(interaction.custom_id, "boost_by_id_admin_modal")
        role = self._role_by_id(value)
        if role is None:
            await self._flash("boost.errors.role_not_found", color="danger")
            return await interaction.edit(view=self)
        if action in ("transfer", "remove") and not await self.store.role_exists(
            self.guild_id, role.id
        ):
            await self._flash("boost.errors.role_not_registered", color="danger")
            return await interaction.edit(view=self)
        if action == "register":
            if await self.store.role_exists(self.guild_id, role.id):
                await self._flash("boost.errors.role_already_registered", color="danger")
                return await interaction.edit(view=self)
            self.role_id = role.id
            self.screen = _Screen.REGISTER_OWNER
            return await interaction.edit(view=self)
        if action == "remove":
            self.role_id = role.id
            self.screen = _Screen.REMOVE_CONFIRM
            return await interaction.edit(view=self)
        self.role_id = role.id
        self.screen = _Screen.INVITE_TARGET
        await interaction.edit(view=self)

    async def _pick_transfer_owner(self, interaction: discord.Interaction) -> None:
        role_id = int(_suffix(interaction.custom_id, "boost_transfer_owner"))
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        try:
            member_id = int(values[0])
        except (TypeError, ValueError):
            return
        await self._transfer_to(interaction, role_id, member_id)

    async def _by_id_transfer_owner(self, interaction: discord.Interaction) -> None:
        role_id = int(_suffix(interaction.custom_id, "boost_by_id_transfer_owner"))
        modal = make_by_id_modal(
            title=await self._t("boost.modals.transfer.title"),
            custom_id=f"boost_by_id_transfer_owner_modal:{role_id}:{interaction.id}",
            label=await self._t("boost.modals.transfer.label"),
            placeholder=await self._t("boost.modals.transfer.placeholder"),
            on_submit=self._transfer_owner_by_id,
        )
        await interaction.response.send_modal(modal)

    async def _transfer_owner_by_id(self, interaction: discord.Interaction, value: str) -> None:
        try:
            member_id = int(value.strip())
        except ValueError:
            await self._flash("boost.errors.member_not_found", color="danger")
            return await interaction.edit(view=self)
        role_id = int(
            _suffix(interaction.custom_id, "boost_by_id_transfer_owner_modal").split(":")[0]
        )
        await self._transfer_to(interaction, role_id, member_id)

    async def _transfer_to(
        self, interaction: discord.Interaction, role_id: int, member_id: int
    ) -> None:
        await self._ack(interaction)
        role = self._guild().get_role(role_id) if self._guild() else None
        member = self._member(member_id)
        if role is None or member is None:
            await self._flash("boost.errors.member_not_found", color="danger")
            return await self.rerender(interaction)
        if member.bot:
            await self._flash("boost.errors.bot_not_allowed", color="danger")
            return await self.rerender(interaction)
        data = await self.store.get_role(self.guild_id, role_id) or {}
        if not await self.store.transfer_ownership(self.guild_id, role_id, member.id):
            await self._flash("boost.errors.role_not_registered", color="danger")
            return await self.rerender(interaction)
        await self._log(
            "transfer",
            color="info",
            role=f"<@&{role_id}>",
            old_owner=f"<@{data.get('owner_id', 0)}>",
            new_owner=member.mention,
            actor=await self._actor_mention(),
        )
        with contextlib.suppress(discord.Forbidden):
            await self._dm_transferred(role, member)
        self.screen = _Screen.HOME
        await self._flash("boost.success.ownership_transferred", role=f"@{role.name}")
        await self.rerender(interaction)

    async def _remove_confirm(self, interaction: discord.Interaction) -> None:
        await self._ack(interaction)
        _, role_id, raw = interaction.custom_id.split(":", 2)
        role_id = int(role_id)
        role = self._guild().get_role(role_id) if self._guild() else None
        data = await self.store.get_role(self.guild_id, role_id) or {}
        await self.store.remove_role(self.guild_id, role_id)
        for invite_id in list(
            (await self.store.get_invites_by_role(self.guild_id, role_id)).keys()
        ):
            await self.store.remove_invite(self.guild_id, invite_id)
        if raw == "delete" and role is not None:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await role.delete(reason="Boost role removed by admin")
        await self._log(
            "remove",
            color="danger",
            role=f"<@&{role_id}>",
            owner=f"<@{data.get('owner_id', 0)}>",
            actor=await self._actor_mention(),
            deleted=await self._t(
                "boost.buttons.delete_role_yes"
                if raw == "delete"
                else "boost.buttons.delete_role_no"
            ),
        )
        self.screen = _Screen.HOME
        await self._flash("boost.success.role_removed")
        await self.rerender(interaction)

    # -- rendering ----------------------------------------------------------

    async def build_items(self) -> list[discord.ui.ViewItem]:
        t = self.bot.translator.t
        theme = self.bot.theme
        if self.screen is _Screen.REGISTER:
            return await self._build_admin_pick(t, theme)
        if self.screen is _Screen.REGISTER_OWNER:
            return await self._build_register_owner(t, theme)
        if self.screen is _Screen.INVITE_TARGET:
            return await self._build_transfer_target(t, theme)
        if self.screen is _Screen.REMOVE_CONFIRM:
            return await self._build_remove_confirm(t, theme)
        if self.screen is _Screen.MY_ROLES:
            return await self._build_list_all(t, theme)
        return await self._build_admin_home(t, theme)

    async def _build_admin_home(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        roles = await self.store.get_roles(self.guild_id)
        total_members = sum(len(data.get("members", [])) for data in roles.values())
        container = self._container(
            "brand",
            TextDisplay(
                theme.md("title", title=await t(self.guild_id, "boost.titles.admin_panel"))
            ),
            TextDisplay(await t(self.guild_id, "boost.descriptions.guide_admin")),
            TextDisplay(
                theme.md(
                    "entry",
                    label=await t(self.guild_id, "boost.emoji.roles"),
                    value=await t(
                        self.guild_id,
                        "boost.descriptions.stats",
                        roles=len(roles),
                        members=total_members,
                    ),
                )
            ),
        )
        row = ActionRow(
            self.make_button(
                custom_id="boost_admin_register",
                label=await t(self.guild_id, "boost.buttons.register"),
                emoji=theme.emojis.get("scroll", ""),
            ),
            self.make_button(
                custom_id="boost_admin_transfer",
                label=await t(self.guild_id, "boost.buttons.transfer"),
                emoji=theme.emojis.get("swap", ""),
            ),
            self.make_button(
                custom_id="boost_admin_remove",
                label=await t(self.guild_id, "boost.buttons.remove"),
                style=discord.ButtonStyle.danger,
                emoji=theme.emojis.get("trash", ""),
            ),
        )
        row2 = ActionRow(
            self.make_button(
                custom_id="boost_admin_list",
                label=await t(self.guild_id, "boost.buttons.list_all"),
                emoji=theme.emojis.get("members", ""),
            ),
            self.make_button(
                custom_id="boost_close",
                label=await t(self.guild_id, "boost.buttons.close"),
                style=discord.ButtonStyle.danger,
            ),
        )
        return [container, row, row2]

    async def _build_admin_pick(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        action = getattr(self, "_admin_action", "register")
        if action == "register":
            title = await t(self.guild_id, "boost.titles.register")
            description = await t(self.guild_id, "boost.descriptions.register")
        elif action == "remove":
            title = await t(self.guild_id, "boost.titles.remove")
            description = await t(self.guild_id, "boost.descriptions.remove")
        else:
            title = await t(self.guild_id, "boost.titles.transfer")
            description = await t(self.guild_id, "boost.descriptions.transfer")
        container = self._container(
            "brand",
            TextDisplay(theme.md("title", title=title)),
            TextDisplay(description),
        )
        role_select = self.make_role_select(
            custom_id="boost_pick_role_admin",
            placeholder=await t(self.guild_id, "boost.placeholders.select_role"),
        )
        by_id = self.make_button(
            custom_id="boost_by_id_admin",
            label=await t(self.guild_id, "boost.buttons.add_by_id"),
            emoji=theme.emojis.get("numbers", ""),
        )
        back = self.make_button(
            custom_id="boost_admin_back", label=await t(self.guild_id, "boost.buttons.back")
        )
        return [
            container,
            divider(),
            ActionRow(role_select),
            ActionRow(by_id, back),
        ]

    async def _build_transfer_target(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        container = self._container(
            "info",
            TextDisplay(
                theme.md("title", title=await t(self.guild_id, "boost.titles.transfer_target"))
            ),
            TextDisplay(await t(self.guild_id, "boost.descriptions.transfer_hint")),
            TextDisplay(await t(self.guild_id, "boost.descriptions.new_owner")),
        )
        member_select = self.make_user_select(
            custom_id=f"boost_transfer_owner:{self.role_id}",
            placeholder=await t(self.guild_id, "boost.placeholders.select_member"),
        )
        by_id = self.make_button(
            custom_id=f"boost_by_id_transfer_owner:{self.role_id}",
            label=await t(self.guild_id, "boost.buttons.add_by_id"),
            emoji=theme.emojis.get("numbers", ""),
        )
        back = self.make_button(
            custom_id="boost_admin_back", label=await t(self.guild_id, "boost.buttons.back")
        )
        return [
            container,
            divider(),
            ActionRow(member_select),
            ActionRow(by_id, back),
        ]

    async def _build_remove_confirm(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        role = self._current_role()
        role_mention = role.mention if role else str(self.role_id)
        items: list[discord.ui.ViewItem] = [
            TextDisplay(
                theme.md("title", title=await t(self.guild_id, "boost.titles.remove_confirm"))
            ),
            TextDisplay(
                await t(self.guild_id, "boost.descriptions.confirm_removal", role=role_mention)
            ),
            TextDisplay(await t(self.guild_id, "boost.descriptions.remove_hint")),
        ]
        if role is not None:
            items = self._role_items(role, *items)
        container = self._container("danger", *items)
        row = ActionRow(
            self.make_button(
                custom_id=f"boost_remove_confirm_admin:{self.role_id}:delete",
                label=await t(self.guild_id, "boost.buttons.delete_role_yes"),
                style=discord.ButtonStyle.danger,
            ),
            self.make_button(
                custom_id=f"boost_remove_confirm_admin:{self.role_id}:keep",
                label=await t(self.guild_id, "boost.buttons.delete_role_no"),
                style=discord.ButtonStyle.secondary,
            ),
        )
        row2 = ActionRow(
            self.make_button(
                custom_id="boost_admin_back", label=await t(self.guild_id, "boost.buttons.cancel")
            )
        )
        return [container, row, row2]

    async def _build_list_all(self, t: Any, theme) -> list[discord.ui.ViewItem]:
        roles = await self.store.get_roles(self.guild_id)
        guild = self._guild()
        entries: list[discord.ui.ViewItem] = []
        for role_id_str, data in roles.items():
            role = guild.get_role(int(role_id_str)) if guild else None
            name = role.name if role else role_id_str
            if role is not None:
                items = self._role_items(
                    role,
                    TextDisplay(
                        theme.md(
                            "entry",
                            label=f"@{name}",
                            value=await t(
                                self.guild_id,
                                "boost.descriptions.role_entry",
                                owner=f"<@{data.get('owner_id', 0)}>",
                                members=len(data.get("members", [])),
                            ),
                        )
                    ),
                )
            else:
                items = [
                    TextDisplay(
                        theme.md(
                            "entry",
                            label=f"@{name}",
                            value=await t(
                                self.guild_id,
                                "boost.descriptions.role_entry",
                                owner=f"<@{data.get('owner_id', 0)}>",
                                members=len(data.get("members", [])),
                            ),
                        )
                    )
                ]
            entries.append(designer_container(theme.color("info"), *items))
        parts: list[discord.ui.ViewItem] = [
            TextDisplay(theme.md("title", title=await t(self.guild_id, "boost.titles.list_all"))),
        ]
        if not entries:
            parts.append(TextDisplay(await t(self.guild_id, "boost.descriptions.no_roles")))
        container = self._container("info", *parts)
        page_entries = paginate(entries, self.page, _LIST_PAGE_SIZE)
        total_pages = page_count(len(entries), _LIST_PAGE_SIZE)
        items: list[discord.ui.ViewItem] = [container, divider()]
        items.extend(page_entries)
        nav: list[discord.ui.ViewItem] = []
        if self.page > 0:
            nav.append(
                self.make_button(
                    custom_id="boost_prev", label=await t(self.guild_id, "boost.buttons.previous")
                )
            )
        if self.page < total_pages - 1:
            nav.append(
                self.make_button(
                    custom_id="boost_next", label=await t(self.guild_id, "boost.buttons.next")
                )
            )
        nav.append(
            self.make_button(
                custom_id="boost_admin_back", label=await t(self.guild_id, "boost.buttons.back")
            )
        )
        items.append(divider("large"))
        items.append(ActionRow(*nav))
        return items
