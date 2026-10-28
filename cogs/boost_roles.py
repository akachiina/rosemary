"""Boost role commands, listeners and maintenance for Rosemary.

Commands:

* ``/boost panel`` (group localized ``/cargo_boost``), the member panel.
* ``/boost admin`` (group localized ``/cargo_boost``), the moderation panel
  (``manage_guild`` required).

The cog also reconciles the stored data with reality: when a member leaves the
server their owned roles are auto-transferred to the first member (or unregistered),
their memberships are dropped, and a periodic task expires stale invites. Persistent
invite buttons are re-registered on every ready so DMs keep working after restarts.

Auto-created boost roles: while ``boost.auto_create.enabled`` is on, a member who
starts boosting gets a role created below ``boost.auto_create.below_role`` and
registered with them as the owner. When the boost ends the ``boost.auto_create.
unboost_action`` setting decides whether the role is deleted (archived in the
store so the next boost restores it with its members), removed from its holders
while the registration stays, or kept as is. A boot-time catch-up covers boosts
that happened while the bot was offline.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import discord
from discord.ext import commands

from rosemary.core.boost_roles import BoostRoleStore
from rosemary.core.debug import send_log
from rosemary.core.localized_group import LocalizedSlashCommandGroup
from rosemary.core.settings import get_setting
from rosemary.ui import boost_dm as dm
from rosemary.ui.boost_invite import BoostInviteView
from rosemary.ui.boost_menu import AdminBoostMenuView, BoostMenuView

log = logging.getLogger(__name__)

_CLEANUP_INTERVAL = 300  # seconds


class BoostRolesCog(commands.Cog):
    """Boost role management."""


    boost_group = LocalizedSlashCommandGroup(
        "boost",
        description="Boost roles panel",
        contexts={discord.InteractionContextType.guild},
    )
    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = BoostRoleStore(bot.storage.data_dir)
        self._views_registered = False
        self._cleanup_task: asyncio.Task | None = None

    #: lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Start per-guild background work once per process.

        Called explicitly from ``RosemaryBot._setup`` because py-cord 2.8.1's
        sync ``add_cog`` never invokes ``cog_load`` (and ``on_ready`` listeners
        registered during the first ready only fire on the next reconnect).
        """
        if not self._views_registered:
            await self._register_persistent_views()
            self._views_registered = True
            await self._remove_legacy_settings_keys()
            await self._catchup_boosters()
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = self.bot.loop.create_task(self._cleanup_loop())

    async def _catchup_boosters(self) -> None:
        """Create the auto boost role for boosters who started while offline.

        Runs once per process, only for guilds that were already present at
        boot (never on ``on_guild_join``: a newly added server must not wake
        up to a batch of fresh roles).
        """
        for guild in self.bot.guilds:
            with contextlib.suppress(Exception):
                if not await self._auto_create_enabled(guild.id):
                    continue
                for member in list(guild.members):
                    if member.bot or member.premium_since is None:
                        continue
                    with contextlib.suppress(Exception):
                        await self._auto_create_for(guild, member)

    async def _remove_legacy_settings_keys(self) -> None:
        """Drop boost keys that were once stored in the /settings file.

        Boost data moved to its own ``boost_roles.json``; this removes the old
        ``boost_roles``/``boost_invites`` keys from ``settings.json`` so the file
        only carries /settings configuration again.
        """
        for guild in self.bot.guilds:
            with contextlib.suppress(Exception):
                await self.bot.storage.delete_keys(guild.id, "boost_roles", "boost_invites")

    async def cog_unload(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()

    async def _register_persistent_views(self) -> None:
        """Rebuild a BoostInviteView per pending invite so DM buttons persist."""
        for guild in self.bot.guilds:
            invites = await self.store.get_invites(guild.id)
            for invite_id in invites:
                view = BoostInviteView(
                    self.bot,
                    guild_id=guild.id,
                    invite_id=invite_id,
                    accept_label=await self.bot.translator.t(
                        guild.id, "boost.buttons.accept_invite"
                    ),
                    decline_label=await self.bot.translator.t(
                        guild.id, "boost.buttons.decline_invite"
                    ),
                )
                self.bot.add_view(view)

    async def _cleanup_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                for guild in self.bot.guilds:
                    with contextlib.suppress(Exception):
                        expired = await self.store.cleanup_expired_invites(guild.id)
                        if expired:
                            log.info(
                                "Cleaned %d expired boost invites in %s", len(expired), guild.id
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("Boost invite cleanup failed: %s", exc)
            await asyncio.sleep(_CLEANUP_INTERVAL)

    #: listeners ----------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Auto-transfer or unregister roles owned by a departed member.

        A role with a live member becomes theirs: the registration survives,
        so the new owner actually sees the role in the ``/boost`` "my roles"
        screen. A role with nobody resolvable (no members, or everyone
        listed has also departed) is unregistered and logged as an orphan.
        Pending invites die with the old owner either way.
        """
        guild = member.guild
        guild_id = guild.id
        owned = await self.store.get_roles_by_owner(guild_id, member.id)
        for role_id_str, data in owned.items():
            role_id = int(role_id_str)
            role = guild.get_role(role_id)
            role_mention = role.mention if role else f"<@&{role_id}>"
            for invite_id in list((await self.store.get_invites_by_role(guild_id, role_id)).keys()):
                await self.store.remove_invite(guild_id, invite_id)
            new_owner = None
            for candidate_id in list(data.get("members", [])):
                if candidate_id == member.id:
                    # Legacy data may still list the departed owner as a
                    # member of their own role; they can never inherit it.
                    continue
                candidate = guild.get_member(candidate_id)
                if candidate is not None and not candidate.bot:
                    new_owner = candidate
                    break
            if new_owner is None:
                await self.store.remove_role(guild_id, role_id)
                await send_log(
                    self.bot,
                    guild_id,
                    "boost.logs.orphan_remove",
                    role=role_mention,
                    owner=member.mention,
                )
                continue
            await self.store.transfer_ownership(guild_id, role_id, new_owner.id)
            if role is not None:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    preview_data = await self.store.get_role(guild_id, role_id) or {}
                    await dm.send(
                        self.bot,
                        new_owner,
                        guild_id,
                        "boost.dm.auto_transferred",
                        mention=new_owner.mention,
                        role_name=role.name,
                        server_name=guild.name,
                    )
                    await dm.send_preview(self.bot, new_owner, guild_id, role, preview_data)
            await send_log(
                self.bot,
                guild_id,
                "boost.logs.auto_transfer",
                role=role_mention,
                old_owner=member.mention,
                new_owner=new_owner.mention,
            )
        for role_id_str in await self.store.get_roles_as_member(guild_id, member.id):
            await self.store.remove_member(guild_id, int(role_id_str), member.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Auto-create or handle the auto boost role when the boost state flips."""
        if before.premium_since == after.premium_since:
            return
        if after.premium_since is not None:
            await self._handle_boost_start(after)
        else:
            await self._handle_boost_end(after)

    #: auto-created roles ---------------------------------------------------

    async def _auto_create_enabled(self, guild_id: int) -> bool:
        if not await get_setting(self.bot.storage, guild_id, "boost.enabled"):
            return False
        return await get_setting(self.bot.storage, guild_id, "boost.auto_create.enabled")

    async def _handle_boost_start(self, member: discord.Member) -> None:
        if member.bot or not await self._auto_create_enabled(member.guild.id):
            return
        try:
            await self._auto_create_for(member.guild, member)
        except Exception as exc:  # noqa: BLE001
            log.error("Boost auto-create failed for %s in %s: %s", member.id, member.guild.id, exc)

    async def _auto_create_for(self, guild: discord.Guild, member: discord.Member) -> None:
        """Create, restore or re-apply the auto boost role of ``member``."""
        archive = await self.store.get_archive(guild.id, member.id)
        if archive is not None:
            await self._restore_archived_role(guild, member, archive)
            return
        owned = await self.store.get_auto_role_by_owner(guild.id, member.id)
        if owned is not None:
            await self._reapply_auto_role(guild, member, owned[0], owned[1])
            return
        if await self.store.get_roles_by_owner(guild.id, member.id):
            return  # already owns a manually registered boost role
        await self._create_auto_role_for(guild, member)

    async def _create_auto_role_for(self, guild: discord.Guild, member: discord.Member) -> None:
        """Fresh boost: create the role, register it and notify the owner."""
        role = await self._create_boost_role(guild, name=self._auto_role_name(member))
        if role is None:
            return
        await self.store.add_role(guild.id, role.id, member.id, self.bot.user.id, auto=True)
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await member.add_roles(role, reason="Boost role owner")
        data = await self.store.get_role(guild.id, role.id) or {}
        await self._dm_auto_owner(member, "boost.dm.auto_created", role, data)
        await send_log(
            self.bot,
            guild.id,
            "boost.logs.auto_create",
            role=f"<@&{role.id}>",
            owner=member.mention,
        )

    async def _restore_archived_role(
        self, guild: discord.Guild, member: discord.Member, archive: dict
    ) -> None:
        """Boost back after a delete: recreate the archived role with its members."""
        role = await self._create_boost_role(
            guild,
            name=str(archive.get("role_name") or self._auto_role_name(member)),
            color=int(archive.get("color") or 0),
            emoji=archive.get("emoji"),
        )
        if role is None:
            return
        await self.store.add_role(guild.id, role.id, member.id, self.bot.user.id, auto=True)
        await self.store.pop_archive(guild.id, member.id)
        previous = list(archive.get("members", []))
        await self._give_role_back(guild, member, role, previous)
        max_members = await get_setting(self.bot.storage, guild.id, "boost.max_members")
        for user_id in previous:
            await self.store.add_member(guild.id, role.id, user_id, max_members)
        data = await self.store.get_role(guild.id, role.id) or {}
        await self._dm_auto_owner(member, "boost.dm.auto_restored", role, data)
        await send_log(
            self.bot,
            guild.id,
            "boost.logs.auto_create",
            role=f"<@&{role.id}>",
            owner=member.mention,
        )

    async def _reapply_auto_role(
        self, guild: discord.Guild, member: discord.Member, role_id: int, data: dict
    ) -> None:
        """Boost back after a remove/keep: hand the existing role to its holders."""
        role = guild.get_role(role_id)
        if role is None or role in member.roles:
            return  # role vanished, or was never taken away
        await self._give_role_back(guild, member, role, list(data.get("members", [])))
        await self._dm_auto_owner(member, "boost.dm.auto_restored", role, data)

    async def _handle_boost_end(self, member: discord.Member) -> None:
        """Apply ``boost.auto_create.unboost_action`` to the member's auto role."""
        guild = member.guild
        if not await self._auto_create_enabled(guild.id):
            return
        owned = await self.store.get_auto_role_by_owner(guild.id, member.id)
        if owned is None:
            return
        role_id, data = owned
        role = guild.get_role(role_id)
        role_name = role.name if role is not None else str(role_id)
        color = role.colour.value if role is not None else 0
        emoji = role.unicode_emoji if role is not None else None
        action = await get_setting(self.bot.storage, guild.id, "boost.auto_create.unboost_action")
        if action == "delete":
            if role is not None:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await role.delete(reason="Boost ended")
            await self.store.archive_role(
                guild.id, role_id, role_name=role_name, color=color, emoji=emoji
            )
            for invite_id in list((await self.store.get_invites_by_role(guild.id, role_id)).keys()):
                await self.store.remove_invite(guild.id, invite_id)
            dm_key = "boost.dm.unboost_deleted"
        elif action == "remove":
            if role is not None:
                await self._take_role_from(guild, member, role, list(data.get("members", [])))
            dm_key = "boost.dm.unboost_removed"
        else:
            dm_key = "boost.dm.unboost_kept"
        with contextlib.suppress(discord.Forbidden):
            await dm.send(
                self.bot,
                member,
                guild.id,
                dm_key,
                mention=member.mention,
                role_name=role_name,
                server_name=guild.name,
            )
        action_label = await self.bot.translator.t(
            guild.id, f"settings.boost.auto_create.unboost_action.choices.{action}"
        )
        await send_log(
            self.bot,
            guild.id,
            "boost.logs.unboost",
            role=f"<@&{role_id}>",
            owner=member.mention,
            action=action_label,
        )

    async def _create_boost_role(
        self,
        guild: discord.Guild,
        *,
        name: str,
        color: int = 0,
        emoji: str | None = None,
    ) -> discord.Role | None:
        """Create the boost role below the configured anchor; ``None`` on failure."""
        try:
            role = await guild.create_role(
                name=name,
                colour=discord.Colour(color),
                mentionable=True,
                reason="Boost role auto-created",
            )
        except discord.Forbidden:
            log.error("Missing permission to create the auto boost role in %s", guild.id)
            return None
        except discord.HTTPException as exc:
            log.error("Auto boost role creation failed in %s: %s", guild.id, exc)
            return None
        if emoji:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await role.edit(unicode_emoji=emoji, reason="Boost role emoji restored")
        await self._place_below_anchor(guild, role)
        return role

    async def _place_below_anchor(self, guild: discord.Guild, role: discord.Role) -> None:
        """Move ``role`` right below the anchor configured in the settings.

        No anchor, an anchor at the very bottom or missing permissions keep the
        role where Discord created it (bottom of the list), still below the
        anchor, so failures never break the creation.
        """
        anchor_id = await get_setting(self.bot.storage, guild.id, "boost.auto_create.below_role")
        if not anchor_id:
            return
        anchor = guild.get_role(anchor_id)
        if anchor is None or anchor.is_default() or anchor.position < 2:
            return
        try:
            await role.edit(position=anchor.position - 1, reason="Boost role position")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning(
                "Could not place auto boost role below %s in %s: %s", anchor_id, guild.id, exc
            )

    async def _give_role_back(
        self,
        guild: discord.Guild,
        member: discord.Member,
        role: discord.Role,
        member_ids: list[int],
    ) -> None:
        """Give the role to the owner and back to the previous members."""
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await member.add_roles(role, reason="Boost role owner")
        for user_id in member_ids:
            other = guild.get_member(user_id)
            if other is None or other.id == member.id:
                continue
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await other.add_roles(role, reason="Boost role member restored")

    async def _take_role_from(
        self,
        guild: discord.Guild,
        member: discord.Member,
        role: discord.Role,
        member_ids: list[int],
    ) -> None:
        """Take the role away from the owner and its members (registration stays)."""
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await member.remove_roles(role, reason="Boost ended")
        for user_id in member_ids:
            other = guild.get_member(user_id)
            if other is None or other.id == member.id:
                continue
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await other.remove_roles(role, reason="Boost ended")

    async def _dm_auto_owner(
        self,
        member: discord.Member,
        key: str,
        role: discord.Role,
        data: dict,
    ) -> None:
        """DM the new owner with the card and the themed role preview."""
        guild_id = member.guild.id
        max_members = await get_setting(self.bot.storage, guild_id, "boost.max_members")
        with contextlib.suppress(discord.Forbidden):
            await dm.send(
                self.bot,
                member,
                guild_id,
                key,
                mention=member.mention,
                role_name=role.name,
                server_name=member.guild.name,
                max_members=max_members,
            )
            await dm.send_preview(self.bot, member, guild_id, role, data)

    @staticmethod
    def _auto_role_name(member: discord.Member) -> str:
        """The auto role name: the member's display name, sanitized."""
        name = " ".join(str(member.display_name or "").split())[:100]
        return name or member.name

    #: helpers ------------------------------------------------------------

    def _guild_id(self, ctx) -> int:
        return ctx.guild.id

    async def _open_menu(self, ctx, view) -> None:
        await view.prepare()
        from rosemary.core.card_service import trace_card_path

        # The menu heading is theme-customizable (card.boost.home): trace it.
        await trace_card_path(self.bot, self._guild_id(ctx), "boost.home")
        await ctx.respond(view=view, ephemeral=True)

    #: commands -----------------------------------------------------------

    @boost_group.command(
        name="panel",
        description="Boost roles panel",
    )
    async def boost(self, ctx: discord.ApplicationContext) -> None:
        """Open the boost-role panel."""
        if not await get_setting(self.bot.storage, self._guild_id(ctx), "boost.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(self._guild_id(ctx), "boost.error_disabled"),
                ephemeral=True,
            )
        view = BoostMenuView(self.bot, self._guild_id(ctx), owner_id=ctx.author.id)
        await self._open_menu(ctx, view)

    @boost_group.command(
        name="admin",
        description="Admin boost roles panel",
        default_member_permissions=discord.Permissions(manage_guild=True),
    )
    async def boost_admin(self, ctx: discord.ApplicationContext) -> None:
        """Open the admin boost-role panel."""
        if not await get_setting(self.bot.storage, self._guild_id(ctx), "boost.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(self._guild_id(ctx), "boost.error_disabled"),
                ephemeral=True,
            )
        view = AdminBoostMenuView(self.bot, self._guild_id(ctx), owner_id=ctx.author.id)
        await self._open_menu(ctx, view)

