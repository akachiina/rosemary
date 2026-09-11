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

import discord
from discord.ext import commands

from rosemary.ui.menu import MenuView


class ThemesMenuView(MenuView):
    """One-panel theme manager; ids are stable for the persistent fallback."""

    def __init__(self, bot, guild_id: int, *, owner_id: int) -> None:
        super().__init__(author_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.flash: str | None = None
        self.flash_color = "info"
        self.register("themes_select", self._select)
        self.register("themes_export", self._export)
        self.register("themes_remove", self._remove)
        self.register("themes_reset", self._reset)
        self.register("themes_close", self._close)

    async def prepare(self) -> None:
        """Load the panel state, then rebuild children (``MenuView.prepare``)."""
        self._guild = self.bot.get_guild(self.guild_id)
        store = getattr(self.bot, "_theme_store", None)
        self._theme_names = store.list_all(self.guild_id) if store else []
        self._active = await store.get_active(self.guild_id) if store else None
        await super().prepare()

    async def _rerender_with_snapshot(self, interaction: discord.Interaction) -> None:
        """Apply + snapshot + redraw: selection changes never need a restart."""
        from rosemary.core.themes import preload_themes

        await preload_themes(self.bot)
        await self.prepare()
        await self.rerender(interaction)

    # -- handlers ------------------------------------------------------------

    async def _select(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        await interaction.response.defer()
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
        await interaction.response.defer()
        name = str(values[0])
        payload = self.bot._theme_store.export_theme(self.guild_id, name)
        if payload is None:
            self.flash = await self.bot.translator.t(self.guild_id, "themes.not_found")
            self.flash_color = "danger"
            return await self.rerender(interaction)
        filename, data = payload
        try:
            await interaction.followup.send(
                file=discord.File(io_bytes(data), filename=filename),
                ephemeral=True,
            )
        except discord.HTTPException:
            self.flash = await self.bot.translator.t(self.guild_id, "themes.export_failed")
            self.flash_color = "danger"
        await self._rerender_with_snapshot(interaction)

    async def _remove(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        await interaction.response.defer()
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
        await interaction.response.defer()
        await self.bot._theme_store.clear_active(self.guild_id)
        self.flash = await self.bot.translator.t(self.guild_id, "themes.reset")
        self.flash_color = "success"
        await self._rerender_with_snapshot(interaction)

    async def _close(self, interaction: discord.Interaction) -> None:
        self.disable_all_items()
        await interaction.edit(view=self)
        self.stop()

    # -- import ----------------------------------------------------------------

    # Imports run inline in the cog (the command receives the attachment);
    # the view only renders the result.
    # -- rendering -------------------------------------------------------------

    async def build_items(self) -> list[discord.ui.ViewItem]:
        t = self.bot.translator.t
        emojis = self.bot.theme.emojis
        active_label = (
            await t(self.guild_id, "themes.active_label", theme=self._active or "—")
            if self._active
            else await t(self.guild_id, "themes.active_none")
        )
        children: list[discord.ui.ViewItem] = [
            discord.ui.TextDisplay(await t(self.guild_id, "themes.title")),
            discord.ui.TextDisplay(active_label),
        ]
        if self.flash:
            children.append(
                discord.ui.TextDisplay(
                    f"-# {self.flash}"
                    if self.flash_color != "danger"
                    else self.flash
                )
            )
        guild_imports = set(self.bot._theme_store.list_guild(self.guild_id))
        if self._theme_names:
            options = [
                discord.SelectOption(
                    label=name,
                    value=name,
                    default=name == self._active,
                )
                for name in self._theme_names[:24]
            ]
            children.append(
                discord.ui.ActionRow(
                    discord.ui.Select(
                        placeholder=await t(self.guild_id, "themes.select_placeholder"),
                        options=options,
                        custom_id="themes_select",
                    )
                )
            )
            children.append(
                discord.ui.ActionRow(
                    discord.ui.Select(
                        placeholder=await t(self.guild_id, "themes.export_placeholder"),
                        options=[
                            discord.SelectOption(label=name, value=name)
                            for name in self._theme_names[:24]
                        ],
                        custom_id="themes_export",
                    )
                )
            )
        removable = sorted(guild_imports)
        if removable:
            children.append(
                discord.ui.ActionRow(
                    discord.ui.Select(
                        placeholder=await t(self.guild_id, "themes.remove_placeholder"),
                        options=[
                            discord.SelectOption(label=name, value=name)
                            for name in removable[:24]
                        ],
                        custom_id="themes_remove",
                    )
                )
            )
        if not self._theme_names:
            children.append(
                discord.ui.TextDisplay(await t(self.guild_id, "themes.none_yet"))
            )
        children.append(
            discord.ui.TextDisplay(await t(self.guild_id, "themes.built_in"))
        )
        children.append(
            discord.ui.ActionRow(
                _button(
                    "themes_reset",
                    await t(self.guild_id, "themes.reset"),
                    emojis.get("refresh", ""),
                    discord.ButtonStyle.secondary,
                ),
                _button(
                    "themes_close",
                    await t(self.guild_id, "themes.close"),
                    emojis.get("check", ""),
                    discord.ButtonStyle.primary,
                ),
            )
        )
        from rosemary.ui.containers import designer_container

        return [
            designer_container(
                self.bot.theme.color(self.flash_color if self.flash else "brand"),
                *children,
            )
        ]


def _button(
    custom_id: str, label: str, emoji: str, style: discord.ButtonStyle
) -> discord.ui.Button:
    return discord.ui.Button(
        style=style,
        label=label,
        emoji=emoji or None,
        custom_id=custom_id,
    )


def io_bytes(data: bytes):
    import io

    return io.BytesIO(data)


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
        import_message = None
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
            import_message = await self.bot.translator.t(
                ctx.guild_id, "themes.imported", theme=name
            )
        view = ThemesMenuView(self.bot, ctx.guild_id, owner_id=ctx.author.id)
        if import_message:
            view.flash = import_message
            view.flash_color = "success"
        await view.prepare()
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
