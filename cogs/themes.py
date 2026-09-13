"""The ``/themes`` command: import, select, export and remove themes.

The menu is one ephemeral panel (Components V2, no embeds): the active theme,
a select to switch, an attachment import (validated before anything is
written), an export select that posts the raw YAML back to the channel and a
remove select for guild-imported files. Every write reloads the guild's theme
snapshot (:func:`rosemary.core.themes.preload_themes`) so changes apply
immediately — no restart.

Like ``cogs/moderation.py``, this module intentionally omits
``from __future__ import annotations``: py-cord inspects slash-command
annotations with ``inspect.signature`` and stringified annotations break
option parsing (the ``attachment`` option).
"""

import io

import discord
from discord.ext import commands

from rosemary.ui.containers import designer_container, divider
from rosemary.ui.menu import MenuView


def _file(data: bytes, filename: str) -> discord.File:
    return discord.File(io.BytesIO(data), filename=filename)


class ThemesMenuView(MenuView):
    """One-panel theme manager; ids are stable for the persistent fallback."""

    def __init__(self, bot, guild_id: int, *, owner_id: int) -> None:
        super().__init__(author_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.flash: str | None = None
        self.flash_color = "success"
        self.register("themes_select", self._select)
        self.register("themes_export", self._export)
        self.register("themes_remove", self._remove)
        self.register("themes_reset", self._reset)
        self.register("themes_reload", self._reload)
        self.register("themes_close", self._close)

    async def prepare(self) -> None:
        """Load the panel state, then rebuild children (``MenuView.prepare``)."""
        self._guild = self.bot.get_guild(self.guild_id)
        store = self.bot._theme_store
        self._theme_names = store.list_all(self.guild_id)
        self._active = await store.get_active(self.guild_id)
        self._imports = store.list_guild(self.guild_id)
        await super().prepare()

    async def _rerender_with_snapshot(self, interaction: discord.Interaction) -> None:
        """Apply + snapshot + redraw: selection changes never need a restart."""
        from rosemary.core.panels import on_theme_changed
        from rosemary.core.themes import preload_themes

        await preload_themes(self.bot)
        await self.prepare()
        await self.rerender(interaction)
        # Stale panels and action-button dispatch views must follow the new
        # theme immediately — no restart.
        await on_theme_changed(self.bot, self.guild_id)

    # -- handlers ------------------------------------------------------------

    async def _select(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        await self._ack(interaction)
        name = str(values[0])
        try:
            await self.bot._theme_store.set_active(self.guild_id, name)
        except Exception:
            self.flash = await self.bot.translator.t(self.guild_id, "themes.not_found")
            self.flash_color = "danger"
        else:
            self.flash = await self.bot.translator.t(
                self.guild_id, "themes.selected", theme=name
            )
            self.flash_color = "success"
        await self._rerender_with_snapshot(interaction)

    async def _export(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        await self._ack(interaction)
        name = str(values[0])
        payload = self.bot._theme_store.export_theme(self.guild_id, name)
        if payload is None:
            self.flash = await self.bot.translator.t(self.guild_id, "themes.not_found")
            self.flash_color = "danger"
            return await self.rerender(interaction)
        filename, data = payload
        try:
            await interaction.followup.send(file=_file(data, filename), ephemeral=True)
        except discord.HTTPException:
            self.flash = await self.bot.translator.t(self.guild_id, "themes.export_failed")
            self.flash_color = "danger"
        await self._rerender_with_snapshot(interaction)

    async def _remove(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        await self._ack(interaction)
        name = str(values[0])
        was_active = self._active == name
        removed = await self.bot._theme_store.remove_guild_theme(self.guild_id, name)
        if not removed:
            self.flash = await self.bot.translator.t(self.guild_id, "themes.not_found")
            self.flash_color = "danger"
        else:
            key = "themes.removed_active" if was_active else "themes.removed"
            self.flash = await self.bot.translator.t(self.guild_id, key, theme=name)
            self.flash_color = "success"
        await self._rerender_with_snapshot(interaction)

    async def _reset(self, interaction: discord.Interaction) -> None:
        await self._ack(interaction)
        await self.bot._theme_store.clear_active(self.guild_id)
        self.flash = await self.bot.translator.t(self.guild_id, "themes.reset_done")
        self.flash_color = "success"
        await self._rerender_with_snapshot(interaction)

    async def _reload(self, interaction: discord.Interaction) -> None:
        """Drop cached theme files, re-read from disk and repaint panels."""
        await self._ack(interaction)
        self.bot._theme_store.invalidate_guild(self.guild_id)
        self.flash = await self.bot.translator.t(self.guild_id, "themes.reloaded")
        self.flash_color = "success"
        await self._rerender_with_snapshot(interaction)

    async def _close(self, interaction: discord.Interaction) -> None:
        await self._ack(interaction)
        self.disable_all_items()
        await interaction.edit(view=self)
        self.stop()

    # -- rendering -----------------------------------------------------------

    async def build_items(self) -> list[discord.ui.ViewItem]:
        t = self.bot.translator.t
        theme = self.bot.theme
        emojis = theme.emojis
        t_ = self.guild_id

        # Header: title + active theme, then the one-shot feedback line.
        active_label = (
            await t(t_, "themes.active_label", theme=self._active)
            if self._active
            else await t(t_, "themes.active_none")
        )
        from rosemary.core.card_service import menu_heading_items

        parts: list[discord.ui.ViewItem] = await menu_heading_items(
            self.bot, self.guild_id, "themes.title"
        ) or [
            discord.ui.TextDisplay(theme.md("title", title=await t(t_, "themes.title"))),
            discord.ui.TextDisplay(active_label),
        ]
        if self.flash:
            parts.append(discord.ui.TextDisplay(self.flash))

        parts.append(divider("large"))

        # Manager rows: one select per action, shown only when usable.
        if self._theme_names:
            parts.append(
                discord.ui.ActionRow(
                    self.make_select(
                        custom_id="themes_select",
                        placeholder=await t(t_, "themes.select_placeholder"),
                        options=[
                            discord.SelectOption(
                                label=name, value=name, default=name == self._active
                            )
                            for name in self._theme_names[:25]
                        ],
                    )
                )
            )
        if self._imports:
            parts.append(
                discord.ui.ActionRow(
                    self.make_select(
                        custom_id="themes_remove",
                        placeholder=await t(t_, "themes.remove_placeholder"),
                        options=[
                            discord.SelectOption(label=name, value=name)
                            for name in sorted(self._imports)[:25]
                        ],
                    )
                )
            )
        if self._theme_names:
            parts.append(
                discord.ui.ActionRow(
                    self.make_select(
                        custom_id="themes_export",
                        placeholder=await t(t_, "themes.export_placeholder"),
                        options=[
                            discord.SelectOption(label=name, value=name)
                            for name in self._theme_names[:25]
                        ],
                    )
                )
            )
        if not self._theme_names:
            parts.append(discord.ui.TextDisplay(await t(t_, "themes.none_yet")))
        parts.append(discord.ui.TextDisplay(await t(t_, "themes.hint")))

        parts.append(divider("large"))
        parts.append(
            discord.ui.ActionRow(
                self.make_button(
                    custom_id="themes_reset",
                    label=await t(t_, "themes.reset_button"),
                    style=discord.ButtonStyle.secondary,
                    emoji=emojis.get("refresh") or None,
                ),
                self.make_button(
                    custom_id="themes_reload",
                    label=await t(t_, "themes.reload_button"),
                    style=discord.ButtonStyle.secondary,
                    emoji=emojis.get("clock") or None,
                ),
                self.make_button(
                    custom_id="themes_close",
                    label=await t(t_, "themes.close"),
                    style=discord.ButtonStyle.primary,
                ),
            )
        )
        return [
            designer_container(
                theme.color(self.flash_color if self.flash else "brand"), *parts
            )
        ]


class ThemesCog(commands.Cog):
    """The ``/themes`` command (decorator name ``themes``)."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @discord.slash_command(
        name="themes",
        description="Manage this server's themes",
        default_member_permissions=discord.Permissions(manage_guild=True),
        contexts={discord.InteractionContextType.guild},
    )
    async def themes(
        self,
        ctx: discord.ApplicationContext,
        attachment: discord.Attachment = None,
    ) -> None:
        """Open the theme panel; with a ``.yaml`` attachment, import it first."""
        if not ctx.response.is_done():
            await ctx.response.defer(ephemeral=True)
        import_flash = None
        if attachment is not None:
            filename = attachment.filename or "theme.yaml"
            store = self.bot._theme_store
            try:
                payload = await attachment.read()
                name = await store.import_theme(ctx.guild_id, filename, payload)
            except Exception as exc:
                reason = await self._rejection_reason(ctx.guild_id, exc)
                await ctx.respond(
                    await self.bot.translator.t(
                        ctx.guild_id, "themes.rejected", reason=reason
                    ),
                    ephemeral=True,
                )
                return
            import_flash = await self.bot.translator.t(
                ctx.guild_id, "themes.imported", theme=name
            )
            from rosemary.core.panels import on_theme_changed

            await on_theme_changed(self.bot, ctx.guild_id)
        view = ThemesMenuView(self.bot, ctx.guild_id, owner_id=ctx.author.id)
        if import_flash:
            view.flash = import_flash
            view.flash_color = "success"
        await view.prepare()
        from rosemary.core.card_service import trace_card_path

        # Heading is theme-customizable (card.themes.title) — trace the path.
        await trace_card_path(self.bot, ctx.guild_id, "themes.title")
        await ctx.respond(view=view, ephemeral=True)

    async def _rejection_reason(self, guild_id: int, exc: Exception) -> str:
        """Translate a ThemeError into the guild language (or a short fallback)."""
        from rosemary.core.v2_convert import ThemeError

        if isinstance(exc, ThemeError) and exc.issues:
            codes = ", ".join(issue.code for issue in exc.issues[:3])
            return await self.bot.translator.t(
                guild_id, "themes.rejection_codes", codes=codes
            )
        return type(exc).__name__
