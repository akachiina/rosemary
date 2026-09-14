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

import copy
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import discord

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
#: Mention placeholder ``{@name}``: same value as ``{name}``, but records
#: the substituted id so the renderer can build ``AllowedMentions``.
_MENTION_RE = re.compile(r"\{@([a-zA-Z_][a-zA-Z0-9_]*)\}")
_MENTION_TOKEN_RE = re.compile(r"<@!?([0-9]{1,20})>|<@&([0-9]{1,20})>")
_URL_RE = re.compile(r"https?://\S+")
#: Self-contained hex color token ("#rrggbb"), valid anywhere a palette name is.
_HEX_COLOR_RE = re.compile(r"#?[0-9a-fA-F]{6}")


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
    ``mention_default`` is the card's mention policy in
    :mod:`rosemary.core.mentions` (guilds override it per card in /customize).
    ``variables`` is the contract: the placeholder names the send site
    provides (see :mod:`rosemary.core.variables`). The editor preview,
    lint and hint derive from it.
    """

    key: str
    category: str
    rich: bool = False  # rich cards open the full composer; plain ones a text field
    mention_default: str = "none"
    variables: tuple[str, ...] = ()

    @property
    def title_key(self) -> str:
        return f"card.{self.key}.title"

    @property
    def placeholders_key(self) -> str:
        return f"card.{self.key}.placeholders"


_CARDS: dict[str, CardSpec] = {}


def register_cards(*specs: CardSpec) -> None:
    """Add specs to the global registry (idempotent, last write wins)."""
    from rosemary.core.mentions import MODES

    for spec in specs:
        if spec.mention_default not in MODES:
            log.warning(
                "card %s declares unknown mention_default %r; using %r",
                spec.key,
                spec.mention_default,
                "none",
            )
            spec = CardSpec(key=spec.key, category=spec.category, rich=spec.rich)
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


def card_store(bot):
    """Deprecated: per-card persistence moved to theme files.

    Kept so stray imports fail loudly at call time instead of AttributeError
    deep inside a send; returns ``None``.
    """
    return None


async def _override_document(bot, guild_id: int | None, key: str) -> dict[str, Any] | None:
    """The guild's themed override document for ``key`` (``None`` = default).

    Thin re-export of :func:`rosemary.core.themes.card_document` kept here so
    every ``maybe_*`` call site reads the same source of truth.
    """
    from rosemary.core.themes import card_document

    return await card_document(bot, guild_id, key)


async def maybe_view(
    bot, guild_id: int, key: str, variables: dict[str, Any] | None = None
) -> discord.ui.DesignerView | None:
    """Render the guild's themed override for ``key``, or ``None`` for defaults.

    An invalid themed document logs a warning and returns ``None`` so sends
    never break because of theme content. When ``variables`` is omitted the
    echo mapping is used, keeping member placeholders literal instead of
    resolving them to theme emojis.
    """
    doc = await _override_document(bot, guild_id, key)
    if doc is None:
        return None
    from rosemary.core.themes import theme_for

    try:
        items = build_items(
            theme_for(bot, guild_id),
            doc,
            dict(ECHO_VARIABLES) if variables is None else variables,
            card_key=key,
        )
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
    so view-capable call sites can fall through to :func:`maybe_view` and
    keep the rich layout (see :func:`maybe_flat_text` for text-only sites).
    """
    doc = await _override_document(bot, guild_id, key)
    if doc is None:
        return None
    blocks = doc.get("blocks")
    if not isinstance(blocks, list) or not blocks or any(
        not isinstance(block, dict) or block.get("type") != "text" for block in blocks
    ):
        return None
    return await maybe_flat_text(bot, guild_id, key, **variables)


