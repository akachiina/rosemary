"""The ``/themes`` command: import, select, export and remove themes.

The menu is one ephemeral panel (Components V2, no embeds): the active theme,
a select to switch, an attachment import (validated before anything is
written), an export select that posts the raw YAML back to the channel and a
remove select for guild-imported files. Every write reloads the guild's theme
snapshot (:func:`rosemary.core.themes.preload_themes`) so changes apply
immediately: no restart.

The panel also **inspects** a theme: the *validate* button reports what is
wrong with it (the cards it names, the copy it points at, the colors it
uses), the *diff* button says what it changes against the built-in one and
the *preview* renders one of its cards in each shape (the plain text, the
classic embed, the Components V2).

Like ``cogs/moderation.py``, this module intentionally omits
``from __future__ import annotations``: py-cord inspects slash-command
annotations with ``inspect.signature`` and stringified annotations break
option parsing (the ``attachment`` option).
"""

import contextlib
import io

import discord
from discord.ext import commands

from rosemary.ui.kit import ActionRow, Panel, TextDisplay, divider

#: The shapes the preview offers (the card pipeline's own three).
_PREVIEW_FORMS = ("text", "embed", "card")

#: How many cards the preview select offers (Discord's select cap).
_MAX_PREVIEW_CARDS = 25

#: The icon of the verb each flash reports (the notice layer owns the icon:
#: the copy never brings one of its own). A failure has no verb, so it keeps
#: the icon of its tone.
_FLASH_ICONS: dict[str, str] = {
    "themes.imported": "register",
    "themes.selected": "palette",
    "themes.removed": "trash",
    "themes.removed_active": "trash",
    "themes.reset_done": "reset",
    "themes.reloaded": "refresh",
    "themes.validate_ok": "check",
}


def _file(data: bytes, filename: str) -> discord.File:
    return discord.File(io.BytesIO(data), filename=filename)


