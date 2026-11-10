"""Theme tools: what a theme does, before the guild picks it.

Three questions the ``/themes`` panel answers about a theme:

* **validate**: does the theme load, and does its content make sense? The
  cards it names exist, the copy it points at exists in the catalog, the
  colors it uses exist, and the variables it fills are the ones the send
  site provides.
* **preview**: how does a card of the theme look in each shape (the plain
  text, the classic embed, the Components V2)?
* **diff**: what does the theme change against the built-in one?

The module is pure: it reads and compares, it never sends. The panel
formats the result (and the copy lives in the catalogs).
"""

from __future__ import annotations

import contextlib
import dataclasses
from typing import Any

from rosemary.core.documents.merge import DocumentError, resolve_layers
from rosemary.core.documents.templates import TOKEN_RE
from rosemary.core.theme_package import default_package

#: The shapes a card may ship in (the preview offers each one).
FORMS = ("text", "embed", "card")


@dataclasses.dataclass(frozen=True)
class ThemeIssue:
    """One problem of a theme: the code, where it is and what it says."""

    code: str
    where: str
    detail: str


@dataclasses.dataclass(frozen=True)
class ThemeDiff:
    """What a theme changes against the built-in one."""

    extends: str | None = None
    colors: dict[str, tuple[str, str]] = dataclasses.field(default_factory=dict)
    emojis: dict[str, tuple[str, str]] = dataclasses.field(default_factory=dict)
    styles: dict[str, tuple[str, str]] = dataclasses.field(default_factory=dict)
    cards: tuple[str, ...] = ()
    deltas: tuple[str, ...] = ()
    pings: dict[str, bool] = dataclasses.field(default_factory=dict)

    @property
    def empty(self) -> bool:
        """Whether the theme changes nothing (it is the built-in one)."""
        return not (
            self.colors
            or self.emojis
            or self.styles
            or self.cards
            or self.deltas
            or self.pings
        )


def diff_theme(theme: Any, base: Any) -> ThemeDiff:
    """What ``theme`` changes against ``base`` (the built-in one).

    The tokens are compared leaf by leaf (the styles too), so the diff says
    exactly which values moved. The cards are listed by key: a **legacy
    full card** replaces the card, a **delta** customizes it.
    """
    colors = _changed(base.colors, theme.colors)
    emojis = _changed(base.emojis, theme.emojis)
    styles = _changed_styles(base.styles, theme.styles)
    cards = tuple(sorted(getattr(theme, "cards", {}) or {}))
    deltas = tuple(sorted(getattr(theme, "deltas", {}) or {}))
    pings = {
        key: bool(value)
        for key, value in (getattr(theme, "pings", {}) or {}).items()
        if bool(value) != bool((getattr(base, "pings", {}) or {}).get(key))
    }
    return ThemeDiff(
        extends=getattr(theme, "extends", None),
        colors=colors,
        emojis=emojis,
        styles=styles,
        cards=cards,
        deltas=deltas,
        pings=pings,
    )