async def maybe_flat_text(bot, guild_id: int, key: str, **variables: Any) -> str | None:
    """Flattened-text override for ``key`` (``None`` when not customized).

    Unlike :func:`maybe_text`, structural documents are flattened to their
    text bodies (link buttons as ``label (url)``), so a rich customization is
    never silently ignored by text-only call sites — members still receive
    the customized copy, minus the layout.
    """
    doc = await _override_document(bot, guild_id, key)
    if doc is None:
        return None
    blocks = doc.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return None
    from rosemary.core.themes import theme_for

    try:
        items = build_items(theme_for(bot, guild_id), doc, dict(variables))
    except CardsError as exc:
        log.warning("card %s for guild %s is invalid, using default: %s", key, guild_id, exc)
        return None
    bodies = [
        item.content.strip()
        for item in _walk_texts(items)
        if getattr(item, "content", "").strip()
    ]
    for item in _walk_texts(items):
        label = getattr(item, "label", "")
        url = getattr(item, "url", "")
        if label and url:
            bodies.append(f"{str(label).strip()} ({str(url).strip()})")
        elif label and not getattr(item, "content", ""):
            bodies.append(str(label).strip())
    return "\n\n".join(bodies) if bodies else None


async def text_or(
    bot, guild_id: int, key: str, translated: str, **variables: Any
) -> str:
    """Return the guild's override for ``key`` or the already-translated copy.

    Structural overrides are flattened to text (see :func:`maybe_flat_text`);
    call sites that can send Components V2 should try :func:`maybe_view`
    first to keep the rich layout.
    """
    override = await maybe_flat_text(bot, guild_id, key, **variables)
    return override if override is not None else translated


async def log_description(bot, guild_id: int, key: str, **variables: Any) -> str:
    """Translated-or-overridden body for one staff-log entry."""
    return await text_or(
        bot, guild_id, key, await bot.translator.t(guild_id, key, **variables), **variables
    )


def _walk_texts(items):
    for item in items:
        yield item
        for child in list(getattr(item, "items", []) or []) + list(
            getattr(item, "children", []) or []
        ):
            yield from _walk_texts([child])


#: Dynamic placeholders that must stay LITERAL when rendering templates
#: without real variables (editor previews, seeded defaults). Theme emoji
#: tokens like ``user`` would otherwise swallow them.
ECHO_VARIABLES: dict[str, str] = {
    name: f"{{{name}}}"
    for name in ("user", "user_name", "server", "count", "user_avatar")
}

#: Sample values for editor preview/compare/test screens, derived from the
#: canonical :mod:`rosemary.core.variables` registry (kept here for compat).
def _preview_samples() -> dict[str, Any]:
    from rosemary.core.variables import VARIABLES

    return {name: spec.sample for name, spec in VARIABLES.items()}


PREVIEW_SAMPLES: dict[str, Any] = _preview_samples()


def preview_variables(*names: str, **extra: Any) -> dict[str, Any]:
    """Mapping for editor previews: every placeholder gets a sample value.

    Unlike :data:`ECHO_VARIABLES` (which keeps templates literal for seeding),
    previews render close to the real send so admins see what members would
    receive. With ``names`` only those variables are sampled (unknowns
    skipped); explicit ``extra`` values (e.g. the guild name) win.
    """
    from rosemary.core.variables import samples_for

    base = samples_for(names) if names else dict(PREVIEW_SAMPLES)
    base.update(extra)
    return base


def spec_variables(spec: CardSpec) -> tuple[str, ...]:
    """Placeholder names a card receives (its contract)."""
    return tuple(spec.variables)


def safe_format(template: str, mapping: dict[str, Any]) -> str:
    """Substitute ``{name}`` placeholders, leaving unknown ones untouched."""
    return _PLACEHOLDER_RE.sub(
        lambda match: str(mapping[match.group(1)]) if match.group(1) in mapping else match.group(0),
        template,
    )


