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
    """Return an empty-but-shaped block for the given type."""
    if block_type == "text":
        return {"type": "text", "body": ""}
    if block_type == "container":
        return {"type": "container", "color": None, "children": [_skeleton("text")]}
    if block_type == "section":
        return {
            "type": "section",
            "accessory": {"type": "thumbnail", "url": "{user_avatar}"},
            "children": [_skeleton("text")],
        }
    if block_type == "divider":
        return {"type": "divider", "spacing": "small"}
    if block_type == "gallery":
        return {"type": "gallery", "urls": []}
    if block_type == "row":
        return {"type": "row", "buttons": [{"label": "", "url": ""}]}
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
        self.register("card_reset", self._reset)
        self.register("card_test", self._test)
        self.register("card_compare", self._open_compare)
        self.register("card_compare_close", self._close_compare)
        self.register("card_color", self._set_container_color)
        self.register("card_exit", self._exit_to_origin)
        self.register("card_mentions", self._set_mentions)

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

    async def _persist(self) -> bool:
        """Validate (draft-tolerant) and save; ``False`` keeps the last save.

        Empty-text skeletons are tolerated so a just-added block can be
        edited; any other structural problem rejects the change, reloads the
        last saved document and flashes the translated issues instead of
        persisting a card that would silently fall back at send time.
        """
        from rosemary.core.cards import CardsError, validate_document

        issues = validate_document(self.document(), theme=self.bot.theme, draft=True)
        if issues:
            await self._reload()
            self.flash = await self._t(
                "cards.editor.invalid",
                error=await self._translate_issues(CardsError(list(issues))),
            )
            self.flash_color = "danger"
            return False
        await self._save_doc(self.guild_id, self.document())
        self.saved_exists = True
        self.using_default_base = False
        self.flash = await self._t("cards.editor.saved")
        self.flash_color = "success"
        return True

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
        doc = await self._load_doc(self.guild_id)
        blocks = doc.get("blocks") if isinstance(doc, dict) else None
        if isinstance(blocks, list):
            self.blocks = copy.deepcopy(blocks)
            self.saved_exists = True
            self.using_default_base = False
            self.loaded = True
            return

        self.saved_exists = False
        seeded = await self._default_blocks()
        if seeded is not None:
            self.blocks = copy.deepcopy(seeded)
            self.using_default_base = True
        else:
            self.blocks = [_skeleton("text")]
            self.using_default_base = False
        self.loaded = True

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

        try:
            parts.extend(
                build_items(theme, self.document(), self._preview_variables(), draft=True)
            )
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

        if self.using_default_base and not self.saved_exists:
            parts.append(
                TextDisplay(
                    theme.md(
                        "footer",
                        text=await self._t("cards.editor.using_default"),
                    )
                )
            )

        if self.adding:
            parts.extend(await self._build_add_screen())
        else:
            parts.extend(await self._build_selection_controls())
        mentions_row = await self._build_mentions_row()
        if mentions_row is not None:
            parts.append(mentions_row)
        parts.extend(await self._build_nav_rows())
        return parts

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
            # Discord selects cap at 25 options: window around the selection.
            total = len(current)
            start = 0
            if total > _BLOCK_PAGE_SIZE:
                anchor = self.selected if self.selected is not None else 0
                start = min(max(anchor - _BLOCK_PAGE_SIZE // 2, 0), total - _BLOCK_PAGE_SIZE)
            window = current[start : start + _BLOCK_PAGE_SIZE]
            options = [
                await _block_option(
                    self.bot, self.guild_id, start + index, block,
                    start + index == self.selected,
                )
                for index, block in enumerate(window)
            ]
            if total > _BLOCK_PAGE_SIZE:
                rows.append(
                    TextDisplay(
                        await self._t(
                            "cards.editor.paged_blocks",
                            shown=len(window),
                            total=total,
                        )
                    )
                )
            select = self.make_select(
                custom_id="card_select",
                placeholder=await self._t("cards.editor.select_placeholder"),
                options=options,
            )
            rows.append(ActionRow(select))
            rows.append(await self._context_row(current))

        if self._inside_container():
            rows.append(await self._color_select_row())
        return rows

    async def _context_row(self, current: list[dict[str, Any]]) -> discord.ui.ViewItem:
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
        delete = self.make_button(
            custom_id="card_act:delete",
            label=await self._t("cards.editor.buttons.delete"),
            style=discord.ButtonStyle.danger,
            emoji=self.bot.theme.emojis.get("trash", ""),
            disabled=nothing,
        )
        buttons.append(delete)
        return ActionRow(*buttons)

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
                custom_id="card_reset",
                label=await self._t("cards.editor.buttons.reset"),
                style=discord.ButtonStyle.danger,
                emoji=theme.emojis.get("refresh", ""),
            )
        )

        # Close stays first so it never jumps when sibling buttons appear.
        secondary: list[discord.ui.Button] = [
            self.make_button(
                custom_id="card_close",
                label=await self._t("cards.editor.buttons.close"),
                style=discord.ButtonStyle.secondary,
            )
        ]
        if self.path:
            secondary.append(
                self.make_button(
                    custom_id="card_uplevel",
                    label=await self._t("cards.editor.buttons.up_level"),
                    emoji=theme.emojis.get("up", ""),
                )
            )
        if self._exit_factory is not None:
            secondary.append(
                self.make_button(
                    custom_id="card_exit",
                    label=await self._t("cards.editor.buttons.back"),
                    emoji=theme.emojis.get("back", ""),
                )
            )
        return [ActionRow(*primary), ActionRow(*secondary)]

    async def _build_compare_screen(self) -> list[discord.ui.ViewItem]:
        theme = self.bot.theme
        parts: list[discord.ui.ViewItem] = [
            designer_container(
                theme.color("info"),
                TextDisplay(
                    theme.md("title", title=await self._t("cards.editor.compare_title"))
                ),
            ),
            TextDisplay(
                theme.md("section", title=await self._t("cards.editor.compare_default"))
            ),
        ]

        effective = await self._effective_document()
        if effective is not None:
            try:
                parts.extend(
                    build_items(theme, effective, self._sample_variables())
                )
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
            parts.extend(
                build_items(theme, self.document(), self._preview_variables(), draft=True)
            )
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
        from rosemary.core.cards import preview_variables

        get_guild = getattr(self.bot, "get_guild", None)
        guild = get_guild(self.guild_id) if callable(get_guild) else None
        return preview_variables(
            user=f"<@{self.author_id or 0}>",
            server=getattr(guild, "name", "…"),
            user_name=getattr(getattr(guild, "me", None), "display_name", "@voce"),
        )

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
        current.append(block)
        self.adding = False
        self.selected = len(current) - 1
        if values[0] in _COMPOSITE_TYPES:
            self.path.append(self.selected)
            self.selected = None
        await self._persist()
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
                return await interaction.response.send_modal(
                    await self._row_modal(block, index)
                )
            if kind == "divider":
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                block["spacing"] = "large" if block.get("spacing") == "small" else "small"
                await self._persist()
                return await self.rerender(interaction)
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if action == "up" and index > 0:
            blocks[index - 1], blocks[index] = blocks[index], blocks[index - 1]
            self.selected = index - 1
            await self._persist()
        elif action == "down" and index < len(blocks) - 1:
            blocks[index + 1], blocks[index] = blocks[index], blocks[index + 1]
            self.selected = index + 1
            await self._persist()
        elif action == "delete":
            del blocks[index]
            self.selected = None
            await self._persist()
        await self.rerender(interaction)

    def _modal_suffix(self, index: int) -> str:
        path_raw = "-".join(str(step) for step in self.path) or "root"
        return f"{path_raw}:{index}"

    async def _text_modal(self, block: dict[str, Any], index: int):
        return make_text_modal(
            title=(await self._t("cards.editor.modals.text_title"))[:45],
            custom_id=f"card_text_modal:{self._modal_suffix(index)}",
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
            custom_id=f"card_urls_modal:{self._modal_suffix(index)}",
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
            custom_id=f"card_row_modal:{self._modal_suffix(index)}",
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
        await self.rerender(interaction)

    async def _reset(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
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
        self.flash = await self._t("cards.editor.reset_done")
        self.flash_color = "warning"
        await self.rerender(interaction)

    async def _test(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            items = build_items(
                self.bot.theme, self.document(), self._preview_variables(), draft=True
            )
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
        target["color"] = values[0] or None
        await self._persist()
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
        try:
            await self._save_mentions(self.guild_id, policy)
        except (ValueError, OSError):
            return await self.rerender(interaction)
        self.mention_policy = policy
        self.flash = await self._t("cards.editor.mentions.saved")
        self.flash_color = "success"
        await self.rerender(interaction)

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

    def _resolve_path(self, raw: str) -> list[int]:
        if raw == "root":
            return []
        return [int(part) for part in raw.split("-") if part.lstrip("-").isdigit()]

    def _block_by_path(self, path: list[int], index: int) -> dict[str, Any] | None:
        """Locate a block by its modal suffix; ``None`` when stale."""
        node: Any = self.blocks
        try:
            for step in path:
                child = node[step]
                node = child["children"]
            block = node[index]
        except (IndexError, KeyError, TypeError):
            return None
        return block if isinstance(block, dict) else None

    async def _after_modal(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.defer(ephemeral=True)
        await self._persist()
        await self.rerender(interaction)

    def _modal_target(
        self, interaction: discord.Interaction
    ) -> dict[str, Any] | None:
        """Parse a modal ``custom_id`` and resolve the block, tolerating stale UI."""
        try:
            _, path_raw, index_raw = interaction.custom_id.split(":")
            path = self._resolve_path(path_raw)
            index = int(index_raw)
        except (ValueError, AttributeError):
            return None
        self._sanitize_path()
        return self._block_by_path(path, index)

    async def _text_submit(self, interaction: discord.Interaction, value: str) -> None:
        block = self._modal_target(interaction)
        if block is None or block.get("type") != "text":
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return await self.rerender(interaction)
        block["body"] = value.strip("\n")
        await self._after_modal(interaction)

    async def _urls_submit(self, interaction: discord.Interaction, value: str) -> None:
        block = self._modal_target(interaction)
        if block is None or block.get("type") != "gallery":
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return await self.rerender(interaction)
        block["urls"] = [line.strip() for line in value.splitlines() if line.strip()]
        await self._after_modal(interaction)

    async def _row_submit(self, interaction: discord.Interaction, value: str) -> None:
        block = self._modal_target(interaction)
        if block is None or block.get("type") != "row":
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return await self.rerender(interaction)
        buttons = []
        for line in value.splitlines():
            # Split on the LAST pipe so labels may contain "|".
            label, sep, url = line.rpartition("|")
            if not sep:
                label, url = line, ""
            if not label.strip():
                continue
            buttons.append({"label": label.strip()[:80], "url": url.strip()})
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
