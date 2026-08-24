"""Composable Components V2 message documents (the /customize engine).

A customizable message is a *document*: an ordered list of blocks that maps
directly onto Discord's Components V2 layout rules. Guilds edit documents
through the card editor; features only declare which messages are editable.

Document schema (``v`` 1)::

    {"v": 1, "blocks": [
      {"type": "container", "color": "brand", "children": [
        {"type": "section",
         "accessory": {"type": "thumbnail", "url": "{user_avatar}"},
         "children": [{"type": "text", "body": "# Bem-vindo, {user}!"}]},
        {"type": "divider"},
        {"type": "text", "body": "Você é o membro #{count} do {server}"},
        {"type": "gallery", "urls": ["https://..."]},
        {"type": "row", "buttons": [{"label": "Site", "url": "https://..."}]}
      ]}
    ]}

Block types: ``text``, ``divider``, ``gallery``, ``row`` and the two
composites ``container``/``section``. Every text field supports placeholders:
known names resolve from the caller-supplied variables plus theme emojis,
unknown ones stay visible so admins can spot typos.

Colors are *theme token names* (see ``ui/theme.yaml``), never raw hex — the
visual identity stays in one place. Rendering validates against Discord's V2
limits first; an invalid stored document falls back to the feature default at
the call site instead of breaking sends.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import discord

from rosemary.core.storage import GuildStorage
from rosemary.ui.containers import ActionRow, Container, Section, Separator, TextDisplay

DOCUMENT_VERSION = 1

MAX_COMPONENTS = 40  # Discord hard limit per message, nested items included.
MAX_DEPTH = 5  # Root blocks are depth 0; container children are depth 1.
MAX_TEXT_CHARS = 4000  # Sum across every TextDisplay in the message.
SECTION_CHILD_MIN, SECTION_CHILD_MAX = 1, 3
BUTTON_MIN, BUTTON_MAX = 1, 5
GALLERY_MIN, GALLERY_MAX = 1, 10
LABEL_MAX = 80
URL_MAX = 512

_BLOCK_TYPES = frozenset({"text", "divider", "gallery", "row", "section", "container"})
_CONTAINER_CHILDREN = frozenset({"text", "divider", "gallery", "row", "section"})
_SECTION_ACCESSORIES = frozenset({"thumbnail", "button"})

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_URL_RE = re.compile(r"https?://\S+")


class CardsError(ValueError):
    """Raised when a document cannot be rendered; carries structured issues."""

    def __init__(self, issues: list[CardIssue]) -> None:
        self.issues = issues
        super().__init__(describe_issues(issues))


log = logging.getLogger(__name__)


# -- registry ----------------------------------------------------------------


@dataclass(frozen=True)
class CardSpec:
    """Declares one customizable message.

    ``key`` is both the storage key and the i18n prefix: catalogs provide the
    default copy through ``card.<key>.title`` / ``card.<key>.placeholders``.
    """

    key: str
    category: str
    rich: bool = False  # rich cards open the full composer; plain ones a text field

    @property
    def title_key(self) -> str:
        return f"card.{self.key}.title"

    @property
    def placeholders_key(self) -> str:
        return f"card.{self.key}.placeholders"


_CARDS: dict[str, CardSpec] = {}


def register_cards(*specs: CardSpec) -> None:
    """Add specs to the global registry (idempotent, last write wins)."""
    for spec in specs:
        _CARDS[spec.key] = spec


def all_cards() -> list[CardSpec]:
    return list(_CARDS.values())


def get_card(key: str) -> CardSpec | None:
    return _CARDS.get(key)


def cards_for_category(category: str) -> list[CardSpec]:
    return [spec for spec in _CARDS.values() if spec.category == category]


# -- default builders --------------------------------------------------------

#: Renders what members receive when a card has no override. Registered by
#: features; powers the editor's "compare with default" screen. Signature:
#: ``async fn(bot, guild_id, variables) -> list[ViewItem]``.
_DEFAULT_BUILDERS: dict[str, Callable[..., Any]] = {}


def set_default_builder(key: str, builder: Callable[..., Any]) -> None:
    _DEFAULT_BUILDERS[key] = builder


def get_default_builder(key: str) -> Callable[..., Any] | None:
    return _DEFAULT_BUILDERS.get(key)


# -- resolution --------------------------------------------------------------


def card_store(bot) -> CardStore:
    """Default store bound to the bot's data directory."""
    return CardStore(bot.storage.data_dir)