def safe_format_mentions(
    template: str,
    mapping: dict[str, Any],
) -> tuple[str, dict[str, tuple[str, int]]]:
    """Substitute ``{name}`` and ``{@name}``, collecting resolved mentions.

    ``{@name}`` resolves to the *same value* as ``{name}`` — it only marks the
    substitution as a mention, so the renderer knows which ids the customized
    text actually contains. Returns ``(text, mentions)`` where ``mentions``
    maps the placeholder name to ``("user" | "role", id)`` parsed from the
    ``<@id>``/``<@&id>`` token the variable resolved to. An ``{@name}`` whose
    value carries no mention token resolves silently (nothing collected);
    unknown names stay literal.
    """
    resolved = safe_format(template, mapping)
    collected: dict[str, tuple[str, int]] = {}

    def collect(match: re.Match[str]) -> str:
        name = match.group(1)
        value = mapping.get(name)
        if value is not None and name not in collected:
            found = _MENTION_TOKEN_RE.search(str(value))
            if found:
                role_id, user_id = found.group(2), found.group(1)
                collected[name] = ("role" if role_id else "user", int(role_id or user_id))
        return str(value) if value is not None else match.group(0)

    text = _MENTION_RE.sub(collect, resolved)
    return text, collected


def document_mention_ids(
    doc: dict[str, Any], mapping: dict[str, Any]
) -> tuple[list[int], list[int]]:
    """``(user_ids, role_ids)`` referenced by ``{@name}`` fields in ``doc``.

    Walks exactly the fields the renderer substitutes as text (block bodies
    and button/accessory labels) and parses the Discord id each ``{@name}``
    resolves to. Order-preserving de-duplication, so a mention repeated in
    the text pings once. Fields without ``{@...}`` are skipped without any
    regex work — the common case for un-customized cards.
    """
    users: list[int] = []
    roles: list[int] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("body", "label") and isinstance(value, str) and "{@" in value:
                    _text, collected = safe_format_mentions(value, mapping)
                    for kind, found_id in collected.values():
                        (roles if kind == "role" else users).append(found_id)
                else:
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(doc.get("blocks", []))
    if is_embed_document(doc):
        # Embed form: the same fields the renderer fills as text.
        raw = doc.get("embed") or {}
        for path in _EMBED_TEXT_PATHS:
            node = raw
            for part in path:
                node = node.get(part) if isinstance(node, dict) else None
            if isinstance(node, str) and "{@" in node:
                _text, collected = safe_format_mentions(node, mapping)
                for kind, found_id in collected.values():
                    (roles if kind == "role" else users).append(found_id)
        for field in raw.get("fields") or []:
            if isinstance(field, dict):
                for key in ("name", "value"):
                    value = field.get(key)
                    if isinstance(value, str) and "{@" in value:
                        _text, collected = safe_format_mentions(value, mapping)
                        for kind, found_id in collected.values():
                            (roles if kind == "role" else users).append(found_id)
    return list(dict.fromkeys(users)), list(dict.fromkeys(roles))


def new_block_id() -> str:
    """Stable random id for one block (editor selections/modals use it)."""
    import uuid

    return f"b_{uuid.uuid4().hex[:8]}"


def ensure_ids(doc: dict[str, Any]) -> dict[str, Any]:
    """Assign missing block ids in place; returns the document."""

    def walk(blocks: Any) -> None:
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block.setdefault("id", new_block_id())
            walk(block.get("children"))
            for button in block.get("buttons", []) or []:
                if isinstance(button, dict):
                    button.setdefault("id", new_block_id())

    walk(doc.get("blocks"))
    return doc


def find_by_id(blocks: Any, block_id: str) -> dict[str, Any] | None:
    """Locate a block by id anywhere in a block tree."""
    if not isinstance(blocks, list):
        return None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("id") == block_id:
            return block
        for child_key in ("children",):
            found = find_by_id(block.get(child_key), block_id)
            if found is not None:
                return found
        for button in block.get("buttons", []) or []:
            if isinstance(button, dict) and button.get("id") == block_id:
                return button
    return None


