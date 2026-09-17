"""Color panel: pickable color roles posted as a public panel.

A panel in the configured channel offers the guild's colors (seeded pastels
on first use). Picking a color grants its role and drops the previous one;
picking it again removes it (toggle). The manager lives in
/settings > Painel de Cores ("Gerenciar cores"): add/edit/reorder/remove
entries, attach existing roles (batch multi-select), create roles from
scratch, and re-post. Every manager mutation repaints the posted panel,
the same contract as the ticket panel.

Architecture mirrors tickets:

* :mod:`rosemary.core.colors` owns persistence;
* the picker is persistent: the boot path registers a view carrying the
  same ``colors_pick:<id>`` custom_ids the posted panel shows, so clicks
  survive restarts;
* the panel repaints through the :mod:`rosemary.core.panels` registry, so
  a theme change or a channel/setting edit refreshes it in place;
* the card is the ``colors.panel`` CardSpec: a theme may restyle the frame
  (V2 or embed), while the chunk containers + picker rows stay code (they
  carry the Discord-bound callbacks).
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import replace

import discord
from discord.ext import commands

from rosemary.core.card_service import CardPayload
from rosemary.core.colors import PASTEL_SEEDS, ColorStore
from rosemary.core.settings import get_setting
from rosemary.ui.colors_panel import ColorPickerView, chunk_containers, chunks_of

log = logging.getLogger(__name__)


class ColorsCog(commands.Cog):
    """Pickable color roles behind a public panel."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = ColorStore(bot.storage.data_dir)
        self._started = False

    async def start(self) -> None:
        """Restore the picker views and repaint once per process."""
        if self._started:
            return
        self._started = True
        from rosemary.core.panels import register as register_panel

        register_panel(
            "colors",
            self.repaint_panel,
            setting_keys=("colors.enabled", "colors.panel_channel"),
        )
        for guild in self.bot.guilds:
            try:
                await self._restore_guild(guild)
            except Exception as exc:
                log.error("Color panel restore failed in %s: %s", guild.id, exc)

    async def _restore_guild(self, guild: discord.Guild) -> None:
        if not await self.enabled(guild.id):
            return
        await self.seed_if_needed(guild.id)
        entries = await self.store.list_colors(guild.id)
        # The registered dispatcher must CARRY the picker rows: py-cord's
        # view store indexes only real children (walk_children), so an empty
        # view registers nothing and every click dies as "did not respond".
        picker = ColorPickerView(self.bot, guild.id, entries)
        mode = await self.picker_mode(guild.id)
        if mode == "select":
            picker.add_item(await picker.select_row())
        else:
            per = await self.per_container(guild.id)
            for chunk in chunks_of(entries, per):
                for row in picker.rows_for([entry for _n, entry in chunk]):
                    picker.add_item(row)
        self.bot.add_view(picker)
        await self.repaint_panel(self.bot, guild.id)

    # helpers ====================

    async def enabled(self, guild_id: int) -> bool:
        return bool(await get_setting(self.bot.storage, guild_id, "colors.enabled"))

    async def per_container(self, guild_id: int) -> int:
        return int(
            await get_setting(self.bot.storage, guild_id, "colors.per_container")
        )

    async def picker_mode(self, guild_id: int) -> str:
        return str(await get_setting(self.bot.storage, guild_id, "colors.picker"))

    async def _panel_channel(self, guild: discord.Guild):
        channel_id = await get_setting(
            self.bot.storage, guild.id, "colors.panel_channel"
        )
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    def _can_manage_roles(self, guild: discord.Guild) -> bool:
        permissions = getattr(guild.me, "guild_permissions", None)
        return bool(permissions and permissions.manage_roles)

    async def _seed_names(self, guild_id: int) -> dict[str, str]:
        """Slug -> translated label for the pastel seed."""
        return {
            slug: await self.bot.translator.t(guild_id, f"colors.defaults.{slug}")
            for slug, _hex in PASTEL_SEEDS
        }

    async def _ensure_seed_roles(
        self, guild: discord.Guild, entries: list
    ) -> list:
        """Create a Discord role for every seed entry missing one.

        The bot owns seeding, so the admin never hand-attaches roles for the
        defaults: each entry gets a role named after the color with that hex,
        positioned below the bot's top role. Failures (missing permission,
        role cap) keep the entry role-less; the panel shows it as detached
        and a later manager save can retry.
        """
        if not self._can_manage_roles(guild):
            log.warning(
                "color seed skipped in %s: missing Manage Roles", guild.id
            )
            return entries
        created: dict[str, int] = {}
        for entry in entries:
            if entry.role_id is not None:
                continue
            if guild.get_role(entry.role_id) if entry.role_id else False:
                continue
            try:
                role = await guild.create_role(
                    name=entry.name,
                    colour=discord.Colour(
                        int(entry.color.lstrip("#"), 16)
                    ),
                    reason="Color panel seed",
                )
                created[entry.id] = role.id
            except discord.HTTPException as exc:
                log.warning(
                    "color seed role %r failed in %s: %s",
                    entry.name,
                    guild.id,
                    exc,
                )
        if not created:
            return entries
        updated = [
            replace(entry, role_id=created[entry.id])
            if entry.id in created
            else entry
            for entry in entries
        ]
        await self.store.set_colors(guild.id, updated)
        return updated

    # panel ====================

    async def build_panel_view(self, guild_id: int, entries=None):
        """Default frame + chunk containers (with images) + picker controls.

        Returns ``(CardPayload, ColorPickerView)``. The picker rows are the
        only components with callbacks; the frame and chunk containers are
        static V2 (chunk buttons are code-built so each row carries the
        dispatcher's handlers).
        """
        from rosemary.core.cards import build_items
        from rosemary.core.themes import theme_for
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        if entries is None:
            entries = await self.store.list_colors(guild_id)
        per = await self.per_container(guild_id)
        theme = self.bot.theme
        t = self.bot.translator.t
        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                theme.color("brand"),
                TextDisplay(theme.md("title", title=await t(guild_id, "colors.panel.title"))),
                TextDisplay(await t(guild_id, "colors.panel.text", count=len(entries))),
            )
        )
        files, documents, row_docs = chunk_containers(
            self.bot, guild_id, entries, per
        )
        for doc in documents:
            try:
                for item in build_items(theme_for(self.bot, guild_id), {"blocks": [doc]}):
                    view.add_item(item)
            except Exception as exc:
                log.warning("color panel chunk skipped in %s: %s", guild_id, exc)
        picker = ColorPickerView(self.bot, guild_id, entries)
        mode = await self.picker_mode(guild_id)
        if mode == "select":
            view.add_item(await picker.select_row())
        else:
            # Picker rows are top-level documents (outside every container):
            # buttons belong to the message, not to the color card.
            for row_doc in row_docs:
                try:
                    for item in build_items(
                        theme_for(self.bot, guild_id), {"blocks": [row_doc]}
                    ):
                        view.add_item(item)
                except Exception as exc:
                    log.warning("color panel row skipped in %s: %s", guild_id, exc)
        return CardPayload(view=view, files=files), picker

    async def _panel_payload(self, guild_id: int, *, trace: bool = False):
        """Themed frame (or default) + chunks + picker, ready to send.

        ``trace=True`` (real sends) routes through :func:`render_card_message`
        so ``debug.card_paths`` fires; internal repaints stay silent. An
        embed-form theme frame can only take classic rows: the picker rides
        as buttons/select and the chunk images are dropped (embeds carry no
        V2 galleries).
        """
        from rosemary.core.card_service import render_card_message

        if trace:
            payload, _allowed = await render_card_message(
                self.bot, guild_id, "colors.panel"
            )
            if payload is not None and payload.embed is not None:
                entries = await self.store.list_colors(guild_id)
                picker = ColorPickerView(self.bot, guild_id, entries)
                mode = await self.picker_mode(guild_id)
                if mode == "select":
                    payload.view.add_item(await picker.select_row())
                else:
                    for row in picker.classic_rows_all(entries):
                        payload.view.add_item(row)
                return payload
            if payload is not None:
                # V2 themed frame: splice chunks + picker onto it.
                entries = await self.store.list_colors(guild_id)
                files, documents, row_docs = chunk_containers(
                    self.bot, guild_id, entries, await self.per_container(guild_id)
                )
                from rosemary.core.cards import build_items
                from rosemary.core.themes import theme_for

                for doc in documents:
                    try:
                        for item in build_items(
                            theme_for(self.bot, guild_id), {"blocks": [doc]}
                        ):
                            payload.view.add_item(item)
                    except Exception as exc:
                        log.warning("color panel chunk skipped in %s: %s", guild_id, exc)
                picker = ColorPickerView(self.bot, guild_id, entries)
                mode = await self.picker_mode(guild_id)
                if mode == "select":
                    payload.view.add_item(await picker.select_row())
                else:
                    for row_doc in row_docs:
                        try:
                            for item in build_items(
                                theme_for(self.bot, guild_id), {"blocks": [row_doc]}
                            ):
                                payload.view.add_item(item)
                        except Exception as exc:
                            log.warning(
                                "color panel row skipped in %s: %s", guild_id, exc
                            )
                payload = CardPayload(view=payload.view, files=files)
                return payload
        return await self.build_panel_view(guild_id)

    async def _post_panel(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        replace_id: int | None = None,
    ) -> None:
        """Post the panel, sweeping away every earlier panel message.

        Deletes the stored ``replace_id`` AND every other recorded panel id
        (older posts predate the single-panel invariant and would stay on
        the channel forever, stacked under the fresh one)."""
        from rosemary.core.mentions import allowed_for_ids

        stale = set(await self.store.known_panels(guild.id))
        if replace_id is not None:
            stale.add(replace_id)
        payload, _picker = await self._panel_payload(guild.id, trace=True)
        message = await channel.send(
            **payload.message_kwargs(),
            allowed_mentions=await allowed_for_ids(self.bot, guild.id, "colors.panel"),
        )
        stale.discard(message.id)
        for panel_id in stale:
            with contextlib.suppress(discord.NotFound, discord.HTTPException):
                await channel.get_partial_message(panel_id).delete()
        await self.store.set_panel(guild.id, message.id)

    async def repaint_panel(self, bot, guild_id: int) -> None:
        """Refresh the posted panel (theme/setting/manager changed).

        Imageless panels edit in place; panels carrying generated images
        re-post, because ``PartialMessage.edit`` is JSON-only and cannot
        upload files. A deleted or unreachable message always falls back to
        a fresh post; a disabled feature or missing channel leaves the panel
        as-is. Registered in :mod:`rosemary.core.panels`.
        """
        if not await self.enabled(guild_id):
            return
        guild = bot.get_guild(guild_id)
        channel = await self._panel_channel(guild) if guild else None
        if guild is None or channel is None:
            return
        payload, _picker = await self._panel_payload(guild_id)
        has_files = bool(payload.message_kwargs().get("files"))
        panel_id = await self.store.get_panel(guild_id)
        if panel_id is not None and not has_files:
            try:
                await channel.get_partial_message(panel_id).edit(
                    **payload.message_kwargs()
                )
                return
            except (discord.NotFound, discord.HTTPException):
                pass  # message gone - re-post below
        await self._post_panel(guild, channel, replace_id=panel_id if has_files else None)

    async def seed_if_needed(self, guild_id: int) -> list:
        """First open seeds 20 pastels AND creates their Discord roles."""
        entries = await self.store.seed_pastels(
            guild_id, await self._seed_names(guild_id)
        )
        guild = self.bot.get_guild(guild_id)
        if guild is not None and any(e.role_id is None for e in entries):
            entries = await self._ensure_seed_roles(guild, entries)
        return entries

    # commands ====================

    @discord.slash_command(
        name="colors_panel",
        description="[ADMIN] Post the color panel",
        default_member_permissions=discord.Permissions(administrator=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def colors_panel(self, ctx: discord.ApplicationContext) -> None:
        """Post (or refresh) the panel in the configured channel."""
        guild = ctx.guild
        if not await self.enabled(guild.id):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "colors.error_disabled"),
                ephemeral=True,
            )
        channel = await self._panel_channel(guild)
        if channel is None:
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "colors.error_no_panel"),
                ephemeral=True,
            )
        if not self._can_manage_roles(guild):
            return await ctx.respond(
                await self.bot.translator.t(guild.id, "colors.error_no_perms"),
                ephemeral=True,
            )
        await ctx.response.defer(ephemeral=True)
        # Re-posting replaces the stored panel message: two live panels would
        # both render picker rows and overlap on the channel.
        await self._post_panel(
            guild, channel, replace_id=await self.store.get_panel(guild.id)
        )
        await ctx.respond(
            await self.bot.translator.t(guild.id, "colors.panel_posted"),
            ephemeral=True,
        )

    @discord.slash_command(
        name="my_colors",
        description="List the available colors",
        contexts={discord.InteractionContextType.guild},
    )
    async def my_colors(self, ctx: discord.ApplicationContext) -> None:
        """Ephemeral listing of the panel colors (member's current one marked)."""
        guild_id = ctx.guild_id
        entries = await self.store.list_colors(guild_id)
        if not entries:
            return await ctx.respond(
                await self.bot.translator.t(guild_id, "colors.list_empty"),
                ephemeral=True,
            )
        member = ctx.guild.get_member(ctx.user.id) if ctx.guild else None
        wearing = {role.id for role in member.roles} if member else set()
        lines = []
        for position, entry in enumerate(entries, start=1):
            mark = ""
            if entry.role_id and entry.role_id in wearing:
                mark = await self.bot.translator.t(guild_id, "colors.list_current")
            lines.append(f"**{position}.** {entry.name}{mark}")
        theme = self.bot.theme
        from rosemary.ui.containers import DesignerView, TextDisplay, designer_container

        view = DesignerView(store=False)
        view.add_item(
            designer_container(
                theme.color("info"),
                TextDisplay(
                    theme.md(
                        "title",
                        title=await self.bot.translator.t(guild_id, "colors.list_title"),
                    )
                ),
                TextDisplay("\n".join(lines)),
            )
        )
        await ctx.respond(view=view, ephemeral=True)