class ThemesMenuView(Panel):
    """One-panel theme manager; ids are stable for the persistent fallback."""

    #: The panel owns its button rows: no shared navigation (the close button
    #: rides the flow row, as it always did).
    nav_actions: tuple[str, ...] = ()

    def __init__(self, bot, guild_id: int, *, owner_id: int) -> None:
        super().__init__(bot, guild_id, author_id=owner_id)
        self.flash: str | None = None
        self.flash_color = "success"
        #: The theme the panel inspects (the active one by default).
        self._inspect: str | None = None
        #: The card the preview renders.
        self._preview_key: str | None = None
        #: The shape the preview renders.
        self._preview_form = "card"
        self.register("themes_select", self._select)
        self.register("themes_export", self._export)
        self.register("themes_remove", self._remove)
        self.register("themes_reset", self._reset)
        self.register("themes_reload", self._reload)
        self.register("themes_close", self._close)
        self.register("themes_inspect", self._inspect_select)
        self.register("themes_preview_card", self._preview_card_select)
        self.register("themes_preview_form", self._preview_form_select)
        self.register("themes_validate", self._validate)
        self.register("themes_diff", self._diff)
        self.register("themes_preview", self._preview)

    async def prepare(self) -> None:
        """Load the panel state, then rebuild children (``MenuView.prepare``)."""
        self._guild = self.bot.get_guild(self.guild_id)
        store = self.bot._theme_store
        self._theme_names = store.list_all(self.guild_id)
        self._active = await store.get_active(self.guild_id)
        self._imports = store.list_guild(self.guild_id)
        if self._inspect not in self._theme_names:
            self._inspect = (
                self._active or (self._theme_names[0] if self._theme_names else None)
            )
        self._preview_options = self._preview_cards()
        if self._preview_key not in self._preview_options:
            self._preview_key = (
                self._preview_options[0] if self._preview_options else None
            )
        await super().prepare()

    def _preview_cards(self) -> list[str]:
        """The cards the preview offers: the theme's own first, then the rest."""
        from rosemary.core.theme_package import default_package

        keys: list[str] = []
        theme = self._inspected_theme()
        for key in sorted(getattr(theme, "deltas", {}) or {}):
            if key not in keys:
                keys.append(key)
        for key in sorted(getattr(theme, "cards", {}) or {}):
            if key not in keys:
                keys.append(key)
        for key in default_package().card_keys():
            if key not in keys:
                keys.append(key)
        return keys[:_MAX_PREVIEW_CARDS]

    def _inspected_theme(self):
        """The theme the panel inspects (``None`` when it does not load)."""
        if not self._inspect:
            return None
        from rosemary.core.v2_convert import ThemeError

        try:
            return self.bot._theme_store.load(self._inspect, guild_id=self.guild_id)
        except (ThemeError, OSError):
            return None

    async def set_flash(self, key: str, color: str = "success", **variables) -> None:
        """Set the one-shot flash: the copy, the tone and the verb's icon.

        The notice layer owns the icon (the copy never brings one of its own),
        so the panel names the verb of the action here.
        """
        self.flash = await self.bot.translator.t(self.guild_id, key, **variables)
        self.flash_color = color
        self.flash_icon = _FLASH_ICONS.get(key)

    async def _rerender_with_snapshot(self, interaction: discord.Interaction) -> None:
        """Apply + snapshot + redraw: selection changes never need a restart."""
        from rosemary.core.panels import on_theme_changed
        from rosemary.core.themes import preload_themes

        await preload_themes(self.bot)
        await self.rerender(interaction)
        # Stale panels and action-button dispatch views must follow the new
        # theme immediately: no restart.
        await on_theme_changed(self.bot, self.guild_id)

    #: handlers ------------------------------------------------------------

    async def _select(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        if not await self._ack(interaction):
            return
        name = str(values[0])
        try:
            await self.bot._theme_store.set_active(self.guild_id, name)
        except Exception:
            await self.set_flash("themes.not_found", "danger")
        else:
            await self.set_flash("themes.selected", "success", theme=name)
        await self._rerender_with_snapshot(interaction)

    async def _export(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        if not await self._ack(interaction):
            return
        name = str(values[0])
        payload = self.bot._theme_store.export_theme(self.guild_id, name)
        if payload is None:
            await self.set_flash("themes.not_found", "danger")
            return await self.rerender(interaction)
        filename, data = payload
        try:
            await interaction.followup.send(file=_file(data, filename), ephemeral=True)
        except discord.HTTPException:
            await self.set_flash("themes.export_failed", "danger")
        await self._rerender_with_snapshot(interaction)

    async def _remove(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        if not await self._ack(interaction):
            return
        name = str(values[0])
        was_active = self._active == name
        removed = await self.bot._theme_store.remove_guild_theme(self.guild_id, name)
        if not removed:
            await self.set_flash("themes.not_found", "danger")
        else:
            key = "themes.removed_active" if was_active else "themes.removed"
            await self.set_flash(key, "success", theme=name)
        await self._rerender_with_snapshot(interaction)

    async def _reset(self, interaction: discord.Interaction) -> None:
        if not await self._ack(interaction):
            return
        await self.bot._theme_store.clear_active(self.guild_id)
        await self.set_flash("themes.reset_done", "success")
        await self._rerender_with_snapshot(interaction)

    async def _reload(self, interaction: discord.Interaction) -> None:
        """Drop cached theme files, re-read from disk and repaint panels."""
        if not await self._ack(interaction):
            return
        self.bot._theme_store.invalidate_guild(self.guild_id)
        await self.set_flash("themes.reloaded", "success")
        await self._rerender_with_snapshot(interaction)

    async def _close(self, interaction: discord.Interaction) -> None:
        from rosemary.ui.runtime import close

        if not await self._ack(interaction):
            self.stop()
            return
        await close(self, interaction)

    #: inspection -----------------------------------------------------------

    async def _inspect_select(self, interaction: discord.Interaction) -> None:
        """Pick the theme the panel inspects."""
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        if not await self._ack(interaction):
            return
        self._inspect = str(values[0])
        await self.rerender(interaction)

    async def _preview_card_select(self, interaction: discord.Interaction) -> None:
        """Pick the card the preview renders."""
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        if not await self._ack(interaction):
            return
        self._preview_key = str(values[0])
        await self.rerender(interaction)

    async def _preview_form_select(self, interaction: discord.Interaction) -> None:
        """Pick the shape the preview renders."""
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        if not await self._ack(interaction):
            return
        form = str(values[0])
        if form in _PREVIEW_FORMS:
            self._preview_form = form
        await self.rerender(interaction)

    async def _validate(self, interaction: discord.Interaction) -> None:
        """Report what is wrong with the inspected theme."""
        if not await self._ack(interaction):
            return
        from rosemary.core.theme_tools import validate_theme

        theme = self._inspected_theme()
        if theme is None:
            await self.set_flash("themes.not_found", "danger")
            return await self.rerender(interaction)
        issues = await validate_theme(self.bot, self.guild_id, theme)
        name = self._inspect or ""
        if not issues:
            await self.set_flash("themes.validate_ok", "success", theme=name)
            return await self.rerender(interaction)
        await self.set_flash("themes.validate_failed", "danger", theme=name, count=len(issues))
        t = self.bot.translator.t
        lines = [await t(self.guild_id, "themes.validate_title", theme=name)]
        for issue in issues[:10]:
            lines.append(
                await t(
                    self.guild_id,
                    "themes.validate_issue",
                    code=issue.code,
                    where=issue.where,
                    detail=issue.detail,
                )
            )
        await self._say(interaction, "\n".join(lines))
        await self.rerender(interaction)

    async def _diff(self, interaction: discord.Interaction) -> None:
        """Say what the inspected theme changes against the built-in one."""
        if not await self._ack(interaction):
            return
        from rosemary.core.theme_tools import diff_theme

        theme = self._inspected_theme()
        if theme is None:
            await self.set_flash("themes.not_found", "danger")
            return await self.rerender(interaction)
        diff = diff_theme(theme, self.bot.theme)
        name = self._inspect or ""
        t = self.bot.translator.t
        lines = [await t(self.guild_id, "themes.diff_title", theme=name)]
        if diff.empty:
            lines.append(await t(self.guild_id, "themes.diff_empty"))
        else:
            if diff.extends:
                lines.append(
                    await t(self.guild_id, "themes.diff_extends", theme=diff.extends)
                )
            for key, (before, after) in diff.colors.items():
                lines.append(
                    await t(
                        self.guild_id,
                        "themes.diff_list",
                        name=key,
                        before=before or "-",
                        after=after,
                    )
                )
            lines.append(
                await t(self.guild_id, "themes.diff_colors", count=len(diff.colors))
            )
            lines.append(
                await t(self.guild_id, "themes.diff_emojis", count=len(diff.emojis))
            )
            lines.append(
                await t(self.guild_id, "themes.diff_styles", count=len(diff.styles))
            )
            lines.append(
                await t(self.guild_id, "themes.diff_cards", count=len(diff.cards))
            )
            lines.append(
                await t(self.guild_id, "themes.diff_deltas", count=len(diff.deltas))
            )
            lines.append(
                await t(self.guild_id, "themes.diff_pings", count=len(diff.pings))
            )
        await self._say(interaction, "\n".join(lines))
        await self.rerender(interaction)

    async def _preview(self, interaction: discord.Interaction) -> None:
        """Render one card of the inspected theme in the chosen shape."""
        if not await self._ack(interaction):
            return
        from rosemary.core.theme_tools import preview_card

        key = self._preview_key
        if not key:
            await self.set_flash("themes.not_found", "danger")
            return await self.rerender(interaction)
        payload, _allowed = await preview_card(
            self.bot,
            self.guild_id,
            key,
            self._preview_form,
            theme=self._inspected_theme(),
        )
        if payload is None:
            await self.set_flash(
                "themes.preview_failed",
                "danger",
                form=await self._form_label(self._preview_form),
            )
            return await self.rerender(interaction)
        try:
            await interaction.followup.send(**payload.message_kwargs(), ephemeral=True)
        except discord.HTTPException:
            await self.set_flash(
                "themes.preview_failed",
                "danger",
                form=await self._form_label(self._preview_form),
            )
        await self.rerender(interaction)

    async def _say(self, interaction: discord.Interaction, text: str) -> None:
        """Send a line to the invoker (the panel itself stays in place)."""
        with contextlib.suppress(discord.HTTPException):
            await interaction.followup.send(text, ephemeral=True)

    async def _form_label(self, form: str) -> str:
        """The name of a shape, in the guild language (the catalog)."""
        return await self.bot.translator.t(self.guild_id, f"themes.form_{form}")

    #: rendering -----------------------------------------------------------

    async def build_items(self) -> list[discord.ui.ViewItem]:
        t = self.bot.translator.t
        t_ = self.guild_id

        active_label = (
            await t(t_, "themes.active_label", theme=self._active)
            if self._active
            else await t(t_, "themes.active_none")
        )
        await self.set_screen("themes.title", heading="themes.title")
        self.lines = [TextDisplay(active_label)]
        self.color = self.flash_color if self.flash else "brand"

        sections: list[discord.ui.ViewItem] = [divider("large")]

        # Manager rows: one select per action, shown only when usable.
        if self._theme_names:
            sections.append(
                ActionRow(
                    await self.select(
                        "themes_select",
                        "themes.select_placeholder",
                        [
                            discord.SelectOption(
                                label=name, value=name, default=name == self._active
                            )
                            for name in self._theme_names[:25]
                        ],
                    )
                )
            )
        if self._imports:
            sections.append(
                ActionRow(
                    await self.select(
                        "themes_remove",
                        "themes.remove_placeholder",
                        [
                            discord.SelectOption(label=name, value=name)
                            for name in sorted(self._imports)[:25]
                        ],
                    )
                )
            )
        if self._theme_names:
            sections.append(
                ActionRow(
                    await self.select(
                        "themes_export",
                        "themes.export_placeholder",
                        [
                            discord.SelectOption(label=name, value=name)
                            for name in self._theme_names[:25]
                        ],
                    )
                )
            )
        if not self._theme_names:
            sections.append(TextDisplay(await t(t_, "themes.none_yet")))
        sections.append(TextDisplay(await t(t_, "themes.hint")))

        sections.append(divider("large"))

        # Inspection rows: the theme to look at, the card to preview and the
        # shape to preview it in.
        if self._theme_names:
            sections.append(
                ActionRow(
                    await self.select(
                        "themes_inspect",
                        "themes.inspect_placeholder",
                        [
                            discord.SelectOption(
                                label=name, value=name, default=name == self._inspect
                            )
                            for name in self._theme_names[:25]
                        ],
                    )
                )
            )
        if self._preview_options:
            sections.append(
                ActionRow(
                    await self.select(
                        "themes_preview_card",
                        "themes.preview_card_placeholder",
                        [
                            discord.SelectOption(
                                label=key, value=key, default=key == self._preview_key
                            )
                            for key in self._preview_options
                        ],
                    )
                )
            )
            sections.append(
                ActionRow(
                    await self.select(
                        "themes_preview_form",
                        "themes.preview_form_placeholder",
                        [
                            discord.SelectOption(
                                label=await t(t_, f"themes.form_{form}"),
                                value=form,
                                default=form == self._preview_form,
                            )
                            for form in _PREVIEW_FORMS
                        ],
                    )
                )
            )
        sections.append(
            ActionRow(
                self.make_button(
                    custom_id="themes_validate",
                    label=await t(t_, "themes.validate_button"),
                    style=discord.ButtonStyle.secondary,
                    emoji=self.emoji("check") or self.emoji("shield"),
                ),
                self.make_button(
                    custom_id="themes_diff",
                    label=await t(t_, "themes.diff_button"),
                    style=discord.ButtonStyle.secondary,
                    emoji=self.emoji("scroll"),
                ),
                self.make_button(
                    custom_id="themes_preview",
                    label=await t(t_, "themes.preview_button"),
                    style=discord.ButtonStyle.secondary,
                    emoji=self.emoji("frame") or self.emoji("search"),
                ),
            )
        )

        sections.append(divider("large"))
        sections.append(
            ActionRow(
                self.make_button(
                    custom_id="themes_reset",
                    label=await t(t_, "themes.reset_button"),
                    style=discord.ButtonStyle.secondary,
                    emoji=self.emoji("refresh"),
                ),
                self.make_button(
                    custom_id="themes_reload",
                    label=await t(t_, "themes.reload_button"),
                    style=discord.ButtonStyle.secondary,
                    emoji=self.emoji("clock"),
                ),
                self.make_button(
                    custom_id="themes_close",
                    label=await t(t_, "themes.close"),
                    style=discord.ButtonStyle.secondary,
                    emoji=self.emoji("door"),
                ),
            )
        )
        return await self.compose(sections)

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
        imported = None
        if attachment is not None:
            filename = attachment.filename or "theme.yaml"
            store = self.bot._theme_store
            try:
                payload = await attachment.read()
                name = await store.import_theme(ctx.guild_id, filename, payload)
            except Exception as exc:
                reason = await self._rejection_reason(ctx.guild_id, exc)
                from rosemary.core.feedback import respond as respond_line

                await respond_line(
                    ctx,
                    self.bot,
                    ctx.guild_id,
                    await self.bot.translator.t(
                        ctx.guild_id, "themes.rejected", reason=reason
                    ),
                    style="danger",
                    ephemeral=True,
                )
                return
            imported = name
            from rosemary.core.panels import on_theme_changed

            await on_theme_changed(self.bot, ctx.guild_id)
        view = ThemesMenuView(self.bot, ctx.guild_id, owner_id=ctx.author.id)
        if imported:
            await view.set_flash("themes.imported", "success", theme=imported)
        await view.prepare()
        from rosemary.core.debug import trace_card_path

        # Heading is theme-customizable (card.themes.title): trace the path.
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