def reid_tree(block: dict[str, Any]) -> dict[str, Any]:
    """Give a (duplicated) block and its descendants fresh ids."""
    block["id"] = new_block_id()
    for child in block.get("children", []) or []:
        if isinstance(child, dict):
            reid_tree(child)
    for button in block.get("buttons", []) or []:
        if isinstance(button, dict):
            button["id"] = new_block_id()
    return block


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
    "button_unknown_action": "unknown button action '{action}'",
    "button_url_and_action": "a button cannot have both url and action",
    "button_unknown_style": "unknown button style '{style}'",
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
        # Placeholders like {user_avatar} resolve at send time; accept them
        # (both spellings — {name} and the explicit {@name} mention form).
        if _PLACEHOLDER_RE.search(url) or _MENTION_RE.search(url):
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
        # Raw hex ("#rrggbb" or "rrggbb") is always valid — theme files speak
        # Discord's own format where accent colors are self-contained.
        if _HEX_COLOR_RE.fullmatch(color.strip()):
            return
        if self.theme is not None and color not in getattr(self.theme, "colors", {}):
            self.error("color_unknown", color=color)

    def _check_text(self, block: dict[str, Any], depth: int) -> None:
        self._count_text(block.get("body"))

    def _check_divider(self, block: dict[str, Any], depth: int) -> None:
        spacing = block.get("spacing", "small")
        if spacing not in ("small", "large"):
            self.error("divider_spacing")
        if "visible" in block and not isinstance(block.get("visible"), bool):
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
            self._check_button(button)

    def _check_button(self, button: Any) -> None:
        from rosemary.core.card_actions import ACTIONS, INTERACTIVE_STYLES

        if not isinstance(button, dict):
            self.error("button_label", max=LABEL_MAX)
            return
        label = button.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > LABEL_MAX:
            self.error("button_label", max=LABEL_MAX)
        if button.get("action") is not None:
            if button.get("action") not in ACTIONS:
                self.error("button_unknown_action", action=button.get("action"))
            if button.get("url"):
                self.error("button_url_and_action")
            if button.get("style") is not None and button.get("style") not in INTERACTIVE_STYLES:
                self.error("button_unknown_style", style=button.get("style"))
        else:
            self._check_url(button.get("url"))

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
    card_key: str | None = None,
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
    doc = resolve_mention_fields(doc, mapping)
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
    return [_build_block(block, theme, mapping, card_key) for block in blocks]


def _build_block(
    block: dict[str, Any],
    theme: Any,
    mapping: dict[str, Any],
    card_key: str | None = None,
) -> discord.ui.ViewItem:
    builder = {
        "text": _build_text,
        "divider": _build_divider,
        "gallery": _build_gallery,
        "row": _build_row,
        "section": _build_section,
        "container": _build_container,
    }[block["type"]]
    # Only row/container builders take card_key (needed to scope action
    # button custom_ids); nested rows inherit it through the container.
    if block["type"] in ("row", "container"):
        return builder(block, theme, mapping, card_key)
    return builder(block, theme, mapping)


def _fill(body: Any, mapping: dict[str, Any]) -> str:
    return safe_format(str(body), mapping)


