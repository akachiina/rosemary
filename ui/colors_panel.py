"""Public color panel: chunk builder, picker views and the /cores helper.

The panel is one message with:

* a frame (default built in the cog; a theme may override ``colors.panel``);
* one V2 container per chunk of ``colors.per_container`` colors, each with
  its own gallery image (``attachment://color_panel_<n>.png``) and that
  chunk's numbered buttons INSIDE the card - the Color-Chan look of several
  cards, numbered continuously across chunks;
* in select mode a single string select replaces the button rows (chunks
  carry only their gallery image).

Pickers are persistent: the boot path registers a view whose children carry
the same ``colors_pick:<id>`` custom_ids the posted panel shows (py-cord only
indexes real children), so clicks survive restarts. Clicking a color toggles:
the attached role replaces any color role the member already has, and
clicking the color they already wear removes it.
"""

from __future__ import annotations

import io
import logging

import discord

from rosemary.core.color_image import PanelColor, render_panel
from rosemary.core.colors import ColorEntry
from rosemary.core.themes import theme_for
from rosemary.ui.colors_menu import resolve_color
from rosemary.ui.menu import MenuView

log = logging.getLogger(__name__)

_PICK_ID = "colors_pick"
_SELECT_ID = f"{_PICK_ID}_select"
_IMAGE_NAME = "color_panel_{n}.png"


def entry_number(position: int) -> str:
    """Panel label number (1-based) for the manager list."""
    return f"{position}."


def chunks_of(
    entries: list[ColorEntry], per: int
) -> list[list[tuple[int, ColorEntry]]]:
    """Split ``(number, entry)`` pairs into per-container chunks."""
    numbered = list(enumerate(entries, start=1))
    if per <= 0:
        return [numbered] if numbered else []
    return [numbered[i : i + per] for i in range(0, len(numbered), per)]


def chunk_containers(
    bot,
    guild_id: int,
    entries: list[ColorEntry],
    per_container: int,
    picker_mode: str = "buttons",
) -> tuple[list[discord.File], list[dict]]:
    """Files and chunk documents for the panel message.

    Each chunk is ONE container: its gallery image followed by that chunk's
    numbered buttons INSIDE the card (the user's layout choice), numbered
    continuously across chunks::

        [card: image 1-10 + buttons 1-10] [card: image 11-20 + buttons]

    ``per_container`` may grow (:func:`fit_per_container`): more colors than
    the requested chunking can hold inside Discord's 40-component budget
    force fewer, larger cards. In ``select`` mode no buttons are emitted:
    the caller adds the single select row. A chunk whose render fails (no
    WeasyPrint/Pango, template error) contributes no gallery - the panel
    still posts, imageless. Colors without an attached role render with
    their stored hex but cannot be picked.
    """
    # Both modes render the cards; the budget differs because select-mode
    # cards carry no buttons (the single select row rides on the message).
    per_container = fit_per_container(
        len(entries), per_container, picker_mode != "select"
    )
    theme = theme_for(bot, guild_id)
    templates = getattr(theme, "color_panel", {}) or {}
    template = templates.get("html")
    item = templates.get("item")
    guild = bot.get_guild(guild_id)
    live = (
        {role.id: getattr(role.color, "value", 0) or 0 for role in guild.roles}
        if guild is not None
        else {}
    )
    files: list[discord.File] = []
    documents: list[dict] = []
    for index, chunk in enumerate(chunks_of(entries, per_container), start=1):
        rows = [
            PanelColor(number, entry.name, resolve_color(live, entry))
            for number, entry in chunk
        ]
        png = render_panel(rows, template, item)
        filename = _IMAGE_NAME.format(n=index)
        children: list[dict] = []
        if png is not None:
            files.append(discord.File(io.BytesIO(png), filename=filename))
            children.append({"type": "gallery", "urls": [f"attachment://{filename}"]})
        if picker_mode != "select":
            children.extend(_picker_rows(chunk))
        if children:
            documents.append(
                {"type": "container", "color": "brand", "children": children}
            )
    return files, documents


#: Node cost of the static frame (container + two text displays) and the
#: select row/select pair, for :func:`fit_per_container`.
_FRAME_NODES = 3
_SELECT_EXTRA_NODES = 2


