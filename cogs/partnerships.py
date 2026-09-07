"""Partnership management: ads, renewal loop, role assignment and logs.

Partners are other servers/members advertised in the partnerships channel. Each
record tracks its representative, ad message, last renewal and warning state.
An hourly loop warns about expiring ads and removes fully expired ones
(stripping the partner role). Representatives can renew their own ad.
"""

import contextlib
import logging

import discord
import discord.ext.tasks as tasks
from discord.ext import commands

from rosemary.core.cards import log_description, text_or
from rosemary.core.debug import send_channel_log
from rosemary.core.partnerships import PartnershipStore
from rosemary.core.settings import get_setting

log = logging.getLogger(__name__)


class PartnershipsCog(commands.Cog):
    """Server partnership ads and renewal."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = PartnershipStore(bot.storage.data_dir)

    async def start(self) -> None:
        self._loop.start()

    def cog_unload(self) -> None:
        self._loop.cancel()

    @tasks.loop(hours=1)
    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        for guild in list(self.bot.guilds):
            try:
                await self.check_guild(guild)
            except Exception as exc:
                log.error("Partnership check failed in %s: %s", guild.id, exc)

    @_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def check_guild(self, guild: discord.Guild) -> None:
        if not await get_setting(self.bot.storage, guild.id, "partnerships.enabled"):
            return
        renewal = await get_setting(self.bot.storage, guild.id, "partnerships.renewal_days")
        grace = await get_setting(self.bot.storage, guild.id, "partnerships.grace_days")
        for partner_id, entry in (await self.store.all(guild.id)).items():
            age = PartnershipStore.days_since(entry.get("last_renewed_at"))
            if age < renewal:
                continue
            if age < renewal + grace and not entry.get("warning_sent"):
                await self._warn(guild, partner_id, entry, int(age - renewal))
                await self.store.mark_warned(guild.id, partner_id)
            elif age >= renewal + grace:
                await self._expire(guild, partner_id, entry)

    # -- helpers ---------------------------------------------------------------

    async def _channel(self, guild: discord.Guild):
        channel_id = await get_setting(self.bot.storage, guild.id, "partnerships.channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _assign_role(self, guild: discord.Guild, user_id: int) -> None:
        role_id = await get_setting(self.bot.storage, guild.id, "partnerships.role")
        role = guild.get_role(role_id) if role_id else None
        member = guild.get_member(user_id)
        if role is None or member is None:
            return
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await member.add_roles(role, reason="Partnership added")

    async def _strip_role(self, guild: discord.Guild, user_id: int) -> None:
        role_id = await get_setting(self.bot.storage, guild.id, "partnerships.role")
        role = guild.get_role(role_id) if role_id else None
        member = guild.get_member(user_id)
        if role is None or member is None:
            return
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await member.remove_roles(role, reason="Partnership removed")

    async def _dm(self, guild: discord.Guild, user_id: int, key: str, **variables) -> None:
        member = guild.get_member(user_id)
        if member is None:
            return
        text = await text_or(
            self.bot,
            guild.id,
            f"partnerships.dm.{key}",
            await self.bot.translator.t(guild.id, f"partnerships.dm.{key}", **variables),
            **variables,
        )
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await member.send(text)

    async def _log(self, guild_id: int, key: str, color: str, ids=(), **variables) -> None:
        await send_channel_log(
            self.bot,
            guild_id,
            await self.bot.translator.t(guild_id, f"partnerships.logs.{key}.title"),
            await log_description(
                self.bot, guild_id, f"partnerships.logs.{key}.description", **variables
            ),
            color=color,
            card_key=f"partnerships.logs.{key}.description",
            mention_user_ids=list(ids),
        )

    async def _delete_ad(self, guild: discord.Guild, entry: dict) -> None:
        channel = await self._channel(guild)
        message_id = entry.get("message_id")
        if channel is None or not message_id:
            return
        with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = await channel.fetch_message(int(message_id))
            await message.delete()

    async def _warn(self, guild: discord.Guild, partner_id: int, entry: dict, overdue: int) -> None:
        rep_id = int(entry.get("rep_id") or 0)
        await self._dm(
            guild, rep_id, "warning", days=overdue, server=guild.name,
        )
        await self._log(
            guild.id, "warning_sent", "warning", [rep_id],
            user=f"<@{rep_id}>", days=overdue,
        )

    async def _expire(self, guild: discord.Guild, partner_id: int, entry: dict) -> None:
        rep_id = int(entry.get("rep_id") or 0)
        await self._delete_ad(guild, entry)
        await self._strip_role(guild, rep_id)
        await self.store.remove(guild.id, partner_id)
        await self._dm(guild, rep_id, "expired", server=guild.name)
        ping_role_id = await get_setting(self.bot.storage, guild.id, "partnerships.ping_role")
        ping = f"<@&{ping_role_id}>" if ping_role_id else ""
        await self._log(
            guild.id, "expired", "danger", [rep_id],
            user=f"<@{rep_id}>", ping=ping,
        )

    async def _post_ad(
        self, guild: discord.Guild, content: str, attachments: list[str]
    ):
        from rosemary.core.mentions import mentions_for

        channel = await self._channel(guild)
        if channel is None:
            return None
        ping_role_id = await get_setting(self.bot.storage, guild.id, "partnerships.ping_role")
        prefix = f"<@&{ping_role_id}>\n" if ping_role_id else ""
        try:
            return await channel.send(
                f"{prefix}{content}".strip(),
                allowed_mentions=await mentions_for(
                    self.bot,
                    guild.id,
                    "partnerships.invite",
                    role_ids=[ping_role_id] if ping_role_id else [],
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Partnership ad post failed in %s: %s", guild.id, exc)
            return None

    # -- commands ------------------------------------------------------------------

    @discord.slash_command(
        name="partnerships_add",
        description="[ADMIN] Add a partnership",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def partnerships_add(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Partner representative"),
        text: discord.Option(str, description="Partnership ad text"),
    ) -> None:
        """Advertise a partner and grant them the partner role."""
        guild = ctx.guild
        if not await get_setting(self.bot.storage, guild.id, "partnerships.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "partnerships.error_disabled"),
                ephemeral=True,
            )
        if await self._channel(guild) is None:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "partnerships.error_no_channel"),
                ephemeral=True,
            )
        await ctx.response.defer(ephemeral=True)
        message = await self._post_ad(guild, text, [])
        await self.store.add(
            guild.id, member.id, member.id, ctx.author.id,
            message.id if message else None, text, [],
        )
        await self._assign_role(guild, member.id)
        await self._dm(guild, member.id, "added", server=guild.name)
        await self._log(
            guild.id, "added", "success", [member.id, ctx.author.id],
            user=member.mention, author=ctx.author.mention,
        )
        await ctx.respond(
            await self.bot.translator.t(
                guild.id, "partnerships.success.added", user=member.mention
            ),
            ephemeral=True,
        )

    @discord.slash_command(
        name="partnerships_remove",
        description="[ADMIN] Remove a partnership",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def partnerships_remove(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(discord.Member, description="Partner representative"),
    ) -> None:
        """Remove a partnership: delete the ad and strip the role."""
        guild = ctx.guild
        if not await get_setting(self.bot.storage, guild.id, "partnerships.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "partnerships.error_disabled"),
                ephemeral=True,
            )
        entry = await self.store.get(guild.id, member.id)
        if entry is None:
            return await ctx.respond(
                await self.bot.translator.t(
                    guild.id, "partnerships.error_not_found", user=member.mention
                ),
                ephemeral=True,
            )
        await ctx.response.defer(ephemeral=True)
        await self._delete_ad(guild, entry)
        await self._strip_role(guild, member.id)
        await self.store.remove(guild.id, member.id)
        await self._dm(guild, member.id, "removed", server=guild.name)
        await self._log(
            guild.id, "removed", "warning", [member.id, ctx.author.id],
            user=member.mention, author=ctx.author.mention,
        )
        await ctx.respond(
            await self.bot.translator.t(
                guild.id, "partnerships.success.removed", user=member.mention
            ),
            ephemeral=True,
        )

    @discord.slash_command(
        name="partnerships_renew",
        description="Renew your partnership ad",
        contexts={discord.InteractionContextType.guild},
    )
    async def partnerships_renew(self, ctx: discord.ApplicationContext) -> None:
        """Re-post your own partnership ad and reset its renewal clock."""
        guild = ctx.guild
        if not await get_setting(self.bot.storage, guild.id, "partnerships.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "partnerships.error_disabled"),
                ephemeral=True,
            )
        entry = await self.store.get(guild.id, ctx.author.id)
        if entry is None or int(entry.get("rep_id") or 0) != ctx.author.id:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "partnerships.error_not_rep"),
                ephemeral=True,
            )
        await ctx.response.defer(ephemeral=True)
        await self._delete_ad(guild, entry)
        message = await self._post_ad(guild, entry.get("content") or "", [])
        await self.store.touch_renew(
            guild.id, ctx.author.id, message.id if message else None
        )
        await self._log(
            guild.id, "renewed", "success", [ctx.author.id],
            user=ctx.author.mention,
        )
        await ctx.respond(
            await self.bot.translator.t(guild.id, "partnerships.success.renewed"),
            ephemeral=True,
        )

    @discord.slash_command(
        name="partnerships_invite",
        description="[ADMIN] Post the partnership invite text",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def partnerships_invite(self, ctx: discord.ApplicationContext) -> None:
        """Post the guild's partnership pitch in the partnerships channel."""
        guild = ctx.guild
        if not ctx.response.is_done():
            await ctx.response.defer(ephemeral=True)
        if not await get_setting(self.bot.storage, guild.id, "partnerships.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "partnerships.error_disabled"),
                ephemeral=True,
            )
        text = await text_or(
            self.bot,
            guild.id,
            "partnerships.invite",
            await self.bot.translator.t(
                guild.id, "partnerships.invite_text", server=guild.name
            ),
            server=guild.name,
        )
        message = await self._post_ad(guild, text, [])
        if message is None:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "partnerships.error_no_channel"),
                ephemeral=True,
            )
        await self._log(
            guild.id, "invite_posted", "info", [ctx.author.id],
            author=ctx.author.mention,
        )
        await ctx.respond(
            await self.bot.translator.t(guild.id, "partnerships.success.invite_posted"),
            ephemeral=True,
        )

    @discord.slash_command(
        name="partnerships_list",
        description="[ADMIN] List partnerships",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def partnerships_list(self, ctx: discord.ApplicationContext) -> None:
        """List partnerships with days left until renewal."""
        guild = ctx.guild
        if not await get_setting(self.bot.storage, guild.id, "partnerships.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "partnerships.error_disabled"),
                ephemeral=True,
            )
        renewal = await get_setting(self.bot.storage, guild.id, "partnerships.renewal_days")
        entries = await self.store.all(guild.id)
        if not entries:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "partnerships.list_empty"),
                ephemeral=True,
            )
        lines = []
        for partner_id, entry in entries.items():
            age = PartnershipStore.days_since(entry.get("last_renewed_at"))
            left = max(renewal - age, 0)
            lines.append(
                await self.bot.translator.t(
                    guild.id,
                    "partnerships.list_line",
                    user=f"<@{partner_id}>",
                    days=f"{left:.0f}",
                    warned=(self.bot.theme.emoji("warning") or "⚠️")
                    if entry.get("warning_sent")
                    else "",
                )
            )
        title = await self.bot.translator.t(guild.id, "partnerships.list_title")
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("info"),
                TextDisplay(self.bot.theme.md("title", title=title)),
                TextDisplay("\n".join(lines)),
            )
        )
        await ctx.respond(view=view, ephemeral=True)

    @discord.slash_command(
        name="partnerships_audit",
        description="[ADMIN] Audit partnerships without acting",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def partnerships_audit(self, ctx: discord.ApplicationContext) -> None:
        """Report expired and orphan partnerships (read-only)."""
        guild = ctx.guild
        if not await get_setting(self.bot.storage, guild.id, "partnerships.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "partnerships.error_disabled"),
                ephemeral=True,
            )
        renewal = await get_setting(self.bot.storage, guild.id, "partnerships.renewal_days")
        grace = await get_setting(self.bot.storage, guild.id, "partnerships.grace_days")
        expired, orphans = [], []
        for partner_id, entry in (await self.store.all(guild.id)).items():
            age = PartnershipStore.days_since(entry.get("last_renewed_at"))
            if age >= renewal + grace:
                expired.append(partner_id)
            if guild.get_member(int(entry.get("rep_id") or 0)) is None:
                orphans.append(partner_id)
        body = await self.bot.translator.t(
            guild.id,
            "partnerships.audit_body",
            expired=", ".join(f"<@{uid}>" for uid in expired) or "-",
            orphans=", ".join(f"<@{uid}>" for uid in orphans) or "-",
        )
        title = await self.bot.translator.t(guild.id, "partnerships.audit_title")
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("warning"),
                TextDisplay(self.bot.theme.md("title", title=title)),
                TextDisplay(body),
            )
        )
        await ctx.respond(view=view, ephemeral=True)

    @discord.slash_command(
        name="partnerships_clean",
        description="[ADMIN] Remove orphan partnerships",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def partnerships_clean(self, ctx: discord.ApplicationContext) -> None:
        """Delete records (and ads) whose representative left the server."""
        guild = ctx.guild
        if not await get_setting(self.bot.storage, guild.id, "partnerships.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "partnerships.error_disabled"),
                ephemeral=True,
            )
        await ctx.response.defer(ephemeral=True)
        removed = 0
        for partner_id, entry in (await self.store.all(guild.id)).items():
            if guild.get_member(int(entry.get("rep_id") or 0)) is None:
                await self._delete_ad(guild, entry)
                await self.store.remove(guild.id, partner_id)
                removed += 1
        await self._log(
            guild.id, "orphans_cleaned", "warning", [ctx.author.id],
            author=ctx.author.mention, count=removed,
        )
        await ctx.respond(
            await self.bot.translator.t(
                guild.id, "partnerships.success.cleaned", count=removed
            ),
            ephemeral=True,
        )