async def maybe_view(
    bot, guild_id: int, key: str, variables: dict[str, Any] | None = None
) -> discord.ui.DesignerView | None:
    """Render the guild's override for ``key``, or ``None`` to use defaults.

    An invalid stored document logs a warning and returns ``None`` so sends
    never break because of editor content.
    """
    doc = await card_store(bot).get_document(guild_id, key)
    if doc is None:
        return None
    try:
        items = build_items(bot.theme, doc, variables)
    except CardsError as exc:
        log.warning("card %s for guild %s is invalid, using default: %s", key, guild_id, exc)
        return None
    view = discord.ui.DesignerView(store=False)
    for item in items:
        view.add_item(item)
    return view


async def maybe_text(bot, guild_id: int, key: str, **variables: Any) -> str | None:
    """Plain-text override for documents made only of top-level text blocks.

    Structural documents (containers, sections, galleries...) yield ``None``
    so callers fall through to :func:`maybe_view` / their default builder.
    """
    doc = await card_store(bot).get_document(guild_id, key)
    if doc is None:
        return None
    blocks = doc.get("blocks")
    if not isinstance(blocks, list) or not blocks or any(
        not isinstance(block, dict) or block.get("type") != "text" for block in blocks
    ):
        return None
    try:
        items = build_items(bot.theme, doc, dict(variables))
    except CardsError as exc:
        log.warning("card %s for guild %s is invalid, using default: %s", key, guild_id, exc)
        return None
    bodies = [
        item.content.strip()
        for item in _walk_texts(items)
        if getattr(item, "content", "").strip()
    ]
    return "\n\n".join(bodies) if bodies else None


async def text_or(
    bot, guild_id: int, key: str, translated: str, **variables: Any
) -> str:
    """Return the guild's override for ``key`` or the already-translated copy."""
    override = await maybe_text(bot, guild_id, key, **variables)
    return override if override is not None else translated


async def log_description(bot, guild_id: int, key: str, **variables: Any) -> str:
    """Translated-or-overridden body for one staff-log entry."""
    return await text_or(
        bot, guild_id, key, await bot.translator.t(guild_id, key, **variables), **variables
    )


def _walk_texts(items):
    for item in items:
        yield item
        for child in getattr(item, "items", []) or []:
            yield from _walk_texts([child])


#: Dynamic placeholders that must stay LITERAL when rendering templates
#: without real variables (editor previews, seeded defaults). Theme emoji
#: tokens like ``user`` would otherwise swallow them.
ECHO_VARIABLES: dict[str, str] = {
    name: f"{{{name}}}"
    for name in ("user", "user_name", "server", "count", "user_avatar")
}


def safe_format(template: str, mapping: dict[str, Any]) -> str:
    """Substitute ``{name}`` placeholders, leaving unknown ones untouched."""
    return _PLACEHOLDER_RE.sub(
        lambda match: str(mapping[match.group(1)]) if match.group(1) in mapping else match.group(0),
        template,
    )


# -- validation -------------------------------------------------------------

#: Stable error codes; catalogs translate them under ``cards.errors.<code>``.


@dataclass(frozen=True)
class CardIssue:
    """One validation problem: a stable code plus display parameters."""

    code: str
    params: tuple[tuple[str, Any], ...] = ()

    @property
    def kwargs(self) -> dict[str, Any]:
        return dict(self.params)