def _has_mention_fields(node: Any) -> bool:
    """Whether any text field in the block tree contains an ``{@name}``."""
    if isinstance(node, str):
        return "{@" in node
    if isinstance(node, dict):
        return any(_has_mention_fields(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_mention_fields(item) for item in node)
    return False


def resolve_mention_fields(
    doc: dict[str, Any], mapping: dict[str, Any]
) -> dict[str, Any]:
    """Resolve ``{@name}`` fields to their values, returning the document.

    Returns the original object untouched when no ``{@...}`` field exists
    (the common case — zero copying); otherwise a deep copy with every
    ``{@name}`` replaced by the same value ``{name}`` would resolve to.
    Mention tokens in URLs are meaningless, but resolving them keeps the
    document renderer single-pass.
    """
    if not _has_mention_fields(doc.get("blocks", [])):
        return doc
    doc = copy.deepcopy(doc)

    def fill(value: Any) -> Any:
        if isinstance(value, str) and "{@" in value:
            text, _ids = safe_format_mentions(value, mapping)
            return text
        if isinstance(value, dict):
            return {key: fill(item) for key, item in value.items()}
        if isinstance(value, list):
            return [fill(item) for item in value]
        return value

    doc["blocks"] = fill(doc.get("blocks", []))
    return doc


def _build_text(block: dict[str, Any], theme: Any, mapping: dict[str, Any]) -> TextDisplay:
    return TextDisplay(_fill(block.get("body", ""), mapping))


def _build_divider(block: dict[str, Any], theme: Any, mapping: dict[str, Any]) -> Separator:
    size = discord.SeparatorSpacingSize.large if block.get("spacing") == "large" else (
        discord.SeparatorSpacingSize.small
    )
    return Separator(spacing=size, divider=block.get("visible", True) is not False)


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


def _build_action_button(
    button: dict[str, Any], mapping: dict[str, Any], card_key: str | None
) -> discord.ui.Button:
    from rosemary.core.card_actions import button_style, custom_id_for

    return discord.ui.Button(
        style=button_style(button.get("style")),
        label=_fill(button.get("label", ""), mapping)[:LABEL_MAX] or "•",
        custom_id=custom_id_for(card_key or "", str(button.get("id", ""))),
        emoji=_fill(button.get("emoji", ""), mapping) or None,
    )


def _build_row(
    block: dict[str, Any], theme: Any, mapping: dict[str, Any], card_key: str | None = None
) -> ActionRow:
    buttons = []
    for button in block.get("buttons", []):
        if not isinstance(button, dict):
            continue
        if button.get("action") is not None:
            buttons.append(_build_action_button(button, mapping, card_key))
        else:
            buttons.append(
                _build_link_button(button.get("label", ""), button.get("url", ""), mapping)
            )
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


# -- embed documents ---------------------------------------------------------

#: Placeholder-bearing string fields of an embed doc, for mention walking and
#: length checks: (path into doc["embed"], kind).
_EMBED_TEXT_PATHS = (
    ("title",),
    ("description",),
    ("footer", "text"),
    ("author", "name"),
)


def is_embed_document(doc: dict[str, Any]) -> bool:
    """Whether ``doc`` is an embed-form card (``kind: embed``)."""
    return isinstance(doc, dict) and doc.get("kind") == "embed"


def resolve_embed(
    doc: dict[str, Any], theme: Any, mapping: dict[str, Any]
) -> tuple[discord.Embed, list[dict[str, Any]]]:
    """Resolve one embed document: ``(discord.Embed, buttons)``.

    Placeholder resolution uses the same ``safe_format`` as V2 — theme emojis
    as defaults, caller variables win, unknown names stay visible. ``{@name}``
    mention fields resolve like V2 too (see :func:`resolve_mention_fields`)
    so pings-as-content keep working inside embeds.
    """
    from rosemary.core.embed_convert import (
        DESCRIPTION_MAX,
        FIELD_VALUE_MAX,
        TITLE_MAX,
        TOTAL_MAX,
    )

    raw = doc.get("embed") or {}
    resolved = resolve_mention_fields({"blocks": [raw]}, mapping)["blocks"][0]

    def fill(value: Any) -> str:
        return safe_format(str(value), mapping) if isinstance(value, str) else ""

    embed = discord.Embed()
    if resolved.get("title"):
        embed.title = fill(resolved["title"])[:TITLE_MAX]
    if resolved.get("description"):
        embed.description = fill(resolved["description"])[:DESCRIPTION_MAX]
    if resolved.get("url"):
        embed.url = fill(resolved["url"])
    color_token = resolved.get("color")
    if isinstance(color_token, str) and color_token.strip():
        token = color_token.strip()
        if _HEX_COLOR_RE.fullmatch(token):
            embed.colour = discord.Colour(int(token.lstrip("#"), 16))
        else:
            try:
                embed.colour = theme.color(token)
            except KeyError as exc:
                raise CardsError(
                    [CardIssue("color_unknown", (("color", token),))]
                ) from exc
    footer = resolved.get("footer") or {}
    if isinstance(footer, dict) and (footer.get("text") or footer.get("icon_url")):
        embed.set_footer(
            text=fill(footer.get("text"))[:2048] or None,
            icon_url=fill(footer.get("icon_url")) or None,
        )
    author = resolved.get("author") or {}
    if isinstance(author, dict) and (author.get("name") or author.get("icon_url")):
        embed.set_author(
            name=fill(author.get("name"))[:256] or "\u200b",
            icon_url=fill(author.get("icon_url")) or None,
        )
    if resolved.get("timestamp"):
        from datetime import UTC, datetime

        embed.timestamp = datetime.fromtimestamp(int(resolved["timestamp"]), tz=UTC)
    image = (resolved.get("image") or {}).get("url")
    if image:
        embed.set_image(url=fill(image))
    thumbnail = (resolved.get("thumbnail") or {}).get("url")
    if thumbnail:
        embed.set_thumbnail(url=fill(thumbnail))
    for field in resolved.get("fields") or []:
        if not isinstance(field, dict):
            continue
        embed.add_field(
            name=fill(field.get("name"))[:256] or "\u200b",
            value=fill(field.get("value"))[:FIELD_VALUE_MAX] or "\u200b",
            inline=bool(field.get("inline")),
        )
    total = len(embed.title or "") + len(embed.description or "")
    total += sum(len(f.name) + len(f.value) for f in embed.fields)
    total += len(embed.footer.text or "") if embed.footer else 0
    total += len(embed.author.name or "") if embed.author else 0
    if total > TOTAL_MAX:
        raise CardsError(
            [
                CardIssue(
                    "embed_too_long", (("field", "total"), ("chars", total), ("max", TOTAL_MAX))
                )
            ]
        )
    return embed, list(doc.get("buttons") or [])


def _build_embed_button(
    button: dict[str, Any], mapping: dict[str, Any], card_key: str | None
) -> discord.ui.Button:
    """One classic-row button for an embed card (link or action)."""
    from rosemary.core.card_actions import custom_id_for

    if button.get("action") is not None:
        return discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=_fill(button.get("label", ""), mapping)[:LABEL_MAX] or "\u2022",
            custom_id=custom_id_for(card_key or "", str(button.get("id", ""))),
            emoji=_fill(button.get("emoji", ""), mapping) or None,
        )
    return discord.ui.Button(
        style=discord.ButtonStyle.link,
        label=_fill(button.get("label", ""), mapping)[:LABEL_MAX] or "\u2022",
        url=_fill(button.get("url", ""), mapping),
    )


