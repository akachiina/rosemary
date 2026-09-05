"""Ticket system: panel-driven support channels with transcripts.

A panel posted in the configured channel offers four ticket types. Opening
creates a locked-down text channel for the requester, with close / transcript
/ reopen / delete buttons. Transcripts are plain-text files sent to the log
channel. Views are persistent (fixed ``custom_id`` scheme) and re-registered
on every boot so buttons survive restarts.

Note: this file intentionally does NOT import ``from __future__ import
annotations``: py-cord inspects slash-command annotations with
``inspect.signature`` and stringified ``discord.Option(...)`` annotations would
break option parsing at runtime (same reason as ``cogs/moderation.py``).
"""

import contextlib
import io
import logging
import re

import discord
from discord.ext import commands

from rosemary.core.cards import log_description, maybe_view
from rosemary.core.debug import send_channel_log
from rosemary.core.settings import get_setting
from rosemary.core.tickets import CLOSED, OPEN, TicketStore
from rosemary.ui.containers import TextDisplay, designer_container
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)

TICKET_TYPES = ("report", "confidential", "partnership", "boost")

_OPEN_ID = "tickets_open"
_CONFIRM_PREFIX = "tickets_confirm"
_CLOSE_PREFIX = "tickets_close"
_TRANSCRIPT_PREFIX = "tickets_transcript"
_REOPEN_PREFIX = "tickets_reopen"
_DELETE_PREFIX = "tickets_delete"


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:20] or "ticket"