_ISSUE_EN: dict[str, str] = {
    "document_shape": "document must be an object with a 'blocks' list",
    "unknown_block": "block must be an object with a known 'type'",
    "depth_exceeded": "exceeds max nesting depth ({max})",
    "text_not_string": "'body' must be a non-empty string",
    "text_empty": "'body' must be a non-empty string",
    "divider_spacing": "'spacing' must be 'small' or 'large'",
    "gallery_count": "needs {min}-{max} urls",
    "bad_url": "needs an https:// (or http://) url",
    "row_count": "needs {min}-{max} buttons",
    "button_label": "'label' is required (max {max})",
    "section_children_count": "needs {min}-{max} text children",
    "section_children_text": "children must be 'text' blocks",
    "section_accessory_missing": "requires an accessory of type 'thumbnail' or 'button'",
    "accessory_button_label": "'label' is required",
    "container_empty": "needs at least one child block",
    "container_nested": "cannot nest containers",
    "container_child_invalid": "invalid child type",
    "color_unknown": "unknown theme color '{color}'",
    "too_many_components": "too many components ({count} > {max})",
    "text_too_long": "text too long ({chars} > {max} chars)",
}


def describe_issues(issues: list[CardIssue]) -> str:
    """English one-line rendering, meant for terminal logs only."""
    parts = []
    for issue in issues:
        template = _ISSUE_EN.get(issue.code, issue.code)
        parts.append(template.format(**issue.kwargs))
    return "; ".join(parts)