def build_embed_view(
    buttons: list[dict[str, Any]],
    mapping: dict[str, Any],
    card_key: str | None,
    *,
    owner_id: int | None = None,
) -> discord.ui.View | None:
    """Classic ActionRow view carrying an embed card's buttons (or ``None``)."""
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)
    for button in buttons:
        if isinstance(button, dict):
            view.add_item(_build_embed_button(button, mapping, card_key))
    from rosemary.core.card_actions import bind_action_callbacks

    bind_action_callbacks(view.children)
    return view


def _build_container(
    block: dict[str, Any], theme: Any, mapping: dict[str, Any], card_key: str | None = None
) -> Container:
    color = None
    token = block.get("color")
    if isinstance(token, str):
        if _HEX_COLOR_RE.fullmatch(token.strip()):
            color = discord.Colour(int(token.strip().lstrip("#"), 16))
        else:
            try:
                color = theme.color(token)
            except KeyError as exc:
                raise CardsError([CardIssue("color_unknown", (("color", token),))]) from exc
    children = [
        _build_block(child, theme, mapping, card_key) for child in block.get("children", [])
    ]
    if color is not None:
        return Container(*children, color=color)
    return Container(*children)


# Per-card persistence moved to theme files (core.themes.ThemeStore); the
# legacy cards.json store was removed along with the /customize editor.