def fit_per_container(count: int, per: int, buttons: bool = True) -> int:
    """Smallest ``per >= requested`` whose panel stays under 40 nodes.

    Every V2 node counts toward Discord's per-message ceiling, nesting
    included: a chunk costs ``container + gallery (+ its button rows and
    buttons when ``buttons``)``. With many colors the requested chunking
    can exceed the budget (25 colors at 10/chunk with buttons = 42 nodes);
    the layout preference then bends - chunks grow so the message still
    sends instead of 400ing. One chunk always fits (25 colors in a single
    card = 35 nodes with buttons, 28 without).
    """
    per = max(per, 1)

    def nodes(candidate: int) -> int:
        chunks = -(-count // candidate) if count else 0
        card = 2 + (
            (-(-candidate // 5) + candidate) if buttons else 0
        )
        return _FRAME_NODES + chunks * card + (
            _SELECT_EXTRA_NODES if not buttons else 0
        )

    candidate = per
    while candidate < count and nodes(candidate) > 40:
        candidate += 1
    return candidate


def _picker_rows(chunk: list[tuple[int, ColorEntry]]) -> list[dict]:
    """Button-row documents for one chunk, nested in its card (5 per row)."""
    rows: list[dict] = []
    for start in range(0, len(chunk), 5):
        rows.append(
            {
                "type": "row",
                "buttons": [
                    {
                        "type": "button",
                        "label": str(position),
                        "style": "secondary",
                        # custom_id the ColorPickerView dispatcher owns.
                        "id": f"{_PICK_ID}:{entry.id}",
                    }
                    for position, entry in chunk[start : start + 5]
                ],
            }
        )
    return rows


class ColorPickerView(MenuView):
    """Persistent color picker: numbered buttons or one select.

    The view instance is a dispatcher: its rows are added to whichever
    DesignerView carries the panel, and the boot path registers a fresh
    instance with the same custom_ids so clicks keep working after restarts.
    """

    def __init__(
        self,
        bot,
        guild_id: int,
        entries: list[ColorEntry],
        timeout: float | None = None,
    ) -> None:
        super().__init__(author_id=None, timeout=timeout)
        self.bot = bot
        self.guild_id = guild_id
        self.entries = entries
        self.register(_PICK_ID, self._pick_button)
        self.register(_SELECT_ID, self._pick_select)

    async def _t(self, key: str, **kwargs) -> str:
        return await self.bot.translator.t(self.guild_id, key, **kwargs)

    async def prepare(self) -> None:
        pass

    def rows_for(self, entries: list[ColorEntry]) -> list[discord.ui.ActionRow]:
        """Numbered buttons for ``entries`` in panel order (5 per row)."""
        rows: list[discord.ui.ActionRow] = []
        row: list[discord.ui.Button] = []
        for position, entry in enumerate(entries, start=1):
            row.append(
                self.make_button(
                    custom_id=f"{_PICK_ID}:{entry.id}",
                    label=str(position),
                    style=discord.ButtonStyle.secondary,
                )
            )
            if len(row) == 5:
                rows.append(discord.ui.ActionRow(*row))
                row = []
        if row:
            rows.append(discord.ui.ActionRow(*row))
        return rows

    def classic_rows_all(self, entries: list[ColorEntry]) -> list[discord.ui.ActionRow]:
        """Every button row in one list (embed-form frames)."""
        return self.rows_for(entries)

    async def select_row(self) -> discord.ui.ActionRow:
        """The select-mode picker (options = every color, 25 max)."""
        options = [
            discord.SelectOption(label=f"{position}. {entry.name}"[:100], value=entry.id)
            for position, entry in enumerate(self.entries, start=1)
        ]
        return discord.ui.ActionRow(
            self.make_select(
                custom_id=_SELECT_ID,
                placeholder=await self._t("colors.panel.placeholder"),
                options=options,
            )
        )

    # == pick handling =======================================================

    async def _apply(self, interaction: discord.Interaction, entry: ColorEntry) -> None:
        cog = self.bot.get_cog("ColorsCog")
        guild = self.bot.get_guild(self.guild_id)
        member = guild.get_member(interaction.user.id) if guild else None
        if cog is None or guild is None or member is None:
            await interaction.followup.send(
                await self._t("general.unknown_error"), ephemeral=True
            )
            return
        color_entries = await cog.store.list_colors(self.guild_id)
        linked = {entry.role_id for entry in color_entries if entry.role_id}
        color_roles = [role for role in member.roles if role.id in linked]
        wearing = entry.role_id is not None and any(
            role.id == entry.role_id for role in color_roles
        )
        if not wearing:
            role = guild.get_role(entry.role_id) if entry.role_id else None
            if role is None:
                return await interaction.followup.send(
                    await self._t("colors.error_no_role", name=entry.name),
                    ephemeral=True,
                )
            if role >= guild.me.top_role:
                return await interaction.followup.send(
                    await self._t("colors.error_hierarchy"), ephemeral=True
                )
        try:
            if wearing:
                await member.remove_roles(
                    guild.get_role(entry.role_id), reason="Color panel toggle off"
                )
            else:
                replace = [
                    role for role in color_roles if role.id != entry.role_id
                ]
                if replace:
                    await member.remove_roles(*replace, reason="Color panel switch")
                await member.add_roles(
                    guild.get_role(entry.role_id), reason="Color panel pick"
                )
        except discord.Forbidden:
            return await interaction.followup.send(
                await self._t("colors.error_no_perms"), ephemeral=True
            )
        except discord.HTTPException as exc:
            log.warning("color pick failed in %s: %s", self.guild_id, exc)
            return await interaction.followup.send(
                await self._t("general.unknown_error"), ephemeral=True
            )
        key = "colors.panel.removed" if wearing else "colors.panel.applied"
        await interaction.followup.send(
            await self._t(key, name=entry.name), ephemeral=True
        )

    async def _entry_for(self, color_id: str) -> ColorEntry | None:
        cog = self.bot.get_cog("ColorsCog")
        if cog is None:
            return None
        return await cog.store.get_color(self.guild_id, color_id)

    async def _pick_button(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        color_id = (interaction.custom_id or "").split(":", 1)[-1]
        entry = await self._entry_for(color_id)
        if entry is None:
            return await interaction.followup.send(
                await self._t("colors.error_not_found"), ephemeral=True
            )
        await self._apply(interaction, entry)

    async def _pick_select(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        values = (interaction.data or {}).get("values") or []
        if not values:
            return
        entry = await self._entry_for(str(values[0]))
        if entry is None:
            return await interaction.followup.send(
                await self._t("colors.error_not_found"), ephemeral=True
            )
        await self._apply(interaction, entry)