def _changed(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """The tokens whose value differs (the base value, the new one)."""
    out: dict[str, tuple[str, str]] = {}
    for key, value in (overlay or {}).items():
        before = (base or {}).get(key)
        if before != value:
            out[key] = (str(before or ""), str(value))
    return out


def _changed_styles(
    base: dict[str, dict[str, str]], overlay: dict[str, dict[str, str]]
) -> dict[str, tuple[str, str]]:
    """The style fields that differ (``style.field`` -> the two values)."""
    out: dict[str, tuple[str, str]] = {}
    for name, fields in (overlay or {}).items():
        before = (base or {}).get(name) or {}
        for field, value in (fields or {}).items():
            if before.get(field) != value:
                out[f"{name}.{field}"] = (str(before.get(field, "")), str(value))
    return out


async def validate_theme(bot, guild_id: int, theme: Any) -> list[ThemeIssue]:
    """The problems of a theme (an empty list means the theme is sound).

    The loader already refuses a theme that does not parse; this checks the
    *meaning*: a card that does not exist, a copy that the catalog does not
    have, a color token nobody defines and a variable the send site never
    provides are all silent holes in the UI, and this is where they surface.
    """
    issues: list[ThemeIssue] = []
    deltas = getattr(theme, "deltas", {}) or {}
    cards = getattr(theme, "cards", {}) or {}
    for key in sorted(set(deltas) | set(cards)):
        if not _known_card(key):
            issues.append(
                ThemeIssue("theme_unknown_card", key, f"{key}: no card with this key")
            )
    for key, doc in sorted(deltas.items()):
        issues.extend(await _validate_delta(bot, guild_id, theme, key, doc))
    for key in sorted(getattr(theme, "pings", {}) or {}):
        if not _known_card(key):
            issues.append(
                ThemeIssue("theme_unknown_card", key, f"{key}: no card with this key")
            )
    return issues


async def _validate_delta(
    bot, guild_id: int, theme: Any, key: str, doc: dict[str, Any]
) -> list[ThemeIssue]:
    """One delta checked: the frames, the color, the copy and the variables."""
    issues: list[ThemeIssue] = []
    archetypes = getattr(theme, "archetypes", {}) or {}
    fragments = getattr(theme, "fragments", {}) or {}
    try:
        resolved = resolve_layers(doc, archetypes, fragments)
    except DocumentError as exc:
        return [ThemeIssue(exc.code, key, str(exc))]
    color = resolved.get("color")
    for token in color if isinstance(color, list) else [color]:
        if not isinstance(token, str):
            continue
        value = token.strip()
        if not value or "{" in value:
            continue
        if value.startswith("#") or value in (getattr(theme, "colors", {}) or {}):
            continue
        issues.append(
            ThemeIssue("theme_unknown_color", key, f"{value}: no color token")
        )
    contract = _contract(key)
    emojis = getattr(theme, "emojis", {}) or {}
    for text in _texts(resolved):
        for match in TOKEN_RE.finditer(text):
            dotted = match.group("dotted")
            if dotted is not None:
                found = await _catalog_copy(bot, guild_id, dotted)
                if not found:
                    issues.append(
                        ThemeIssue(
                            "theme_unknown_copy",
                            key,
                            f"{dotted}: the catalog has no copy for it",
                        )
                    )
                continue
            name = (match.group("plain") or match.group("mention") or "").lstrip("@")
            if not name or name in contract or name in emojis:
                continue
            issues.append(
                ThemeIssue(
                    "theme_unknown_variable",
                    key,
                    f"{name}: the send site does not provide it",
                )
            )
    return issues


async def _catalog_copy(bot, guild_id: int, key: str) -> str:
    """The copy of a catalog key (``""`` when the catalog has none)."""
    raw = getattr(getattr(bot, "translator", None), "raw", None)
    if not callable(raw):
        return ""
    found = await raw(guild_id, key)
    if not isinstance(found, str) or not found.strip() or found == key:
        return ""
    return found


def _known_card(key: str) -> bool:
    """Whether a card key exists (the package or the code-built registry)."""
    from rosemary.core.cards import get_card

    if key in default_package().card_keys():
        return True
    return get_card(key) is not None


def _contract(key: str) -> tuple[str, ...]:
    """The variables the send site provides for a card (its contract)."""
    from rosemary.core.cards import get_card

    metadata = default_package().metadata(key)
    if metadata.get("variables"):
        return tuple(metadata["variables"])
    spec = get_card(key)
    if spec is not None:
        return tuple(spec.variables)
    return ()


def _texts(doc: Any) -> list[str]:
    """Every string of a document (wherever it sits)."""
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            out.append(node)

    walk(doc)
    return out


async def preview_card(
    bot, guild_id: int, key: str, form: str, theme: Any = None
) -> tuple[Any, Any]:
    """Render a card of a theme in one shape.

    The variables are the samples of the contract (the same ones the editor
    preview uses), so the admin sees what a member would receive. Returns
    the ``(payload, allowed_mentions)`` pair of the card pipeline.

    ``theme`` previews a theme that is not the active one: the guild's
    snapshot is swapped for the render and restored right after, so the
    admin sees the theme they picked, not the one in use.
    """
    from rosemary.core.card_service import render_card_message
    from rosemary.core.cards import preview_variables

    contract = _contract(key)
    variables = preview_variables(*contract)
    with _using_theme(bot, guild_id, theme):
        return await render_card_message(bot, guild_id, key, variables, form=form)


@contextlib.contextmanager
def _using_theme(bot, guild_id: int, theme: Any):
    """Render as if ``theme`` were the guild's active one (then restore)."""
    if theme is None:
        yield
        return
    cache = getattr(bot, "_theme_cache", None)
    if not isinstance(cache, dict):
        yield
        return
    before = cache.get(guild_id)
    cache[guild_id] = theme
    try:
        yield
    finally:
        if before is None:
            cache.pop(guild_id, None)
        else:
            cache[guild_id] = before


__all__ = [
    "FORMS",
    "ThemeDiff",
    "ThemeIssue",
    "diff_theme",
    "preview_card",
    "validate_theme",
]