class TicketsCog(commands.Cog):
    """Panel-driven ticket channels."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = TicketStore(bot.storage.data_dir)
        self._started = False

    async def start(self) -> None:
        """Restore panels and persistent views once per process."""
        if self._started:
            return
        self._started = True
        for guild in self.bot.guilds:
            try:
                await self._restore_guild(guild)
            except Exception as exc:
                log.error("Ticket restore failed in %s: %s", guild.id, exc)

    async def _restore_guild(self, guild: discord.Guild) -> None:
        if not await get_setting(self.bot.storage, guild.id, "tickets.enabled"):
            return
        self.bot.add_view(TicketPanelView(self.bot, guild.id))
        for channel_id in await self.store.open_tickets(guild.id):
            self.bot.add_view(TicketActionsView(self.bot, guild.id, channel_id))
        panel_id = await self.store.get_panel(guild.id)
        channel = await self._panel_channel(guild)
        if channel is None:
            return
        if panel_id is not None:
            with contextlib.suppress(discord.NotFound, discord.HTTPException):
                await channel.fetch_message(panel_id)
                return
        await self._post_panel(guild, channel)

    # -- helpers ---------------------------------------------------------------

    async def _panel_channel(self, guild: discord.Guild):
        channel_id = await get_setting(self.bot.storage, guild.id, "tickets.panel_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _category(self, guild: discord.Guild):
        category_id = await get_setting(self.bot.storage, guild.id, "tickets.category")
        channel = guild.get_channel(category_id) if category_id else None
        return channel if isinstance(channel, discord.CategoryChannel) else None

    def _can_manage(self, guild: discord.Guild) -> bool:
        permissions = getattr(guild.me, "guild_permissions", None)
        return bool(permissions and permissions.manage_channels)

    async def _type_label(self, guild_id: int, ticket_type: str) -> str:
        return await self.bot.translator.t(
            guild_id, f"tickets.types.{ticket_type}.label"
        )

    async def _post_panel(self, guild: discord.Guild, channel: discord.TextChannel) -> None:
        view = await maybe_view(self.bot, guild.id, "tickets.panel")
        if view is None:
            view = await self._build_panel_view(guild.id)
        view.add_item(await TicketPanelView(self.bot, guild.id).open_row())
        message = await channel.send(view=view)
        await self.store.set_panel(guild.id, message.id)

    async def _build_panel_view(self, guild_id: int):
        from rosemary.ui.containers import DesignerView

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("brand"),
                TextDisplay(
                    self.bot.theme.md(
                        "title",
                        title=await self.bot.translator.t(guild_id, "tickets.panel.title"),
                    )
                ),
                TextDisplay(await self.bot.translator.t(guild_id, "tickets.panel.text")),
            )
        )
        return view

    async def _log(
        self,
        guild_id: int,
        key: str,
        color: str,
        user_ids: list[int] | tuple[int, ...] = (),
        **variables,
    ) -> None:
        await send_channel_log(
            self.bot,
            guild_id,
            await self.bot.translator.t(guild_id, f"tickets.logs.{key}.title"),
            await log_description(
                self.bot, guild_id, f"tickets.logs.{key}.description", **variables
            ),
            color=color,
            card_key=f"tickets.logs.{key}.description",
            mention_user_ids=list(user_ids),
        )

    async def _actor_allowed(self, guild: discord.Guild, user_id: int, owner_id: int) -> bool:
        if user_id == owner_id:
            return True
        member = guild.get_member(user_id)
        return bool(
            member is not None and member.guild_permissions.manage_channels
        )

    # -- ticket actions ------------------------------------------------------------

    async def create_ticket(
        self, guild: discord.Guild, owner: discord.Member, ticket_type: str
    ) -> tuple[bool, str]:
        """Create a ticket channel. Returns ``(ok, message_key_or_channel_id)``."""
        guild_id = guild.id
        if not self._can_manage(guild):
            return False, "tickets.error_no_perms"
        category = await self._category(guild)
        if category is None:
            return False, "tickets.error_no_category"
        limit = await get_setting(
            self.bot.storage, guild_id, "tickets.max_open_per_user"
        )
        if await self.store.user_open_count(guild_id, owner.id) >= limit:
            return False, "tickets.error_max_open"
        prefix = await self.bot.translator.t(
            guild_id, f"tickets.types.{ticket_type}.prefix"
        )
        name = f"{_slug(prefix)}-{_slug(owner.display_name)}"[:90]
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            owner: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        try:
            channel = await guild.create_text_channel(
                name, category=category, overwrites=overwrites, reason="Ticket opened"
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Ticket channel create failed in %s: %s", guild_id, exc)
            return False, "tickets.error_no_perms"
        await self.store.open_ticket(guild_id, channel.id, owner.id, ticket_type)
        self.bot.add_view(TicketActionsView(self.bot, guild_id, channel.id))
        await self._post_intro(guild, channel, owner, ticket_type)
        await self._log(
            guild_id,
            "opened",
            "info",
            [owner.id],
            user=owner.mention,
            type=await self._type_label(guild_id, ticket_type),
            channel=channel.mention,
        )
        return True, str(channel.id)

    async def _post_intro(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        owner: discord.Member,
        ticket_type: str,
    ) -> None:
        from rosemary.core.mentions import mentions_for

        view = await maybe_view(self.bot, guild.id, "tickets.created")
        if view is None:
            t = self.bot.translator.t
            view = await self._intro_view(
                guild.id,
                await t(
                    guild.id,
                    "tickets.created.text",
                    user=owner.mention,
                    type=await self._type_label(guild.id, ticket_type),
                ),
            )
        view.add_item(TicketActionsView(self.bot, guild.id, channel.id).action_row())
        await channel.send(
            view=view,
            allowed_mentions=await mentions_for(self.bot, guild.id, "tickets.created"),
        )

    async def _intro_view(self, guild_id: int, body: str):
        from rosemary.ui.containers import DesignerView

        t = self.bot.translator.t
        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                self.bot.theme.color("brand"),
                TextDisplay(
                    self.bot.theme.md(
                        "title", title=await t(guild_id, "tickets.created.title")
                    )
                ),
                TextDisplay(body),
            )
        )
        return view

    async def close_ticket(self, guild: discord.Guild, channel_id: int) -> bool:
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return False
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            overwrites = channel.overwrites
            overwrites[guild.default_role] = discord.PermissionOverwrite(
                view_channel=False
            )
            await channel.edit(overwrites=overwrites, reason="Ticket closed")
        ok = await self.store.set_status(guild.id, channel_id, CLOSED)
        if ok:
            ticket = await self.store.get_ticket(guild.id, channel_id) or {}
            await self._log(
                guild.id,
                "closed",
                "warning",
                [int(ticket.get("owner_id") or 0)],
                channel=channel.mention,
            )
        return ok

    async def reopen_ticket(self, guild: discord.Guild, channel_id: int) -> bool:
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return False
        ticket = await self.store.get_ticket(guild.id, channel_id)
        if not ticket:
            return False
        owner = guild.get_member(int(ticket.get("owner_id") or 0))
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            overwrites = channel.overwrites
            overwrites[guild.default_role] = discord.PermissionOverwrite(
                view_channel=False
            )
            if owner is not None:
                overwrites[owner] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
            await channel.edit(overwrites=overwrites, reason="Ticket reopened")
        ok = await self.store.set_status(guild.id, channel_id, OPEN)
        if ok:
            await self._log(
                guild.id, "reopened", "success", [int(ticket.get("owner_id") or 0)],
                channel=channel.mention,
            )
        return ok

    async def build_transcript(self, channel: discord.TextChannel) -> discord.File:
        lines = [f"# {channel.name}", ""]
        async for message in channel.history(limit=1000, oldest_first=True):
            stamp = message.created_at.strftime("%Y-%m-%d %H:%M")
            author = str(message.author)
            body = message.content or ""
            if message.attachments:
                body += " " + " ".join(a.url for a in message.attachments)
            lines.append(f"[{stamp}] {author}: {body}".rstrip())
        data = "\n".join(lines).encode("utf-8", errors="replace")
        return discord.File(io.BytesIO(data), filename=f"transcript-{channel.id}.txt")

    async def _log_file(
        self, guild_id: int, title_key: str, description_key: str, file: discord.File,
        **variables,
    ) -> bool:
        from rosemary.core.settings import get_setting as _get

        if not await _get(self.bot.storage, guild_id, "logging.enabled"):
            return False
        channel_id = await _get(self.bot.storage, guild_id, "logging.channel")
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            description = await log_description(
                self.bot, guild_id, description_key, **variables
            )
            title = await self.bot.translator.t(guild_id, title_key)
            await channel.send(
                f"**{title}**\n{description}",
                file=file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            log.warning("Could not send transcript to %s: %s", channel_id, exc)
            return False
        return True

    # -- commands ------------------------------------------------------------

    @discord.slash_command(
        name="tickets_panel",
        description="[ADMIN] Post the ticket panel",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def tickets_panel(self, ctx: discord.ApplicationContext) -> None:
        """Post (or refresh) the ticket panel in the configured channel."""
        guild = ctx.guild
        if not await get_setting(self.bot.storage, guild.id, "tickets.enabled"):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "tickets.error_disabled"),
                ephemeral=True,
            )
        channel = await self._panel_channel(guild)
        if channel is None:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "tickets.error_no_panel"),
                ephemeral=True,
            )
        if not self._can_manage(guild):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "tickets.error_no_perms"),
                ephemeral=True,
            )
        await ctx.response.defer(ephemeral=True)
        await self._post_panel(guild, channel)
        self.bot.add_view(TicketPanelView(self.bot, guild.id))
        await ctx.respond(
            await self.bot.translator.t(guild.id, "tickets.panel_posted"),
            ephemeral=True,
        )

    # -- listeners -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._restore_guild(guild)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        try:
            await self.store.delete_ticket(guild.id, channel.id)
        except Exception as exc:
            log.error("Ticket cleanup failed for %s: %s", channel.id, exc)


class TicketPanelView(MenuView):
    """Persistent panel: pick a ticket type to open."""

    def __init__(self, bot, guild_id: int) -> None:
        super().__init__(author_id=None, timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.register(_OPEN_ID, self._pick_type)

    async def open_row(self):
        """Translated type picker (built lazily, labels need the catalog)."""
        options = [
            discord.SelectOption(
                label=await self._t(f"tickets.types.{ticket_type}.label"),
                value=ticket_type,
                description=await self._t(f"tickets.types.{ticket_type}.description"),
            )
            for ticket_type in TICKET_TYPES
        ]
        return self.make_select(
            custom_id=_OPEN_ID,
            placeholder=await self._t("tickets.panel.placeholder"),
            options=options,
        )

    async def _t(self, key: str, **kwargs) -> str:
        return await self.bot.translator.t(self.guild_id, key, **kwargs)

    async def prepare(self) -> None:
        pass

    async def _pick_type(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values or values[0] not in TICKET_TYPES:
            return
        ticket_type = values[0]
        label = await self._t(f"tickets.types.{ticket_type}.label")
        view = MenuView(author_id=interaction.user.id)
        view.add_item(
            TextDisplay(
                await self._t("tickets.confirm_text", type=label)
            )
        )

        async def confirm(inner: discord.Interaction) -> None:
            await inner.response.defer(ephemeral=True)
            cog = self.bot.get_cog("TicketsCog")
            guild = inner.guild
            member = guild.get_member(inner.user.id) if guild else None
            if cog is None or guild is None or member is None:
                return
            ok, detail = await cog.create_ticket(guild, member, ticket_type)
            if ok:
                channel = guild.get_channel(int(detail))
                message = await self._t(
                    "tickets.created_confirm",
                    channel=channel.mention if channel else detail,
                )
            else:
                message = await self._t(detail)
            view.clear_items()
            view.add_item(TextDisplay(message))
            view.stop()
            await inner.edit(view=view)

        async def cancel(inner: discord.Interaction) -> None:
            await inner.response.defer(ephemeral=True)
            view.clear_items()
            view.add_item(TextDisplay(await self._t("tickets.cancelled")))
            view.stop()
            await inner.edit(view=view)

        view.register(f"{_CONFIRM_PREFIX}:{ticket_type}", confirm)
        view.register("tickets_cancel", cancel)
        view.add_item(
            discord.ui.ActionRow(
                view.make_button(
                    custom_id=f"{_CONFIRM_PREFIX}:{ticket_type}",
                    label=await self._t("tickets.confirm"),
                    style=discord.ButtonStyle.primary,
                ),
                view.make_button(
                    custom_id="tickets_cancel",
                    label=await self._t("tickets.cancel"),
                    style=discord.ButtonStyle.secondary,
                ),
            )
        )
        await interaction.response.send_message(view=view, ephemeral=True)


class TicketActionsView(MenuView):
    """Persistent per-ticket buttons: close / transcript / reopen / delete."""

    def __init__(self, bot, guild_id: int, channel_id: int) -> None:
        super().__init__(author_id=None, timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.register(_CLOSE_PREFIX, self._close)
        self.register(_TRANSCRIPT_PREFIX, self._transcript)
        self.register(_REOPEN_PREFIX, self._reopen)
        self.register(_DELETE_PREFIX, self._delete)

    def action_row(self):
        return discord.ui.ActionRow(
            self.make_button(custom_id=f"{_CLOSE_PREFIX}:{self.channel_id}", label="🔒"),
            self.make_button(
                custom_id=f"{_TRANSCRIPT_PREFIX}:{self.channel_id}", label="📝"
            ),
            self.make_button(
                custom_id=f"{_REOPEN_PREFIX}:{self.channel_id}", label="🔓"
            ),
            self.make_button(
                custom_id=f"{_DELETE_PREFIX}:{self.channel_id}",
                label="🗑️",
                style=discord.ButtonStyle.danger,
            ),
        )

    async def _t(self, key: str, **kwargs) -> str:
        return await self.bot.translator.t(self.guild_id, key, **kwargs)

    def _ids(self, interaction: discord.Interaction) -> tuple[int, int]:
        channel_id = int(interaction.custom_id.split(":")[-1])
        return self.guild_id, channel_id

    async def _ticket_or_error(
        self, interaction: discord.Interaction
    ):
        cog = self.bot.get_cog("TicketsCog")
        guild_id, channel_id = self._ids(interaction)
        guild = self.bot.get_guild(guild_id)
        if cog is None or guild is None:
            return None, None, None
        ticket = await cog.store.get_ticket(guild_id, channel_id)
        if ticket is None:
            await interaction.response.send_message(
                await self._t("tickets.error_not_found"), ephemeral=True
            )
            return None, None, None
        if not await cog._actor_allowed(guild, interaction.user.id, int(ticket["owner_id"])):
            await interaction.response.send_message(
                await self._t("tickets.error_forbidden"), ephemeral=True
            )
            return None, None, None
        return cog, guild, ticket

    async def _close(self, interaction: discord.Interaction) -> None:
        resolved = await self._ticket_or_error(interaction)
        cog, guild, _ticket = resolved
        if cog is None:
            return
        await interaction.response.defer(ephemeral=True)
        _, channel_id = self._ids(interaction)
        if await cog.close_ticket(guild, channel_id):
            message = await self._t("tickets.closed_done")
        else:
            message = await self._t("tickets.error_not_found")
        await interaction.followup.send(message, ephemeral=True)

    async def _transcript(self, interaction: discord.Interaction) -> None:
        resolved = await self._ticket_or_error(interaction)
        cog, guild, _ticket = resolved
        if cog is None:
            return
        await interaction.response.defer(ephemeral=True)
        _, channel_id = self._ids(interaction)
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return await interaction.followup.send(
                await self._t("tickets.error_not_found"), ephemeral=True
            )
        file = await cog.build_transcript(channel)
        ok = await cog._log_file(
            guild.id,
            "tickets.logs.transcript.title",
            "tickets.logs.transcript.description",
            file,
            channel=channel.mention,
            moderator=interaction.user.mention,
        )
        key = "tickets.transcript_sent" if ok else "tickets.transcript_failed"
        await interaction.followup.send(await self._t(key), ephemeral=True)

    async def _reopen(self, interaction: discord.Interaction) -> None:
        resolved = await self._ticket_or_error(interaction)
        cog, guild, _ticket = resolved
        if cog is None:
            return
        await interaction.response.defer(ephemeral=True)
        _, channel_id = self._ids(interaction)
        if await cog.reopen_ticket(guild, channel_id):
            message = await self._t("tickets.reopened_done")
        else:
            message = await self._t("tickets.error_not_found")
        await interaction.followup.send(message, ephemeral=True)

    async def _delete(self, interaction: discord.Interaction) -> None:
        resolved = await self._ticket_or_error(interaction)
        cog, guild, ticket = resolved
        if cog is None:
            return
        _, channel_id = self._ids(interaction)
        if ticket.get("status") != CLOSED:
            return await interaction.response.send_message(
                await self._t("tickets.error_close_first"), ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        channel = guild.get_channel(channel_id)
        await cog.store.delete_ticket(guild.id, channel_id)
        await cog._log(
            guild.id,
            "deleted",
            "danger",
            [int(ticket.get("owner_id") or 0)],
            channel=f"#{channel_id}",
        )
        if isinstance(channel, discord.TextChannel):
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await channel.delete(reason="Ticket deleted")
        await interaction.followup.send(
            await self._t("tickets.deleted_done"), ephemeral=True
        )


def setup(bot) -> None:
    bot.add_cog(TicketsCog(bot))
