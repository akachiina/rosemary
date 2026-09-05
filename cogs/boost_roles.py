"""Boost role commands, listeners and maintenance for Rosemary.

Commands:

* ``/boost`` (localized ``/cargo_boost``), the member panel for boost roles.
* ``/boost_admin`` (localized ``/admin_boost``), the moderation panel
  (``manage_guild`` required).

The cog also reconciles the stored data with reality: when a member leaves the
server their owned roles are auto-transferred to the first member (or unregistered),
their memberships are dropped, and a periodic task expires stale invites. Persistent
invite buttons are re-registered on every ready so DMs keep working after restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import discord
from discord.ext import commands

from rosemary.core.boost_roles import BoostRoleStore
from rosemary.core.cards import text_or
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting
from rosemary.ui import boost_dm as dm
from rosemary.ui.boost_invite import BoostInviteView
from rosemary.ui.boost_menu import AdminBoostMenuView, BoostMenuView

log = logging.getLogger(__name__)

_CLEANUP_INTERVAL = 300  # seconds


class BoostRolesCog(commands.Cog):
    """Boost role management."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = BoostRoleStore(bot.storage.data_dir)
        self._views_registered = False
        self._cleanup_task: asyncio.Task | None = None

    # -- lifecycle ----------------------------------------------------------

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
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = self.bot.loop.create_task(self._cleanup_loop())

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

    # -- listeners ----------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Auto-transfer or unregister roles owned by a departed member."""
        guild = member.guild
        guild_id = guild.id
        owned = await self.store.get_roles_by_owner(guild_id, member.id)
        for role_id_str, data in owned.items():
            role_id = int(role_id_str)
            members = list(data.get("members", []))
            await self.store.remove_role(guild_id, role_id)
            for invite_id in list((await self.store.get_invites_by_role(guild_id, role_id)).keys()):
                await self.store.remove_invite(guild_id, invite_id)
            role = guild.get_role(role_id)
            if not members:
                await send_channel_log(
                    self.bot,
                    guild_id,
                    await self.bot.translator.t(guild_id, "boost.logs.orphan_remove.title"),
                    await text_or(
                        self.bot,
                        guild_id,
                        "boost.logs.orphan_remove.description",
                        await self.bot.translator.t(
                            guild_id,
                            "boost.logs.orphan_remove.description",
                            role=role.mention if role else f"<@&{role_id}>",
                            owner=member.mention,
                        ),
                        role=role.mention if role else f"<@&{role_id}>",
                        owner=member.mention,
                    ),
                    color="danger",
                    card_key="boost.logs.orphan_remove.description",
                    mention_user_ids=[member.id],
                )
                continue
            new_owner = guild.get_member(members[0])
            if new_owner is None:
                continue
            await self.store.transfer_ownership(guild_id, role_id, new_owner.id)
            if role is not None:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    data = await self.store.get_role(guild_id, role_id) or {}
                    await dm.send(
                        self.bot,
                        new_owner,
                        guild_id,
                        "boost.dm.auto_transferred",
                        mention=new_owner.mention,
                        role_name=role.name,
                        server_name=guild.name,
                    )
                    await dm.send_preview(self.bot, new_owner, guild_id, role, data)
            await send_channel_log(
                self.bot,
                guild_id,
                await self.bot.translator.t(guild_id, "boost.logs.auto_transfer.title"),
                await text_or(
                    self.bot,
                    guild_id,
                    "boost.logs.auto_transfer.description",
                    await self.bot.translator.t(
                        guild_id,
                        "boost.logs.auto_transfer.description",
                        role=role.mention if role else f"<@&{role_id}>",
                        old_owner=member.mention,
                        new_owner=new_owner.mention,
                    ),
                    role=role.mention if role else f"<@&{role_id}>",
                    old_owner=member.mention,
                    new_owner=new_owner.mention,
                ),
                color="warning",
                card_key="boost.logs.auto_transfer.description",
                mention_user_ids=[member.id, new_owner.id],
            )
        for role_id_str in await self.store.get_roles_as_member(guild_id, member.id):
            await self.store.remove_member(guild_id, int(role_id_str), member.id)

    # -- helpers ------------------------------------------------------------

    def _guild_id(self, ctx) -> int:
        return ctx.guild.id

    async def _open_menu(self, ctx, view) -> None:
        await view.prepare()
        await ctx.respond(view=view, ephemeral=True)

    # -- commands -----------------------------------------------------------

    @discord.slash_command(
        name="boost",
        description="Boost roles panel",
        contexts={discord.InteractionContextType.guild},
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

    @discord.slash_command(
        name="boost_admin",
        description="Admin boost roles panel",
        default_member_permissions=discord.Permissions(manage_guild=True),
        contexts={discord.InteractionContextType.guild},
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


def setup(bot) -> None:
    bot.add_cog(BoostRolesCog(bot))