def validate_document(
    doc: Any,
    theme: Any | None = None,
    *,
    variables: dict[str, Any] | None = None,
    draft: bool = False,
) -> list[CardIssue]:
    """Return the list of problems with a document; empty means renderable.

    ``theme`` enables color-token checks. When ``variables`` is given, text
    lengths are measured after placeholder substitution (the real send-time
    size); otherwise the raw templates are measured. ``draft=True`` tolerates
    empty text bodies (editor skeletons) instead of flagging them.
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("blocks"), list):
        return [CardIssue("document_shape")]
    state = _ValidationState(theme=theme, variables=variables or {}, draft=draft)
    for block in doc["blocks"]:
        state.check_block(block, depth=0)
    if state.components > MAX_COMPONENTS:
        state.error(
            "too_many_components", count=state.components, max=MAX_COMPONENTS
        )
    if state.text_chars > MAX_TEXT_CHARS:
        state.error(
            "text_too_long", chars=state.text_chars, max=MAX_TEXT_CHARS
        )
    return state.errors


class _ValidationState:
    """Accumulates rule violations while walking a block tree."""

    def __init__(
        self, theme: Any | None, variables: dict[str, Any], draft: bool = False
    ) -> None:
        self.theme = theme
        self.variables = variables
        self.draft = draft
        self.errors: list[CardIssue] = []
        self.components = 0
        self.text_chars = 0

    def error(self, code: str, **params: Any) -> None:
        self.errors.append(CardIssue(code, tuple(params.items())))

    def check_block(self, block: Any, depth: int) -> None:
        if not isinstance(block, dict) or block.get("type") not in _BLOCK_TYPES:
            self.error("unknown_block")
            return
        kind = block["type"]
        self.components += 1
        if depth > MAX_DEPTH:
            self.error("depth_exceeded", max=MAX_DEPTH)
            return
        getattr(self, f"_check_{kind}", lambda *_: None)(block, depth)

    def _count_text(self, body: Any) -> bool:
        if not isinstance(body, str):
            self.error("text_not_string")
            return False
        resolved = safe_format(body, self.variables)
        if self.draft and not resolved.strip():
            return False
        if not body.strip():
            self.error("text_empty")
            return False
        self.text_chars += len(resolved)
        return True

    def _check_url(self, url: Any) -> bool:
        if not isinstance(url, str):
            self.error("bad_url")
            return False
        # Placeholders like {user_avatar} resolve at send time; accept them.
        if _PLACEHOLDER_RE.search(url):
            return len(url) <= URL_MAX * 2
        if not _URL_RE.match(url) or len(url) > URL_MAX:
            self.error("bad_url")
            return False
        return True

    def _check_color(self, color: Any) -> None:
        if color is None:
            return
        if not isinstance(color, str):
            self.error("color_unknown", color=color)
            return
        if self.theme is not None and color not in getattr(self.theme, "colors", {}):
            self.error("color_unknown", color=color)

    def _check_text(self, block: dict[str, Any], depth: int) -> None:
        self._count_text(block.get("body"))

    def _check_divider(self, block: dict[str, Any], depth: int) -> None:
        spacing = block.get("spacing", "small")
        if spacing not in ("small", "large"):
            self.error("divider_spacing")

    def _check_gallery(self, block: dict[str, Any], depth: int) -> None:
        urls = block.get("urls")
        if not isinstance(urls, list) or not (GALLERY_MIN <= len(urls) <= GALLERY_MAX):
            self.error("gallery_count", min=GALLERY_MIN, max=GALLERY_MAX)
            return
        self.components += len(urls) - 1
        for url in urls:
            self._check_url(url)

    def _check_row(self, block: dict[str, Any], depth: int) -> None:
        buttons = block.get("buttons")
        if not isinstance(buttons, list) or not (BUTTON_MIN <= len(buttons) <= BUTTON_MAX):
            self.error("row_count", min=BUTTON_MIN, max=BUTTON_MAX)
            return
        self.components += len(buttons) - 1
        for button in buttons:
            label = button.get("label") if isinstance(button, dict) else None
            if not isinstance(label, str) or not label.strip() or len(label) > LABEL_MAX:
                self.error("button_label", max=LABEL_MAX)
            self._check_url(button.get("url") if isinstance(button, dict) else None)

    def _check_section(self, block: dict[str, Any], depth: int) -> None:
        children = block.get("children")
        ok_children = isinstance(children, list) and (
            SECTION_CHILD_MIN <= len(children) <= SECTION_CHILD_MAX
        )
        if not ok_children:
            self.error(
                "section_children_count", min=SECTION_CHILD_MIN, max=SECTION_CHILD_MAX
            )
            children = []
        for child in children:
            if isinstance(child, dict) and child.get("type") != "text":
                self.error("section_children_text")
                continue
            self.check_block(child, depth + 1)
        accessory = block.get("accessory")
        if not isinstance(accessory, dict) or accessory.get("type") not in _SECTION_ACCESSORIES:
            self.error("section_accessory_missing")
            return
        if accessory.get("type") == "thumbnail":
            self._check_url(accessory.get("url"))
        else:
            label = accessory.get("label")
            if not isinstance(label, str) or not label.strip():
                self.error("accessory_button_label")
            self._check_url(accessory.get("url"))

    def _check_container(self, block: dict[str, Any], depth: int) -> None:
        self._check_color(block.get("color"))
        children = block.get("children")
        if not isinstance(children, list) or not children:
            self.error("container_empty")
            return
        for child in children:
            if isinstance(child, dict):
                if child.get("type") == "container":
                    self.error("container_nested")
                    continue
                if child.get("type") not in _CONTAINER_CHILDREN:
                    self.error("container_child_invalid")
                    continue
            self.check_block(child, depth + 1)


# -- rendering --------------------------------------------------------------


def build_items(
    theme: Any,
    doc: dict[str, Any],
    variables: dict[str, Any] | None = None,
    *,
    draft: bool = False,
) -> list[discord.ui.ViewItem]:
    """Render a validated document into top-level V2 items for a DesignerView.

    Raises :class:`CardsError` (carrying structured ``issues``) when the
    document breaks Discord's layout rules; callers should treat that as "fall
    back to the default message". ``draft=True`` skips empty text skeletons
    instead of failing, for live editor previews.
    """
    errors = validate_document(doc, theme=theme, variables=variables, draft=draft)
    if errors:
        raise CardsError(errors)
    # Theme emojis are defaults; caller variables win on name clashes.
    mapping: dict[str, Any] = {**(getattr(theme, "emojis", {}) or {}), **(variables or {})}
    blocks = [
        block
        for block in doc.get("blocks", [])
        if not (
            draft
            and isinstance(block, dict)
            and block.get("type") == "text"
            and not safe_format(str(block.get("body", "")), mapping).strip()
        )
    ]
    return [_build_block(block, theme, mapping) for block in blocks]


def _build_block(block: dict[str, Any], theme: Any, mapping: dict[str, Any]) -> discord.ui.ViewItem:
    builder = {
        "text": _build_text,
        "divider": _build_divider,
        "gallery": _build_gallery,
        "row": _build_row,
        "section": _build_section,
        "container": _build_container,
    }[block["type"]]
    return builder(block, theme, mapping)


def _fill(body: Any, mapping: dict[str, Any]) -> str:
    return safe_format(str(body), mapping)


def _build_text(block: dict[str, Any], theme: Any, mapping: dict[str, Any]) -> TextDisplay:
    return TextDisplay(_fill(block.get("body", ""), mapping))


def _build_divider(block: dict[str, Any], theme: Any, mapping: dict[str, Any]) -> Separator:
    size = discord.SeparatorSpacingSize.large if block.get("spacing") == "large" else (
        discord.SeparatorSpacingSize.small
    )
    return Separator(spacing=size)


def _build_gallery(
    block: dict[str, Any], theme: Any, mapping: dict[str, Any]
) -> discord.ui.MediaGallery:
    items = [discord.MediaGalleryItem(_fill(url, mapping)) for url in block.get("urls", [])]
    return discord.ui.MediaGallery(*items)


def _build_link_button(label: str, url: str, mapping: dict[str, Any]) -> discord.ui.Button:
    return discord.ui.Button(
        style=discord.ButtonStyle.link,
        label=_fill(label, mapping)[:LABEL_MAX],
        url=_fill(url, mapping),
    )


def _build_row(block: dict[str, Any], theme: Any, mapping: dict[str, Any]) -> ActionRow:
    buttons = [
        _build_link_button(button.get("label", ""), button.get("url", ""), mapping)
        for button in block.get("buttons", [])
        if isinstance(button, dict)
    ]
    return ActionRow(*buttons)


def _build_section(block: dict[str, Any], theme: Any, mapping: dict[str, Any]) -> Section:
    children = [
        _build_text(child, theme, mapping)
        for child in block.get("children", [])
        if isinstance(child, dict) and child.get("type") == "text"
    ]
    accessory = block.get("accessory") or {}
    if accessory.get("type") == "button":
        item = _build_link_button(
            accessory.get("label", ""), accessory.get("url", ""), mapping
        )
    else:
        item = discord.ui.Thumbnail(_fill(accessory.get("url", ""), mapping))
    return Section(*children, accessory=item)


def _build_container(block: dict[str, Any], theme: Any, mapping: dict[str, Any]) -> Container:
    color = None
    token = block.get("color")
    if isinstance(token, str):
        try:
            color = theme.color(token)
        except KeyError as exc:  # pragma: no cover - validate_document guards this
            raise CardsError(f"unknown theme color {token!r}") from exc
    children = [_build_block(child, theme, mapping) for child in block.get("children", [])]
    if color is not None:
        return Container(*children, color=color)
    return Container(*children)


# -- persistence ------------------------------------------------------------


class CardStore:
    """Per-guild storage of customized card documents.

    Uses its own ``cards.json`` file (via :class:`GuildStorage`), so overrides
    never mix with settings or feature data. A missing key means "not
    customized" — resolution then falls back to the feature default.
    """

    def __init__(self, data_dir) -> None:
        self._storage = GuildStorage(data_dir, filename="cards.json", use_defaults=False)

    async def get_document(self, guild_id: int, key: str) -> dict[str, Any] | None:
        """Return the guild's saved document for ``key`` or ``None``."""
        data = await self._storage.get(guild_id)
        doc = data.get(key)
        return doc if isinstance(doc, dict) else None

    async def save_document(self, guild_id: int, key: str, doc: dict[str, Any]) -> None:
        """Persist one card override atomically."""
        await self._storage.set(guild_id, key, doc)

    async def reset(self, guild_id: int, key: str) -> None:
        """Remove an override, restoring catalog defaults."""
        await self._storage.delete_keys(guild_id, key)

    async def customized_keys(self, guild_id: int) -> set[str]:
        """Keys this guild has overridden (for editor badges)."""
        data = await self._storage.get(guild_id)
        return {key for key, value in data.items() if isinstance(value, dict)}
