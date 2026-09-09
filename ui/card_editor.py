"""Interactive V2 card composer (the /customize editor surface).

Edits one card document (:mod:`rosemary.core.cards`) as a tree of blocks.
Persistence is injected via ``load/save/reset`` callables, so this module knows
nothing about where documents live — features wire their own store, keeping
the editor reusable by every cog that declares an editable card.

UX model: a numbered select picks the block to work on, one context row acts
on the selection (edit/move/delete/open), a breadcrumb shows where you are,
and a live draft preview of the whole document sits on top. "Compare" renders
the feature's registered default builder above the custom document.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from rosemary.core.card_actions import bind_action_callbacks
from rosemary.core.cards import (
    CardsError,
    build_items,
    get_default_builder,
)
from rosemary.ui.containers import (
    ActionRow,
    TextDisplay,
    designer_container,
    divider,
)
from rosemary.ui.menu import MenuView
from rosemary.ui.modals import make_text_modal

LoadDoc = Callable[[int], Awaitable[dict[str, Any] | None]]
SaveDoc = Callable[[int, dict[str, Any]], Awaitable[None]]
ResetDoc = Callable[[int], Awaitable[None]]
LoadMentions = Callable[[int], Awaitable[str | None]]
SaveMentions = Callable[[int, str], Awaitable[None]]

_COMPOSITE_TYPES = ("container", "section")
_BLOCK_TYPES = ("text", "container", "section", "divider", "gallery", "row")
_THEME_COLORS = ("brand", "success", "warning", "danger", "info", "gold", "orange")

#: How many blocks the block-picker select shows at once (Discord caps at 25).
_BLOCK_PAGE_SIZE = 25




def _skeleton(block_type: str) -> dict[str, Any]:
    """Return an empty-but-shaped block for the given type (with stable id)."""
    from rosemary.core.cards import new_block_id

    if block_type == "text":
        return {"id": new_block_id(), "type": "text", "body": ""}
    if block_type == "container":
        return {
            "id": new_block_id(),
            "type": "container",
            "color": None,
            "children": [_skeleton("text")],
        }
    if block_type == "section":
        return {
            "id": new_block_id(),
            "type": "section",
            "accessory": {"type": "thumbnail", "url": "{user_avatar}"},
            "children": [_skeleton("text")],
        }
    if block_type == "divider":
        return {"id": new_block_id(), "type": "divider", "spacing": "small"}
    if block_type == "gallery":
        return {"id": new_block_id(), "type": "gallery", "urls": []}
    if block_type == "row":
        return {
            "id": new_block_id(),
            "type": "row",
            "buttons": [{"id": new_block_id(), "label": "", "url": ""}],
        }
    raise ValueError(f"unknown block type {block_type!r}")


async def _block_option(bot, guild_id: int, index: int, block: dict[str, Any], selected: bool):
    """Async variant that resolves translated labels/counters."""
    kind = block.get("type", "?")
    base = str(await bot.translator.t(guild_id, f"cards.editor.types.{kind}"))
    description: str | None = None
    if kind == "text":
        body = str(block.get("body") or "").strip()
        description = (
            body.splitlines()[0][:100]
            if body
            else str(await bot.translator.t(guild_id, "cards.editor.empty_block"))
        )
    elif kind in ("container", "section"):
        count = len(block.get("children") or [])
        description = str(
            await bot.translator.t(guild_id, "cards.editor.child_count", count=count)
        )
    elif kind == "gallery":
        count = len(block.get("urls") or [])
        description = str(
            await bot.translator.t(guild_id, "cards.editor.image_count", count=count)
        )
    elif kind == "divider":
        spacing = str(block.get("spacing", "small"))
        description = str(
            await bot.translator.t(guild_id, f"cards.editor.spacing.{spacing}")
        )
    elif kind == "row":
        count = len(block.get("buttons") or [])
        description = str(
            await bot.translator.t(guild_id, "cards.editor.button_count", count=count)
        )
    return discord.SelectOption(
        label=f"{index + 1}. {base}"[:100],
        value=str(index),
        description=description,
        default=selected,
    )


class CardEditorView(MenuView):
    """Browse, compose and test one card document."""

    def __init__(
        self,
        bot,
        guild_id: int,
        key: str,
        *,
        load_doc: LoadDoc,
        save_doc: SaveDoc,
        reset_doc: ResetDoc,
        owner_id: int | None = None,
        placeholders_hint: str = "",
        exit_factory: Callable[[], MenuView] | None = None,
        load_mentions: LoadMentions | None = None,
        save_mentions: SaveMentions | None = None,
    ) -> None:
        super().__init__(author_id=owner_id)
        self.bot = bot
        self.guild_id = guild_id
        self.key = key
        self._load_doc = load_doc
        self._save_doc = save_doc
        self._reset_doc = reset_doc
        self._load_mentions = load_mentions
        self._save_mentions = save_mentions
        self.mention_policy: str | None = None
        self.placeholders_hint = placeholders_hint
        self._exit_factory = exit_factory
        self.blocks: list[dict[str, Any]] = []
        self.loaded = False
        self.path: list[int] = []
        self.selected: int | None = None
        self.adding = False
        self.comparing = False
        self.saved_exists = False
        self.using_default_base = False
        self.flash: str | None = None
        self.flash_color: str = "brand"
        self._undo: list[list[dict[str, Any]]] = []
        self._redo: list[list[dict[str, Any]]] = []
        self._saved_blocks: list[dict[str, Any]] = []
        self._armed: str | None = None
        self._block_page: int = 0
        self._editing_row: str | None = None
        self._editing_accessory: str | None = None
        self._editing_divider: str | None = None
        self._adding_action: bool = False
        self._adding_action_type: str | None = None
        self._show_more: bool = False
        self._show_templates: bool = False
        self._show_history: bool = False
        self._register_handlers()

    # -- state ---------------------------------------------------------------

    def _register_handlers(self) -> None:
        self.register("card_close", self._close)
        self.register("card_add", self._open_add)
        self.register("card_add_cancel", self._cancel_add)
        self.register("card_add_type", self._pick_add_type)
        self.register("card_select", self._select_block)
        self.register("card_act", self._act_on_selected)
        self.register("card_uplevel", self._up_level)
        self.register("card_nav", self._nav_to)
        self.register("card_page_prev", self._block_page_prev)
        self.register("card_page_next", self._block_page_next)
        self.register("card_row_add", self._row_add)
        self.register("card_row_add_action", self._row_add_action)
        self.register("card_row_action", self._pick_row_action)
        self.register("card_row_action_type", self._pick_row_action_type)
        self.register("card_row_edit", self._row_edit)
        self.register("card_row_delete", self._row_delete)
        self.register("card_row_back", self._row_back)
        self.register("card_divider", self._set_divider_style)
        self.register("card_divider_back", self._divider_back)
        self.register("card_accessory", self._open_accessory)
        self.register("card_accessory_type", self._set_accessory_type)
        self.register("card_accessory_edit", self._edit_accessory_values)
        self.register("card_accessory_back", self._accessory_back)
        self.register("card_save", self._save)
        self.register("card_discard", self._discard)
        self.register("card_undo", self._undo_action)
        self.register("card_more", self._open_more)
        self.register("card_more_back", self._more_back)
        self.register("card_templates", self._open_templates)
        self.register("card_template_pick", self._pick_template)
        self.register("card_history", self._open_history)
        self.register("card_history_pick", self._pick_history)
        self.register("card_export", self._export)
        self.register("card_import", self._import)
        self.register("card_reset", self._reset)
        self.register("card_test", self._test)
        self.register("card_compare", self._open_compare)
        self.register("card_compare_close", self._close_compare)
        self.register("card_color", self._set_container_color)
        self.register("card_exit", self._exit_to_origin)
        self.register("card_mentions", self._set_mentions)

    def _touch(self) -> None:
        """Snapshot for undo; call after every draft mutation."""
        self._undo.append(copy.deepcopy(self.blocks))
        del self._undo[:-50]
        self._redo.clear()
        self._armed = None

    def _snapshot_saved(self) -> None:
        self._saved_blocks = copy.deepcopy(self.blocks)

    def is_dirty(self) -> bool:
        """Whether the draft differs from the last save."""
        return self.blocks != self._saved_blocks

    async def _t(self, translation_key: str, **kwargs: Any) -> str:
        return await self.bot.translator.t(self.guild_id, translation_key, **kwargs)

    def document(self) -> dict[str, Any]:
        return {"v": 1, "blocks": self.blocks}

    def _current_blocks(self) -> list[dict[str, Any]]:
        node: Any = self.blocks
        for index in self.path:
            node = node[index]["children"]
        return node

    def _node_type_at_path(self) -> str | None:
        """Type of the block the current navigation path points at."""
        if not self.path:
            return None
        node: Any = self.blocks
        for index in self.path[:-1]:
            node = node[index]["children"]
        return node[self.path[-1]].get("type")

    def _inside_section(self) -> bool:
        return self._node_type_at_path() == "section"

    def _inside_container(self) -> bool:
        return self._node_type_at_path() == "container"

    async def _validate_draft(self):
        """Structural issues in the working copy (draft-tolerant)."""
        from rosemary.core.cards import validate_document

        return validate_document(self.document(), theme=self.bot.theme, draft=True)

    async def _persist(self) -> bool:
        """Legacy autosave entrypoint; kept for tests, delegates to _save_draft.

        New flows mutate via :meth:`_touch` and persist explicitly with
        :meth:`_save`.
        """
        return await self._save_draft()

    async def _save_draft(self) -> bool:
        """Validate (draft-tolerant) and save; ``False`` keeps the draft.

        Empty-text skeletons are tolerated so a just-added block can be
        edited later; any other structural problem is flashed with the
        translated issues while the draft is kept (never silently reloaded).
        """
        from rosemary.core.cards import CardsError, validate_document

        issues = validate_document(self.document(), theme=self.bot.theme, draft=True)
        if issues:
            self.flash = await self._t(
                "cards.editor.invalid",
                error=await self._translate_issues(CardsError(list(issues))),
            )
            self.flash_color = "danger"
            return False
        await self._save_doc(self.guild_id, self.document())
        self.saved_exists = True
        self.using_default_base = False
        self._snapshot_saved()
        from rosemary.core.card_actions import sync_guild
        from rosemary.core.card_history import history_store

        await sync_guild(self.bot, self.guild_id)
        await history_store(self.bot).append(
            self.guild_id, self.key, self.document(), self.author_id
        )
        self.flash = await self._t("cards.editor.saved")
        self.flash_color = "success"
        return True

    async def _save(self, interaction: discord.Interaction) -> None:
        """Explicit save: strict validation, nothing lost on failure."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        from rosemary.core.cards import CardsError, validate_document

        issues = validate_document(self.document(), theme=self.bot.theme, draft=False)
        if issues:
            self.flash = await self._t(
                "cards.editor.invalid",
                error=await self._translate_issues(CardsError(list(issues))),
            )
            self.flash_color = "danger"
            return await self.rerender(interaction)
        await self._save_doc(self.guild_id, self.document())
        self.saved_exists = True
        self.using_default_base = False
        self._snapshot_saved()
        from rosemary.core.card_actions import sync_guild
        from rosemary.core.card_history import history_store

        await sync_guild(self.bot, self.guild_id)
        await history_store(self.bot).append(
            self.guild_id, self.key, self.document(), self.author_id
        )
        self.flash = await self._t("cards.editor.saved")
        self.flash_color = "success"
        await self.rerender(interaction)

    async def _discard(self, interaction: discord.Interaction) -> None:
        """Two-step armed discard of unsaved changes."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not self.is_dirty():
            return await self.rerender(interaction)
        if self._armed != "discard":
            self._armed = "discard"
            self.flash = await self._t("cards.editor.arm_discard")
            self.flash_color = "warning"
            return await self.rerender(interaction)
        self._armed = None
        self.loaded = False
        self.path.clear()
        self.selected = None
        self.adding = False
        self.comparing = False
        await self._load_or_seed()
        self.flash = await self._t("cards.editor.discarded")
        self.flash_color = "warning"
        await self.rerender(interaction)

    async def _undo_action(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if self._undo:
            self._redo.append(copy.deepcopy(self.blocks))
            self.blocks = self._undo.pop()
            self._sanitize_path()
            self._clamp_selection()
        await self.rerender(interaction)

    async def _reload(self) -> None:
        """Restore the working copy from the last saved document (or seed)."""
        self.loaded = False
        self.path.clear()
        self.selected = None
        self.adding = False
        await self._load_or_seed()

    def _clamp_selection(self) -> None:
        total = len(self._current_blocks())
        if self.selected is not None and not 0 <= self.selected < total:
            self.selected = None

    # -- lifecycle -----------------------------------------------------------

    async def prepare(self) -> None:
        if not self.loaded:
            await self._load_or_seed()
        self._sanitize_path()
        self._clamp_selection()
        if self._load_mentions is not None and self.mention_policy is None:
            try:
                self.mention_policy = await self._load_mentions(self.guild_id)
            except Exception:
                self.mention_policy = None
        await self._apply_menu_timeout()
        self.clear_items()
        for item in await self.build_editor():
            self.add_item(item)

    async def _load_or_seed(self) -> None:
        """Load the saved override, or seed the working copy from the default.

        "Sua versão" começa sendo exatamente a mensagem que existe hoje.
        """
        from rosemary.core.cards import ensure_ids

        doc = await self._load_doc(self.guild_id)
        blocks = doc.get("blocks") if isinstance(doc, dict) else None
        if isinstance(blocks, list):
            self.blocks = copy.deepcopy(blocks)
            self.saved_exists = True
            self.using_default_base = False
            self.loaded = True
        else:
            self.saved_exists = False
            seeded = await self._default_blocks()
            if seeded is not None:
                self.blocks = copy.deepcopy(seeded)
                self.using_default_base = True
            else:
                self.blocks = [_skeleton("text")]
                self.using_default_base = False
            self.loaded = True
        ensure_ids({"blocks": self.blocks})
        self._snapshot_saved()
        self._undo.clear()
        self._redo.clear()

    async def _default_blocks(self) -> list[dict[str, Any]] | None:
        builder = get_default_builder(self.key)
        if builder is None:
            return None
        try:
            default_doc = await builder(self.bot, self.guild_id)
        except Exception as exc:
            log_default_failure(self.key, exc)
            return None
        if isinstance(default_doc, dict) and isinstance(default_doc.get("blocks"), list):
            return default_doc["blocks"]
        return None

    # -- build screens -------------------------------------------------------

    async def build_editor(self) -> list[discord.ui.ViewItem]:
        theme = self.bot.theme
        if self.comparing:
            return await self._build_compare_screen()

        parts: list[discord.ui.ViewItem] = [await self._build_header()]
        if self._show_more or self._show_templates or self._show_history:
            if self._show_templates:
                parts.extend(await self._build_templates_screen())
            elif self._show_history:
                parts.extend(await self._build_history_screen())
            else:
                parts.extend(await self._build_more_screen())
            return parts
        parts.extend(await self._breadcrumb_row())

        try:
            preview_items = build_items(
                theme, self.document(), self._preview_variables(), draft=True,
                card_key=self.key,
            )
            bind_action_callbacks(preview_items)
            parts.extend(preview_items)
        except CardsError as exc:
            parts.append(
                designer_container(
                    theme.color("danger"),
                    TextDisplay(
                        await self._t(
                            "cards.editor.invalid",
                            error=await self._translate_issues(exc),
                        )
                    ),
                )
            )

        lint_lines = await self._lint_lines()
        if lint_lines:
            parts.append(
                designer_container(
                    theme.color("warning"),
                    *[TextDisplay(line) for line in lint_lines],
                )
            )

        if self.using_default_base and not self.saved_exists:
            parts.append(
                TextDisplay(
                    theme.md(
                        "footer",
                        text=await self._t("cards.editor.using_default"),
                    )
                )
            )

        if self._adding_action:
            parts.extend(await self._build_action_pick_screen())
        elif self._adding_action_type is not None:
            parts.extend(await self._build_action_type_screen())
        elif self._editing_row is not None:
            parts.extend(await self._build_row_screen())
        elif self._editing_accessory is not None:
            parts.extend(await self._build_accessory_screen())
        elif self._editing_divider is not None:
            parts.extend(await self._build_divider_screen())
        elif self.adding:
            parts.extend(await self._build_add_screen())
        else:
            parts.extend(await self._build_selection_controls())
        mentions_row = await self._build_mentions_row()
        if mentions_row is not None:
            parts.append(mentions_row)
        parts.extend(await self._build_nav_rows())
        return parts

    async def _breadcrumb_row(self) -> list[discord.ui.ViewItem]:
        """Home + move-out (the header text already shows the full path)."""
        if not self.path:
            return []
        theme = self.bot.theme
        buttons = [
            self.make_button(
                custom_id="card_nav:-1",
                label=self.key.split(".")[-1],
                emoji=theme.emojis.get("back", ""),
            )
        ]
        if self.selected is not None:
            buttons.append(
                self.make_button(
                    custom_id="card_act:move_out",
                    label=await self._t("cards.editor.buttons.move_out"),
                    emoji=theme.emojis.get("up", ""),
                )
            )
        return [ActionRow(*buttons)]

    async def _nav_to(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            depth = int(interaction.custom_id.split(":", 1)[1])
        except (ValueError, IndexError):
            return await self.rerender(interaction)
        self._sanitize_path()
        self.path = [] if depth < 0 else self.path[: depth + 1]
        self.selected = None
        self._clear_subscreens()
        await self.rerender(interaction)

    async def _build_header(self) -> discord.ui.ViewItem:
        theme = self.bot.theme
        self._sanitize_path()
        breadcrumb_parts = [self.key]
        node: Any = self.blocks
        for index in self.path:
            block = node[index] if isinstance(node, list) and 0 <= index < len(node) else {}
            kind = block.get("type", "?") if isinstance(block, dict) else "?"
            label = str(
                await self.bot.translator.t(
                    self.guild_id, f"cards.editor.types.{kind}"
                )
            )
            total = len(node) if isinstance(node, list) else 1
            breadcrumb_parts.append(f"{label} {index + 1}/{total}")
            children = block.get("children") if isinstance(block, dict) else None
            node = children if isinstance(children, list) else []

        lines: list[discord.ui.ViewItem] = [
            TextDisplay(theme.md("title", title=await self._t("cards.editor.title"))),
            TextDisplay(
                await self._t(
                    "cards.editor.hint",
                    card_key=self.key,
                    placeholders=self.placeholders_hint
                    or await self._t("cards.editor.placeholders_default"),
                )
            ),
            TextDisplay(
                theme.md("footer", text=await self._t(
                    "cards.editor.breadcrumb", path=" ▸ ".join(breadcrumb_parts)
                ))
            ),
        ]
        if self.is_dirty():
            lines.append(TextDisplay(await self._t("cards.editor.unsaved")))
        color = "brand"
        if self.flash:
            lines.append(TextDisplay(self.flash))
            color = self.flash_color
            self.flash = None
            self.flash_color = "brand"
        return designer_container(theme.color(color), *lines)

    async def _build_selection_controls(self) -> list[discord.ui.ViewItem]:
        current = self._current_blocks()
        rows: list[discord.ui.ViewItem] = []

        if not current:
            rows.append(TextDisplay(await self._t("cards.editor.empty_level")))
        else:
            # Discord selects cap at 25 options: explicit pages.
            total = len(current)
            pages = max((total + _BLOCK_PAGE_SIZE - 1) // _BLOCK_PAGE_SIZE, 1)
            self._block_page = min(max(self._block_page, 0), pages - 1)
            start = self._block_page * _BLOCK_PAGE_SIZE
            window = current[start : start + _BLOCK_PAGE_SIZE]
            options = [
                await _block_option(
                    self.bot, self.guild_id, start + index, block,
                    start + index == self.selected,
                )
                for index, block in enumerate(window)
            ]
            if pages > 1:
                rows.append(
                    TextDisplay(
                        await self._t(
                            "cards.editor.blocks_page",
                            page=self._block_page + 1,
                            pages=pages,
                        )
                    )
                )
            select = self.make_select(
                custom_id="card_select",
                placeholder=await self._t("cards.editor.select_placeholder"),
                options=options,
            )
            rows.append(ActionRow(select))
            if pages > 1:
                rows.append(
                    ActionRow(
                        self.make_button(
                            custom_id="card_page_prev",
                            label=await self._t("cards.customize.prev"),
                            disabled=self._block_page <= 0,
                        ),
                        self.make_button(
                            custom_id="card_page_next",
                            label=await self._t("cards.customize.next"),
                            disabled=self._block_page >= pages - 1,
                        ),
                    )
                )
            rows.extend(await self._context_row(current))

        if self._inside_container():
            rows.append(await self._color_select_row())
        return rows

    async def _context_row(self, current: list[dict[str, Any]]) -> list[discord.ui.ViewItem]:
        selected = (
            current[self.selected] if self.selected is not None and current else None
        )
        nothing = selected is None
        kind = selected.get("type") if isinstance(selected, dict) else None

        async def button(action: str, label_key: str, emoji: str, disabled: bool):
            return self.make_button(
                custom_id=f"card_act:{action}",
                label=await self._t(label_key),
                emoji=self.bot.theme.emojis.get(emoji, ""),
                disabled=disabled,
            )

        buttons: list[discord.ui.ViewItem] = []
        if kind in _COMPOSITE_TYPES:
            buttons.append(
                await button(
                    "enter",
                    "cards.editor.buttons.enter",
                    "gear",
                    nothing,
                )
            )
        else:
            buttons.append(await button("edit", "cards.editor.buttons.edit", "pencil", nothing))
        buttons.append(
            await button(
                "up", "cards.editor.buttons.move_up", "up", nothing or self.selected == 0
            )
        )
        buttons.append(
            await button(
                "down",
                "cards.editor.buttons.move_down",
                "down",
                nothing or self.selected == len(current) - 1,
            )
        )
        buttons.append(
            await button(
                "duplicate",
                "cards.editor.buttons.duplicate",
                "plus",
                nothing,
            )
        )
        delete = self.make_button(
            custom_id="card_act:delete",
            label=await self._t("cards.editor.buttons.delete"),
            style=discord.ButtonStyle.danger,
            emoji=self.bot.theme.emojis.get("trash", ""),
            disabled=nothing,
        )
        buttons.append(delete)
        rows: list[discord.ui.ViewItem] = [ActionRow(*buttons)]
        if kind == "section":
            rows.append(
                ActionRow(
                    self.make_button(
                        custom_id="card_accessory",
                        label=await self._t("cards.editor.buttons.accessory"),
                        emoji=self.bot.theme.emojis.get("frame", ""),
                        disabled=nothing,
                    )
                )
            )
        return rows

    def _clear_subscreens(self) -> None:
        """Leave row/accessory/divider/action editing screens."""
        self._editing_row = None
        self._editing_accessory = None
        self._editing_divider = None
        self._adding_action = False
        self._adding_action_type = None

    def _row_block(self) -> dict[str, Any] | None:
        from rosemary.core.cards import find_by_id

        if not self._editing_row:
            return None
        block = find_by_id(self.blocks, self._editing_row)
        return block if isinstance(block, dict) and block.get("type") == "row" else None

    def _sanitize_path(self) -> None:
        """Drop stale path segments (modal submitted after delete/move/reset)."""
        node: Any = self.blocks
        for depth, index in enumerate(self.path):
            if not isinstance(node, list) or not 0 <= index < len(node):
                del self.path[depth:]
                self.selected = None
                return
            block = node[index]
            children = block.get("children") if isinstance(block, dict) else None
            if not isinstance(children, list):
                del self.path[depth:]
                self.selected = None
                return
            node = children

    async def _color_select_row(self) -> discord.ui.ViewItem:
        options = [
            discord.SelectOption(
                label=await self._t(f"cards.editor.colors.{name}"), value=name
            )
            for name in _THEME_COLORS
        ]
        options.append(
            discord.SelectOption(label=await self._t("cards.editor.colors.none"), value="")
        )
        select = self.make_select(
            custom_id="card_color",
            placeholder=await self._t("cards.editor.colors.placeholder"),
            options=options,
        )
        token = None
        node: Any = self.blocks
        try:
            for index in self.path[:-1]:
                node = node[index]["children"]
            current = node[self.path[-1]] if self.path else None
            token = current.get("color") if isinstance(current, dict) else None
        except (IndexError, KeyError, TypeError):
            token = None
        for option in select.options:
            option.default = option.value == (token or "")
        return ActionRow(select)

    def _effective_mention_policy(self) -> str:
        """Saved override, else the card spec default (never unknown)."""
        from rosemary.core.mentions import MODES, spec_default

        if self.mention_policy in MODES:
            return self.mention_policy
        default = spec_default(self.key)
        return default if default in MODES else "none"

    async def _build_mentions_row(self) -> discord.ui.ViewItem | None:
        """Per-card ping policy select (only when a store is wired)."""
        if self._load_mentions is None or self._save_mentions is None:
            return None
        from rosemary.core.mentions import MODES

        current = self._effective_mention_policy()
        options = [
            discord.SelectOption(
                label=str(await self._t(f"cards.mentions.modes.{mode}"))[:100],
                value=mode,
                description=str(await self._t(f"cards.mentions.desc.{mode}"))[:100],
                default=(mode == current),
            )
            for mode in MODES
        ]
        select = self.make_select(
            custom_id="card_mentions",
            placeholder=str(await self._t("cards.editor.mentions.placeholder"))[:150],
            options=options,
        )
        return ActionRow(select)

    async def _build_add_screen(self) -> list[discord.ui.ViewItem]:
        allowed = ["text"] if self._inside_section() else list(_BLOCK_TYPES)
        options = [
            discord.SelectOption(
                label=await self._t(f"cards.editor.types.{name}"), value=name
            )
            for name in allowed
        ]
        select = self.make_select(
            custom_id="card_add_type",
            placeholder=await self._t("cards.editor.add_placeholder"),
            options=options,
        )
        container = designer_container(
            self.bot.theme.color("info"),
            TextDisplay(await self._t("cards.editor.add_title")),
            ActionRow(select),
            ActionRow(
                self.make_button(
                    custom_id="card_add_cancel",
                    label=await self._t("cards.editor.buttons.cancel"),
                    style=discord.ButtonStyle.secondary,
                )
            ),
        )
        return [container]

    async def _row_buttons(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        return [b for b in row.get("buttons", []) or [] if isinstance(b, dict)]

    async def _build_row_screen(self) -> list[discord.ui.ViewItem]:
        row = self._row_block()
        if row is None:
            self._editing_row = None
            return await self._build_selection_controls()
        buttons = await self._row_buttons(row)
        rows: list[discord.ui.ViewItem] = [
            TextDisplay(await self._t("cards.editor.row_buttons_title"))
        ]
        if not buttons:
            rows.append(TextDisplay(await self._t("cards.editor.row_buttons_empty")))
        else:
            options = [
                discord.SelectOption(
                    label=(b.get("label") or "(sem rótulo)")[:100],
                    value=str(b.get("id", index)),
                    description=str(b.get("url") or "")[:100] or None,
                    default=(index == 0),
                )
                for index, b in enumerate(buttons)
            ]
            rows.append(
                ActionRow(
                    self.make_select(
                        custom_id="card_row_edit",
                        placeholder=await self._t("cards.editor.select_placeholder"),
                        options=options,
                    )
                )
            )
        rows.append(
            ActionRow(
                self.make_button(
                    custom_id="card_row_add",
                    label=await self._t("cards.editor.buttons.add"),
                    style=discord.ButtonStyle.primary,
                    emoji=self.bot.theme.emojis.get("plus", ""),
                    disabled=len(buttons) >= 5,
                ),
                self.make_button(
                    custom_id="card_row_add_action",
                    label=await self._t("cards.editor.buttons.action_add"),
                    style=discord.ButtonStyle.primary,
                    disabled=len(buttons) >= 5,
                ),
                self.make_button(
                    custom_id="card_row_delete",
                    label=await self._t("cards.editor.buttons.delete"),
                    style=discord.ButtonStyle.danger,
                    emoji=self.bot.theme.emojis.get("trash", ""),
                    disabled=not buttons,
                ),
                self.make_button(
                    custom_id="card_row_back",
                    label=await self._t("cards.editor.buttons.back"),
                    emoji=self.bot.theme.emojis.get("back", ""),
                ),
            )
        )
        return rows

    async def _row_back(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self._editing_row = None
        self._adding_action = False
        self._adding_action_type = None
        await self.rerender(interaction)

    async def _build_action_pick_screen(self) -> list[discord.ui.ViewItem]:
        from rosemary.core.card_actions import ACTIONS

        options = [
            discord.SelectOption(
                label=str(await self._t(f"actions.{action}.label"))[:100],
                value=action,
                description=str(await self._t(f"actions.{action}.description"))[:100],
            )
            for action in ACTIONS
        ]
        return [
            TextDisplay(await self._t("cards.editor.action_title")),
            ActionRow(
                self.make_select(
                    custom_id="card_row_action",
                    placeholder=await self._t("cards.editor.action_placeholder"),
                    options=options,
                )
            ),
            ActionRow(
                self.make_button(
                    custom_id="card_row_back",
                    label=await self._t("cards.editor.buttons.back"),
                    emoji=self.bot.theme.emojis.get("back", ""),
                )
            ),
        ]

    async def _row_add_action(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        row = self._row_block()
        if row is None or len(await self._row_buttons(row)) >= 5:
            return await self.rerender(interaction)
        self._adding_action = True
        await self.rerender(interaction)

    async def _pick_row_action(self, interaction: discord.Interaction) -> None:
        from rosemary.core.card_actions import ACTIONS

        values = (interaction.data or {}).get("values") or []
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not values or values[0] not in ACTIONS:
            self._adding_action = False
            return await self.rerender(interaction)
        if values[0] == "open_ticket":
            self._adding_action = False
            self._adding_action_type = "open_ticket"
            return await self.rerender(interaction)
        self._adding_action = False
        return await interaction.response.send_modal(
            await self._action_label_modal(values[0], None)
        )

    async def _build_action_type_screen(self) -> list[discord.ui.ViewItem]:
        options = [
            discord.SelectOption(
                label=str(await self._t(f"tickets.types.{name}.label"))[:100],
                value=name,
            )
            for name in ("report", "confidential", "partnership", "boost")
        ]
        return [
            TextDisplay(await self._t("cards.editor.action_type_title")),
            ActionRow(
                self.make_select(
                    custom_id="card_row_action_type",
                    placeholder=await self._t("cards.editor.action_type_placeholder"),
                    options=options,
                )
            ),
            ActionRow(
                self.make_button(
                    custom_id="card_row_back",
                    label=await self._t("cards.editor.buttons.back"),
                    emoji=self.bot.theme.emojis.get("back", ""),
                )
            ),
        ]

    async def _pick_row_action_type(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not values or values[0] not in ("report", "confidential", "partnership", "boost"):
            self._adding_action_type = None
            return await self.rerender(interaction)
        ticket_type = values[0]
        self._adding_action_type = None
        return await interaction.response.send_modal(
            await self._action_label_modal("open_ticket", ticket_type)
        )

    async def _action_label_modal(self, action: str, ticket_type: str | None):
        from rosemary.ui.modals import make_text_modal

        suffix = f"{action}:{ticket_type or '-'}"
        return make_text_modal(
            title=(await self._t("cards.editor.button_title"))[:45],
            custom_id=f"card_action_modal:{self._editing_row}:{suffix}",
            label=await self._t("cards.editor.button_label_label"),
            placeholder=await self._t("cards.editor.button_label_placeholder"),
            value=str(await self._t(f"actions.{action}.label"))[:80],
            max_length=80,
            on_submit=self._action_label_submit,
        )

    async def _action_label_submit(self, interaction: discord.Interaction, value: str) -> None:
        from rosemary.core.cards import find_by_id, new_block_id

        try:
            _, row_id, action, ticket_type = interaction.custom_id.split(":")
        except ValueError:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            self._adding_action = False
            self._adding_action_type = None
            return await self.rerender(interaction)
        row = find_by_id(self.blocks, row_id)
        label = value.strip()
        buttons = [b for b in (row.get("buttons", []) or []) if isinstance(b, dict)] if isinstance(
            row, dict
        ) else []
        if (
            not isinstance(row, dict)
            or row.get("type") != "row"
            or not label
            or len(buttons) >= 5
            or action not in ("open_ticket", "dismiss")
        ):
            return await self._stale_modal(interaction, value)
        button: dict[str, Any] = {
            "id": new_block_id(),
            "label": label[:80],
            "action": action,
            "style": "primary",
        }
        if action == "open_ticket":
            button["ticket_type"] = ticket_type if ticket_type != "-" else "report"
        self._touch()
        buttons.append(button)
        row["buttons"] = buttons
        if not interaction.response.is_done():
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.defer(ephemeral=True)
        await self.rerender(interaction)

    async def _row_add(self, interaction: discord.Interaction) -> None:
        from rosemary.ui.modals import make_two_field_modal

        row = self._row_block()
        if row is None or len(await self._row_buttons(row)) >= 5:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return await self.rerender(interaction)
        return await interaction.response.send_modal(
            make_two_field_modal(
                title=(await self._t("cards.editor.button_title"))[:45],
                custom_id=f"card_button_modal:{row.get('id')}:new",
                first_label=await self._t("cards.editor.button_label_label"),
                first_placeholder=await self._t("cards.editor.button_label_placeholder"),
                second_label=await self._t("cards.editor.button_url_label"),
                second_placeholder=await self._t("cards.editor.button_url_placeholder"),
                second_max_length=512,
                on_submit=self._button_submit,
            )
        )

    async def _row_edit(self, interaction: discord.Interaction) -> None:
        from rosemary.ui.modals import make_two_field_modal

        values = (interaction.data or {}).get("values") or []
        row = self._row_block()
        wanted = values[0] if values else ""
        button = next(
            (b for b in (await self._row_buttons(row or {})) if str(b.get("id")) == wanted),
            None,
        )
        if row is None or button is None:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return await self.rerender(interaction)
        return await interaction.response.send_modal(
            make_two_field_modal(
                title=(await self._t("cards.editor.button_title"))[:45],
                custom_id=f"card_button_modal:{row.get('id')}:{button.get('id')}",
                first_label=await self._t("cards.editor.button_label_label"),
                first_placeholder=await self._t("cards.editor.button_label_placeholder"),
                first_value=str(button.get("label", "")),
                second_label=await self._t("cards.editor.button_url_label"),
                second_placeholder=await self._t("cards.editor.button_url_placeholder"),
                second_value=str(button.get("url", "")),
                second_max_length=512,
                on_submit=self._button_submit,
            )
        )

    async def _row_delete(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        row = self._row_block()
        if row is None:
            self._editing_row = None
            return await self.rerender(interaction)
        buttons = await self._row_buttons(row)
        target = values[0] if values else (buttons[0].get("id") if buttons else None)
        self._touch()
        row["buttons"] = [b for b in buttons if str(b.get("id")) != str(target)]
        await self.rerender(interaction)

    async def _button_submit(
        self, interaction: discord.Interaction, label: str, url: str
    ) -> None:
        """Validate one link button without fragile pipe parsing."""
        from rosemary.core.cards import CardIssue, CardsError, find_by_id, new_block_id

        try:
            _, row_id, button_id = interaction.custom_id.split(":")
        except ValueError:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return await self.rerender(interaction)
        row = find_by_id(self.blocks, row_id)
        if not isinstance(row, dict) or row.get("type") != "row":
            return await self._stale_modal(interaction, label)
        label, url = label.strip(), url.strip()
        if not label or not url:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            self.flash = await self._t(
                "cards.editor.invalid",
                error=await self._translate_issues(
                    CardsError([CardIssue("button_label", (("max", 80),))])
                ),
            )
            self.flash_color = "danger"
            return await self.rerender(interaction)
        buttons = [b for b in row.get("buttons", []) or [] if isinstance(b, dict)]
        existing = next((b for b in buttons if str(b.get("id")) == button_id), None)
        if existing is None:
            if len(buttons) >= 5:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                return await self.rerender(interaction)
            buttons.append({"id": new_block_id(), "label": label[:80], "url": url})
        else:
            existing["label"] = label[:80]
            existing["url"] = url
        self._touch()
        row["buttons"] = buttons
        if not interaction.response.is_done():
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.defer(ephemeral=True)
        await self.rerender(interaction)

    async def _build_divider_screen(self) -> list[discord.ui.ViewItem]:
        from rosemary.core.cards import find_by_id

        block = find_by_id(self.blocks, self._editing_divider or "")
        if not isinstance(block, dict) or block.get("type") != "divider":
            self._editing_divider = None
            return await self._build_selection_controls()
        current = "hidden" if block.get("visible") is False else block.get("spacing", "small")
        options = [
            discord.SelectOption(
                label=await self._t("cards.editor.spacing.small"),
                value="small",
                default=(current == "small"),
            ),
            discord.SelectOption(
                label=await self._t("cards.editor.spacing.large"),
                value="large",
                default=(current == "large"),
            ),
            discord.SelectOption(
                label=await self._t("cards.editor.spacing.hidden"),
                value="hidden",
                default=(current == "hidden"),
            ),
        ]
        return [
            TextDisplay(await self._t("cards.editor.divider_title")),
            ActionRow(
                self.make_select(
                    custom_id="card_divider",
                    placeholder=await self._t("cards.editor.divider_placeholder"),
                    options=options,
                )
            ),
            ActionRow(
                self.make_button(
                    custom_id="card_divider_back",
                    label=await self._t("cards.editor.buttons.back"),
                    emoji=self.bot.theme.emojis.get("back", ""),
                )
            ),
        ]

    async def _set_divider_style(self, interaction: discord.Interaction) -> None:
        from rosemary.core.cards import find_by_id

        values = (interaction.data or {}).get("values") or []
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        block = find_by_id(self.blocks, self._editing_divider or "")
        if not isinstance(block, dict) or block.get("type") != "divider":
            self._editing_divider = None
            return await self.rerender(interaction)
        if values and values[0] in ("small", "large", "hidden"):
            self._touch()
            if values[0] == "hidden":
                block["spacing"] = "small"
                block["visible"] = False
            else:
                block["spacing"] = values[0]
                block.pop("visible", None)
        await self.rerender(interaction)

    async def _divider_back(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self._editing_divider = None
        await self.rerender(interaction)

    async def _build_accessory_screen(self) -> list[discord.ui.ViewItem]:
        from rosemary.core.cards import find_by_id

        section = find_by_id(self.blocks, self._editing_accessory or "")
        if not isinstance(section, dict) or section.get("type") != "section":
            self._editing_accessory = None
            return await self._build_selection_controls()
        accessory = section.get("accessory") or {}
        current = accessory.get("type", "thumbnail")
        detail = str(accessory.get("url") or accessory.get("label") or "")
        return [
            TextDisplay(await self._t("cards.editor.accessory_title")),
            TextDisplay(detail[:200] if detail else "—"),
            ActionRow(
                self.make_select(
                    custom_id="card_accessory_type",
                    placeholder=await self._t("cards.editor.accessory_type_placeholder"),
                    options=[
                        discord.SelectOption(
                            label=await self._t("cards.editor.accessory_thumbnail"),
                            value="thumbnail",
                            default=(current == "thumbnail"),
                        ),
                        discord.SelectOption(
                            label=await self._t("cards.editor.accessory_button"),
                            value="button",
                            default=(current == "button"),
                        ),
                    ],
                )
            ),
            ActionRow(
                self.make_button(
                    custom_id="card_accessory_edit",
                    label=await self._t("cards.editor.buttons.edit"),
                    emoji=self.bot.theme.emojis.get("pencil", ""),
                ),
                self.make_button(
                    custom_id="card_accessory_back",
                    label=await self._t("cards.editor.buttons.back"),
                    emoji=self.bot.theme.emojis.get("back", ""),
                ),
            ),
        ]

    async def _open_accessory(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self._clear_subscreens()
        self._clamp_selection()
        current = self._current_blocks()
        selected = (
            current[self.selected] if self.selected is not None and current else None
        )
        if isinstance(selected, dict) and selected.get("type") == "section":
            self._editing_accessory = selected.get("id")
        await self.rerender(interaction)

    async def _set_accessory_type(self, interaction: discord.Interaction) -> None:
        from rosemary.core.cards import find_by_id

        values = (interaction.data or {}).get("values") or []
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        section = find_by_id(self.blocks, self._editing_accessory or "")
        if not isinstance(section, dict) or section.get("type") != "section":
            self._editing_accessory = None
            return await self.rerender(interaction)
        if values and values[0] in ("thumbnail", "button"):
            self._touch()
            if values[0] == "thumbnail":
                section["accessory"] = {"type": "thumbnail", "url": "{user_avatar}"}
            else:
                section["accessory"] = {"type": "button", "label": "", "url": ""}
        await self.rerender(interaction)

    async def _edit_accessory_values(self, interaction: discord.Interaction) -> None:
        from rosemary.core.cards import find_by_id
        from rosemary.ui.modals import make_text_modal, make_two_field_modal

        section = find_by_id(self.blocks, self._editing_accessory or "")
        if not isinstance(section, dict) or section.get("type") != "section":
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            self._editing_accessory = None
            return await self.rerender(interaction)
        accessory = section.get("accessory") or {}
        section_id = section.get("id", "")
        if accessory.get("type") == "button":
            return await interaction.response.send_modal(
                make_two_field_modal(
                    title=(await self._t("cards.editor.button_title"))[:45],
                    custom_id=f"card_accessory_modal:{section_id}",
                    first_label=await self._t("cards.editor.button_label_label"),
                    first_placeholder=await self._t("cards.editor.button_label_placeholder"),
                    first_value=str(accessory.get("label", "")),
                    second_label=await self._t("cards.editor.button_url_label"),
                    second_placeholder=await self._t("cards.editor.button_url_placeholder"),
                    second_value=str(accessory.get("url", "")),
                    second_max_length=512,
                    on_submit=self._accessory_submit,
                )
            )
        return await interaction.response.send_modal(
            make_text_modal(
                title=(await self._t("cards.editor.accessory_thumbnail"))[:45],
                custom_id=f"card_thumbnail_modal:{section_id}",
                label=await self._t("cards.editor.button_url_label"),
                placeholder="{user_avatar} ou https://...",
                value=str(accessory.get("url", ""))[:512],
                max_length=512,
                on_submit=self._thumbnail_submit,
            )
        )

    async def _accessory_submit(
        self, interaction: discord.Interaction, label: str, url: str
    ) -> None:
        from rosemary.core.cards import find_by_id

        try:
            section_id = interaction.custom_id.split(":")[-1]
        except (ValueError, AttributeError):
            section_id = ""
        section = find_by_id(self.blocks, section_id)
        if not isinstance(section, dict) or section.get("type") != "section":
            return await self._stale_modal(interaction, label)
        self._touch()
        section["accessory"] = {"type": "button", "label": label.strip()[:80], "url": url.strip()}
        if not interaction.response.is_done():
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.defer(ephemeral=True)
        await self.rerender(interaction)

    async def _thumbnail_submit(self, interaction: discord.Interaction, value: str) -> None:
        from rosemary.core.cards import find_by_id

        try:
            section_id = interaction.custom_id.split(":")[-1]
        except (ValueError, AttributeError):
            section_id = ""
        section = find_by_id(self.blocks, section_id)
        if not isinstance(section, dict) or section.get("type") != "section":
            return await self._stale_modal(interaction, value)
        self._touch()
        section["accessory"] = {"type": "thumbnail", "url": value.strip() or "{user_avatar}"}
        if not interaction.response.is_done():
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.defer(ephemeral=True)
        await self.rerender(interaction)

    async def _accessory_back(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self._editing_accessory = None
        await self.rerender(interaction)

    async def _back_button(self) -> discord.ui.Button:
        return self.make_button(
            custom_id="card_more_back",
            label=await self._t("cards.editor.buttons.back"),
            emoji=self.bot.theme.emojis.get("back", ""),
        )

    async def _build_more_screen(self) -> list[discord.ui.ViewItem]:
        theme = self.bot.theme
        return [
            TextDisplay(await self._t("cards.editor.more_title")),
            ActionRow(
                self.make_button(
                    custom_id="card_templates",
                    label=await self._t("cards.editor.buttons.templates"),
                    emoji=theme.emojis.get("plus", ""),
                ),
                self.make_button(
                    custom_id="card_history",
                    label=await self._t("cards.editor.buttons.history"),
                    emoji=theme.emojis.get("clock", ""),
                ),
                self.make_button(
                    custom_id="card_export",
                    label=await self._t("cards.editor.buttons.export"),
                ),
            ),
            ActionRow(
                self.make_button(
                    custom_id="card_import",
                    label=await self._t("cards.editor.buttons.import"),
                ),
                self.make_button(
                    custom_id="card_reset",
                    label=await self._t("cards.editor.buttons.reset"),
                    style=discord.ButtonStyle.danger,
                    emoji=theme.emojis.get("refresh", ""),
                ),
                await self._back_button(),
            ),
        ]

    async def _open_more(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self._show_more = True
        self._show_templates = False
        self._show_history = False
        await self.rerender(interaction)

    async def _more_back(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self._show_more = False
        self._show_templates = False
        self._show_history = False
        await self.rerender(interaction)

    async def _build_templates_screen(self) -> list[discord.ui.ViewItem]:
        from rosemary.core.card_templates import list_templates

        templates = list_templates()
        rows: list[discord.ui.ViewItem] = [
            TextDisplay(await self._t("cards.editor.templates_title"))
        ]
        if not templates:
            rows.append(TextDisplay(await self._t("cards.editor.templates_empty")))
        else:
            options = [
                discord.SelectOption(
                    label=str(await self._t(template.label_key))[:100],
                    value=template.slug,
                    description=str(await self._t(template.description_key))[:100],
                )
                for template in templates
            ]
            rows.append(
                ActionRow(
                    self.make_select(
                        custom_id="card_template_pick",
                        placeholder=await self._t("cards.editor.templates_placeholder"),
                        options=options,
                    )
                )
            )
        rows.append(ActionRow(await self._back_button()))
        return rows

    async def _open_templates(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self._show_templates = True
        await self.rerender(interaction)

    async def _pick_template(self, interaction: discord.Interaction) -> None:
        from rosemary.core.card_templates import get_template
        from rosemary.core.cards import ensure_ids

        values = (interaction.data or {}).get("values") or []
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        template = get_template(values[0]) if values else None
        if template is None:
            return await self.rerender(interaction)
        self._touch()
        self.blocks = copy.deepcopy(template.blocks)
        ensure_ids({"blocks": self.blocks})
        self.path.clear()
        self.selected = None
        self._clear_subscreens()
        self._show_templates = False
        self.flash = await self._t("cards.editor.templates_applied")
        self.flash_color = "success"
        await self.rerender(interaction)

    async def _build_history_screen(self) -> list[discord.ui.ViewItem]:
        from rosemary.core.card_history import history_store

        versions = await history_store(self.bot).list(self.guild_id, self.key)
        rows: list[discord.ui.ViewItem] = [
            TextDisplay(await self._t("cards.editor.history_title"))
        ]
        if not versions:
            rows.append(TextDisplay(await self._t("cards.editor.history_empty")))
        else:
            from datetime import datetime

            options = []
            for version in versions[-25:]:
                stamp = datetime.fromtimestamp(version.get("at") or 0).strftime("%d/%m %H:%M")
                options.append(
                    discord.SelectOption(
                        label=f"v{version.get('ver')} · {stamp}"[:100],
                        value=str(version.get("ver")),
                    )
                )
            rows.append(
                ActionRow(
                    self.make_select(
                        custom_id="card_history_pick",
                        placeholder=await self._t("cards.editor.history_placeholder"),
                        options=options,
                    )
                )
            )
        rows.append(ActionRow(await self._back_button()))
        return rows

    async def _open_history(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self._show_history = True
        await self.rerender(interaction)

    async def _pick_history(self, interaction: discord.Interaction) -> None:
        from rosemary.core.card_history import history_store

        values = (interaction.data or {}).get("values") or []
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            ver = int(values[0]) if values else -1
        except (TypeError, ValueError):
            ver = -1
        doc = await history_store(self.bot).get(self.guild_id, self.key, ver)
        if not isinstance(doc, dict) or not isinstance(doc.get("blocks"), list):
            return await self.rerender(interaction)
        self._touch()
        self.blocks = copy.deepcopy(doc["blocks"])
        from rosemary.core.cards import ensure_ids

        ensure_ids({"blocks": self.blocks})
        self.path.clear()
        self.selected = None
        self._clear_subscreens()
        self._show_history = False
        self.flash = await self._t("cards.editor.history_restored")
        self.flash_color = "success"
        await self.rerender(interaction)

    async def _export(self, interaction: discord.Interaction) -> None:
        import io
        import json
        import time

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        payload = {
            "v": 2,
            "key": self.key,
            "exported_at": int(time.time()),
            "blocks": self.blocks,
        }
        await interaction.followup.send(
            content=await self._t("cards.editor.exported"),
            file=discord.File(
                io.BytesIO(json.dumps(payload, indent=2, ensure_ascii=False).encode()),
                filename=f"{self.key.replace('.', '-')}.json",
            ),
            ephemeral=True,
        )
        await self.rerender(interaction)

    async def _import(self, interaction: discord.Interaction) -> None:
        from rosemary.ui.modals import make_file_modal

        return await interaction.response.send_modal(
            make_file_modal(
                title=(await self._t("cards.editor.import_title"))[:45],
                custom_id=f"card_import_modal:{self.key}",
                label=await self._t("cards.editor.import_label"),
                description=await self._t("cards.editor.import_description"),
                required=True,
                on_submit=self._import_submit,
            )
        )

    async def _import_submit(
        self, interaction: discord.Interaction, files: list[discord.Attachment]
    ) -> None:
        import json

        if not interaction.response.is_done():
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.defer(ephemeral=True)
        if not files:
            return await self.rerender(interaction)
        try:
            raw = await files[0].read()
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            payload = None
        blocks = payload.get("blocks") if isinstance(payload, dict) else None
        if not isinstance(blocks, list) or not blocks:
            self.flash = await self._t("cards.editor.import_invalid")
            self.flash_color = "danger"
            return await self.rerender(interaction)
        from rosemary.core.cards import CardsError, ensure_ids, validate_document

        doc = ensure_ids({"v": 2, "blocks": copy.deepcopy(blocks)})
        issues = validate_document(doc, theme=self.bot.theme, draft=True)
        if issues:
            self.flash = await self._t(
                "cards.editor.invalid",
                error=await self._translate_issues(CardsError(list(issues))),
            )
            self.flash_color = "danger"
            return await self.rerender(interaction)
        self._touch()
        self.blocks = copy.deepcopy(blocks)
        ensure_ids({"blocks": self.blocks})
        self.path.clear()
        self.selected = None
        self._clear_subscreens()
        self._show_more = False
        self.flash = await self._t("cards.editor.imported")
        self.flash_color = "success"
        await self.rerender(interaction)

    async def _build_nav_rows(self) -> list[discord.ui.ViewItem]:
        theme = self.bot.theme
        primary: list[discord.ui.Button] = [
            self.make_button(
                custom_id="card_add",
                label=await self._t("cards.editor.buttons.add"),
                style=discord.ButtonStyle.primary,
                emoji=theme.emojis.get("plus", ""),
            ),
            self.make_button(
                custom_id="card_test",
                label=await self._t("cards.editor.buttons.test"),
                emoji=theme.emojis.get("rocket", ""),
            ),
        ]
        if get_default_builder(self.key) is not None:
            primary.append(
                self.make_button(
                    custom_id="card_compare",
                    label=await self._t("cards.editor.buttons.compare"),
                    emoji=theme.emojis.get("bar_chart", ""),
                )
            )
        primary.append(
            self.make_button(
                custom_id="card_more",
                label=await self._t("cards.editor.buttons.more"),
            )
        )
        save_row: list[discord.ui.Button] = [
            self.make_button(
                custom_id="card_save",
                label=await self._t("cards.editor.buttons.save"),
                style=discord.ButtonStyle.success,
            ),
            self.make_button(
                custom_id="card_discard",
                label=await self._t("cards.editor.buttons.discard"),
                style=discord.ButtonStyle.secondary,
                disabled=not self.is_dirty(),
            ),
            self.make_button(
                custom_id="card_undo",
                label=await self._t("cards.editor.buttons.undo"),
                disabled=not self._undo,
            ),
        ]

        # Close stays first so it never jumps when sibling buttons appear.
        secondary: list[discord.ui.Button] = [
            self.make_button(
                custom_id="card_close",
                label=await self._t("cards.editor.buttons.close"),
                style=discord.ButtonStyle.secondary,
            )
        ]
        # Up-navigation lives in the breadcrumb row when inside a level.
        if self._exit_factory is not None:
            secondary.append(
                self.make_button(
                    custom_id="card_exit",
                    label=await self._t("cards.editor.buttons.back"),
                    emoji=theme.emojis.get("back", ""),
                )
            )
        return [ActionRow(*primary), ActionRow(*save_row), ActionRow(*secondary)]

    def _compare_summary(self, effective: dict | None) -> tuple[int, int, int]:
        """(same, changed, total) top-level blocks vs the effective document."""
        import json

        mine = self.blocks
        theirs = effective.get("blocks", []) if isinstance(effective, dict) else []
        total = max(len(mine), len(theirs))
        same = sum(
            1
            for a, b in zip(mine, theirs, strict=False)
            if json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        )
        return same, total - same, total

    async def _build_compare_screen(self) -> list[discord.ui.ViewItem]:
        theme = self.bot.theme
        effective = await self._effective_document()
        same, changed, total = self._compare_summary(effective)
        parts: list[discord.ui.ViewItem] = [
            designer_container(
                theme.color("info"),
                TextDisplay(
                    theme.md("title", title=await self._t("cards.editor.compare_title"))
                ),
                TextDisplay(
                    await self._t(
                        "cards.editor.compare_summary",
                        same=same,
                        changed=changed,
                        total=total,
                    )
                ),
            ),
            TextDisplay(
                theme.md("section", title=await self._t("cards.editor.compare_default"))
            ),
        ]

        if effective is not None:
            try:
                effective_items = build_items(theme, effective, self._sample_variables())
                bind_action_callbacks(effective_items)
                parts.extend(effective_items)
            except CardsError as exc:
                parts.append(TextDisplay(
                    await self._t("cards.editor.invalid",
                                  error=await self._translate_issues(exc))
                ))
        else:
            parts.append(TextDisplay(await self._t("cards.editor.compare_no_default")))

        parts.append(divider("large"))
        parts.append(
            TextDisplay(
                theme.md("section", title=await self._t("cards.editor.compare_custom"))
            )
        )
        try:
            custom_items = build_items(
                theme, self.document(), self._preview_variables(), draft=True,
                card_key=self.key,
            )
            bind_action_callbacks(custom_items)
            parts.extend(custom_items)
        except CardsError as exc:
            parts.append(
                TextDisplay(
                    await self._t(
                        "cards.editor.invalid",
                        error=await self._translate_issues(exc),
                    )
                )
            )
        if self.using_default_base and not self.saved_exists:
            parts.append(
                TextDisplay(
                    theme.md("footer", text=await self._t("cards.editor.using_default"))
                )
            )

        parts.append(divider("large"))
        parts.append(
            ActionRow(
                self.make_button(
                    custom_id="card_compare_close",
                    label=await self._t("cards.editor.buttons.back"),
                    emoji=theme.emojis.get("back", ""),
                    style=discord.ButtonStyle.primary,
                ),
                self.make_button(
                    custom_id="card_close",
                    label=await self._t("cards.editor.buttons.close"),
                    style=discord.ButtonStyle.secondary,
                ),
            )
        )
        return parts

    async def _effective_document(self) -> dict[str, Any] | None:
        """What members receive today: the saved override, else the default."""
        if self.saved_exists:
            saved = await self._load_doc(self.guild_id)
            if isinstance(saved, dict) and isinstance(saved.get("blocks"), list):
                return saved
        builder = get_default_builder(self.key)
        if builder is None:
            return None
        try:
            doc = await builder(self.bot, self.guild_id)
        except Exception as exc:
            log_default_failure(self.key, exc)
            return None
        return doc if isinstance(doc, dict) else None

    def _preview_variables(self) -> dict[str, Any]:
        """Mapping for preview/compare/test: samples for every placeholder.

        Guild-derived values (server name, bot display name, author mention)
        render for real and every other known placeholder gets a sample
        value, so neither side of the compare screen shows raw
        ``{inviter}``-style gaps.
        """
        from rosemary.core.cards import get_card
        from rosemary.core.variables import samples_for

        spec = get_card(self.key)
        base = samples_for(spec.variables) if spec is not None else {}
        get_guild = getattr(self.bot, "get_guild", None)
        guild = get_guild(self.guild_id) if callable(get_guild) else None
        base.update(
            user=f"<@{self.author_id or 0}>",
            server=getattr(guild, "name", "…"),
            user_name=getattr(getattr(guild, "me", None), "display_name", "@voce"),
        )
        return base

    def _sample_variables(self) -> dict[str, Any]:
        """Legacy alias kept for the compare screen; prefer _preview_variables."""
        return self._preview_variables()

    # -- handlers ------------------------------------------------------------

    async def _close(self, interaction: discord.Interaction) -> None:
        import contextlib

        self.stop()
        with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            self.disable_all_items()
            await interaction.edit(view=self)

    async def _open_add(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self._clear_subscreens()
        self.adding = True
        self.comparing = False
        self.selected = None
        await self.rerender(interaction)

    async def _cancel_add(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self.adding = False
        await self.rerender(interaction)

    async def _pick_add_type(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not values or values[0] not in _BLOCK_TYPES:
            self.adding = False
            return await self.rerender(interaction)
        block = _skeleton(values[0])
        current = self._current_blocks()
        self._touch()
        current.append(block)
        self._touch()
        self.adding = False
        self.selected = len(current) - 1
        await self.rerender(interaction)

    async def _select_block(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            index = int(values[0]) if values else -1
        except (TypeError, ValueError):
            index = -1
        if not 0 <= index < len(self._current_blocks()):
            self.selected = None
            return await self.rerender(interaction)
        self.selected = index
        self._block_page = index // _BLOCK_PAGE_SIZE
        self._clear_subscreens()
        await self.rerender(interaction)

    async def _block_page_prev(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self._block_page = max(self._block_page - 1, 0)
        await self.rerender(interaction)

    async def _block_page_next(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self._block_page += 1
        await self.rerender(interaction)

    async def _act_on_selected(self, interaction: discord.Interaction) -> None:
        try:
            _, action = interaction.custom_id.split(":", 1)
        except ValueError:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return await self.rerender(interaction)
        self._clamp_selection()
        blocks = self._current_blocks()
        index = self.selected
        if index is None or not 0 <= index < len(blocks):
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return await self.rerender(interaction)
        block = blocks[index]

        if action == "enter":
            if block.get("type") not in _COMPOSITE_TYPES:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                return
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            self.adding = False
            self.selected = None
            self.path.append(index)
            return await self.rerender(interaction)

        if action == "edit":
            kind = block["type"]
            if kind in _COMPOSITE_TYPES:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                self.adding = False
                self.selected = None
                self.path.append(index)
                return await self.rerender(interaction)
            if kind == "text":
                return await interaction.response.send_modal(
                    await self._text_modal(block, index)
                )
            if kind == "gallery":
                return await interaction.response.send_modal(
                    await self._urls_modal(block, index)
                )
            if kind == "row":
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                self._clear_subscreens()
                self._editing_row = block.get("id")
                return await self.rerender(interaction)
            if kind == "divider":
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                self._clear_subscreens()
                self._editing_divider = block.get("id")
                return await self.rerender(interaction)
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if action == "up" and index > 0:
            self._touch()
            blocks[index - 1], blocks[index] = blocks[index], blocks[index - 1]
            self.selected = index - 1
        elif action == "down" and index < len(blocks) - 1:
            self._touch()
            blocks[index + 1], blocks[index] = blocks[index], blocks[index + 1]
            self.selected = index + 1
        elif action == "duplicate":
            from rosemary.core.cards import reid_tree

            clone = reid_tree(copy.deepcopy(block))
            self._touch()
            blocks.insert(index + 1, clone)
            self.selected = index + 1
        elif action == "move_out" and self.path:
            self._touch()
            moving = blocks.pop(index)
            parent_level = self._parent_blocks()
            parent_index = self.path[-1]
            parent_level.insert(parent_index + 1, moving)
            self.path.pop()
            self.selected = parent_index + 1
            self._touch()
        elif action == "delete":
            armed = f"del:{index}"
            if self._armed != armed:
                self._armed = armed
                self.flash = await self._t("cards.editor.arm_delete")
                self.flash_color = "danger"
                return await self.rerender(interaction)
            self._armed = None
            self._touch()
            del blocks[index]
            self.selected = None
        await self.rerender(interaction)

    def _parent_blocks(self) -> list[dict[str, Any]]:
        """Level containing the current composite (one step above path)."""
        node: Any = self.blocks
        for index in self.path[:-1]:
            node = node[index]["children"]
        return node

    def _modal_suffix(self, block: dict[str, Any]) -> str:
        """Stable block id: survives move/delete/reset between open and submit."""
        from rosemary.core.cards import ensure_ids

        if not isinstance(block.get("id"), str):
            ensure_ids({"blocks": self.blocks})
        return str(block.get("id", ""))

    async def _text_modal(self, block: dict[str, Any], index: int):
        return make_text_modal(
            title=(await self._t("cards.editor.modals.text_title"))[:45],
            custom_id=f"card_text_modal:{self._modal_suffix(block)}",
            label=await self._t("cards.editor.modals.text_label"),
            placeholder=await self._t("cards.editor.modals.text_placeholder"),
            # Discord caps modal input at 4000 chars: prefill longer bodies
            # truncated rather than failing the modal open with a 400.
            value=str(block.get("body", ""))[:4000],
            max_length=4000,
            on_submit=self._text_submit,
        )

    async def _urls_modal(self, block: dict[str, Any], index: int):
        return make_text_modal(
            title=(await self._t("cards.editor.modals.urls_title"))[:45],
            custom_id=f"card_urls_modal:{self._modal_suffix(block)}",
            label=await self._t("cards.editor.modals.urls_label"),
            placeholder=await self._t("cards.editor.modals.urls_placeholder"),
            value="\n".join(block.get("urls", []))[:4000],
            max_length=4000,
            on_submit=self._urls_submit,
        )

    async def _row_modal(self, block: dict[str, Any], index: int):
        lines = [
            f"{button.get('label', '')} | {button.get('url', '')}"
            for button in block.get("buttons", [])
            if isinstance(button, dict)
        ]
        return make_text_modal(
            title=(await self._t("cards.editor.modals.row_title"))[:45],
            custom_id=f"card_row_modal:{self._modal_suffix(block)}",
            label=await self._t("cards.editor.modals.row_label"),
            placeholder=await self._t("cards.editor.modals.row_placeholder"),
            value="\n".join(lines)[:4000],
            max_length=4000,
            on_submit=self._row_submit,
        )

    async def _up_level(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if self.path:
            self.path.pop()
            self.selected = None
        self.adding = False
        self._clear_subscreens()
        await self.rerender(interaction)

    async def _reset(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if self._armed != "reset":
            self._armed = "reset"
            self.flash = await self._t("cards.editor.arm_reset")
            self.flash_color = "danger"
            return await self.rerender(interaction)
        self._armed = None
        await self._reset_doc(self.guild_id)
        self.path.clear()
        self.selected = None
        self.adding = False
        self.comparing = False
        self.mention_policy = None
        self.saved_exists = False
        seeded = await self._default_blocks()
        if seeded is not None:
            self.blocks = copy.deepcopy(seeded)
            self.using_default_base = True
        else:
            self.blocks = [_skeleton("text")]
            self.using_default_base = False
        from rosemary.core.card_actions import sync_guild
        from rosemary.core.cards import ensure_ids

        ensure_ids({"blocks": self.blocks})
        self._snapshot_saved()
        self._undo.clear()
        self._redo.clear()
        await sync_guild(self.bot, self.guild_id)
        self.flash = await self._t("cards.editor.reset_done")
        self.flash_color = "warning"
        await self.rerender(interaction)

    async def _test(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            items = build_items(
                self.bot.theme, self.document(), self._preview_variables(), draft=True,
                card_key=self.key,
            )
            bind_action_callbacks(items)
        except CardsError as exc:
            self.flash = await self._t(
                "cards.editor.invalid", error=await self._translate_issues(exc)
            )
            self.flash_color = "danger"
            return await self.rerender(interaction)
        view = discord.ui.DesignerView(store=False)
        for item in items:
            view.add_item(item)
        await interaction.followup.send(view=view, ephemeral=True)
        self.flash = await self._t("cards.editor.test_sent")
        self.flash_color = "success"
        await self.rerender(interaction)

    async def _open_compare(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self.comparing = True
        self.adding = False
        await self.rerender(interaction)

    async def _close_compare(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self.comparing = False
        await self.rerender(interaction)

    async def _set_container_color(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        self._sanitize_path()
        if not values or not self.path or values[0] not in (*_THEME_COLORS, ""):
            return await self.rerender(interaction)
        try:
            node: Any = self.blocks
            for index in self.path[:-1]:
                node = node[index]["children"]
            target = node[self.path[-1]]
        except (IndexError, KeyError, TypeError):
            return await self.rerender(interaction)
        if not isinstance(target, dict):
            return await self.rerender(interaction)
        self._touch()
        target["color"] = values[0] or None
        await self.rerender(interaction)

    async def _set_mentions(self, interaction: discord.Interaction) -> None:
        from rosemary.core.mentions import valid_policy

        values = (interaction.data or {}).get("values") or []
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not values or self._save_mentions is None:
            return await self.rerender(interaction)
        policy = values[0]
        if not valid_policy(policy):
            return await self.rerender(interaction)
        if policy == "all" and self._armed != "mentions:all":
            self._armed = "mentions:all"
            self.flash = await self._t("cards.editor.arm_mentions")
            self.flash_color = "danger"
            return await self.rerender(interaction)
        self._armed = None
        try:
            await self._save_mentions(self.guild_id, policy)
        except (ValueError, OSError):
            return await self.rerender(interaction)
        self.mention_policy = policy
        self.flash = await self._t("cards.editor.mentions.saved")
        self.flash_color = "success"
        await self.rerender(interaction)

    async def _lint_lines(self) -> list[str]:
        """Unknown-placeholder hints for the draft (never blocks saving)."""
        from rosemary.core.cards import get_card
        from rosemary.core.variables import ALIASES, lint_placeholders

        spec = get_card(self.key)
        if spec is None:
            return []
        allowed = (
            set(spec.variables)
            | set(ALIASES)
            | set(ALIASES.values())
            | set(self.bot.theme.emojis)
        )
        lines = []
        for unknown, suggestion in lint_placeholders(self.document(), allowed):
            lines.append(await self._t("cards.editor.lint_unknown", unknown=unknown))
            if suggestion is not None:
                lines.append(
                    await self._t("cards.editor.lint_suggestion", suggestion=suggestion)
                )
        return lines

    async def _translate_issues(self, exc: CardsError) -> str:
        """Render structured issues through the cards.errors.* catalog keys."""
        lines = [
            await self._t(f"cards.errors.{issue.code}", **issue.kwargs)
            for issue in exc.issues
        ]
        return "\n".join(lines) or describe_fallback(exc)

    async def _exit_to_origin(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        factory = self._exit_factory
        if factory is None:
            return await self.rerender(interaction)
        try:
            target = factory()
            await target.prepare()
        except Exception:
            return await self.rerender(interaction)
        try:
            await interaction.edit(view=target)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        self.stop()

    # -- modal submits -------------------------------------------------------

    def _modal_target(
        self, interaction: discord.Interaction
    ) -> dict[str, Any] | None:
        """Resolve a modal submit by stable block id (survives move/delete)."""
        from rosemary.core.cards import find_by_id

        try:
            block_id = interaction.custom_id.split(":")[-1]
        except (ValueError, AttributeError):
            return None
        if not block_id:
            return None
        return find_by_id(self.blocks, block_id)

    async def _after_modal(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.defer(ephemeral=True)
        await self.rerender(interaction)

    async def _stale_modal(self, interaction: discord.Interaction, value: str) -> None:
        """ACK a submit for a removed block without losing the typed text."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        kept = value.strip().replace("\n", " ")[:200]
        self.flash = await self._t("cards.editor.modal_stale", value=kept)
        self.flash_color = "danger"
        await self.rerender(interaction)

    async def _text_submit(self, interaction: discord.Interaction, value: str) -> None:
        block = self._modal_target(interaction)
        if block is None or block.get("type") != "text":
            return await self._stale_modal(interaction, value)
        self._touch()
        block["body"] = value.strip("\n")
        await self._after_modal(interaction)

    async def _urls_submit(self, interaction: discord.Interaction, value: str) -> None:
        block = self._modal_target(interaction)
        if block is None or block.get("type") != "gallery":
            return await self._stale_modal(interaction, value)
        self._touch()
        block["urls"] = [line.strip() for line in value.splitlines() if line.strip()]
        await self._after_modal(interaction)

    async def _row_submit(self, interaction: discord.Interaction, value: str) -> None:
        block = self._modal_target(interaction)
        if block is None or block.get("type") != "row":
            return await self._stale_modal(interaction, value)
        buttons = []
        for line in value.splitlines():
            # Split on the LAST pipe so labels may contain "|".
            label, sep, url = line.rpartition("|")
            if not sep:
                label, url = line, ""
            if not label.strip():
                continue
            from rosemary.core.cards import new_block_id

            buttons.append({"id": new_block_id(), "label": label.strip()[:80], "url": url.strip()})
        self._touch()
        block["buttons"] = buttons
        await self._after_modal(interaction)


def describe_fallback(exc: CardsError) -> str:
    import logging

    logging.getLogger(__name__).debug("untranslated card issues: %s", exc)
    return str(exc) or "invalid card"


def log_default_failure(key: str, exc: Exception) -> None:
    import logging

    logging.getLogger(__name__).warning(
        "default builder failed for card %s: %s", key, exc
    )
