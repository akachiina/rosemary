"""Theme packages: the built-in default, the guild imports and the chain.

A theme is a **package**: a directory with a manifest (``theme.yaml``), the
look & feel (``tokens.yaml``), the frames (``archetypes.yaml`` /
``fragments.yaml``) and the card documents (``cards/*.yaml``). The built-in
one lives in ``themes/default/``; a guild imports its own in
``data/<guild_id>/themes/``.

The old single-file format still works: a ``.yaml`` file with the tokens and
the ``cards:``/``pings:`` sections is a theme with one layer.

A theme may **extend** another one (``extends: default``): the chain is
merged from the root to the child, so the child's tokens win, its card
documents merge over the parent's (the fields it does not declare are
inherited) and a cycle is rejected with a clear error.

Layout::

    themes/default/               # the built-in package (the base of the cards)
    themes/ridiculous_theme.yaml  # a global theme (the old single-file format)
    data/<guild_id>/themes/       # the themes a guild imported via /themes
    data/<guild_id>/themes.json   # {"active": "cute"} per-guild selection

Resolution per guild: the active theme's documents override the catalog
defaults; look & feel falls back to the built-in theme for anything the
theme omits. Files are validated fully at load/import: an invalid theme is
rejected with :class:`ThemeError`, never breaks a send later.
"""

from __future__ import annotations

import copy
import io
import json
import logging
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from rosemary.core.cards import (
    FORMS,
    CardIssue,
    is_embed_document,
    validate_document,
)
from rosemary.core.documents.merge import DocumentError, keep_removals, merge
from rosemary.core.documents.semantic import is_semantic_document, validate_keys
from rosemary.core.storage import GuildStorage
from rosemary.core.theme_package import PACKAGE_DIR, ThemePackage, load_theme_package
from rosemary.core.v2_convert import _HEX_RE, ThemeError, convert_card_entry
from rosemary.ui.theme import Theme

log = logging.getLogger(__name__)

#: A theme card entry that only picks the form ("cards.<key>.form: embed").
#: The content then comes from the card's default document.


#: The keys an entry may carry and still be only a shape override. Anything
#: else makes it a semantic document (a delta over the packaged card).
_FORM_ENTRY_KEYS = frozenset({"form", "text"})


def _is_form_entry(entry: Any) -> bool:
    """Whether a theme card entry is a form-only override.

    Only ``{form: ...}`` (optionally with the legacy ``text`` shortcut) picks
    the shape without rewriting the card. An entry that also declares copy or
    a frame is a semantic document: it must inherit the packaged fields and
    apply its own, form included.
    """
    if not isinstance(entry, dict):
        return False
    return "form" in entry and set(entry) <= _FORM_ENTRY_KEYS


#: Global themes shipped with the bot (resolved from the package root, like
#: ``language/`` and ``data/``: never from the CWD).
THEMES_DIR = Path(__file__).resolve().parent.parent / "themes"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_THEME_BYTES = 64 * 1024

#: The maximum size of an imported package (the plan's limit).
_MAX_PACKAGE_BYTES = 2 * 1024 * 1024

#: The maximum number of files inside an imported package.
_MAX_PACKAGE_FILES = 64

#: The maximum number of cards a package may carry.
_MAX_PACKAGE_CARDS = 200

#: The maximum length of the ``extends`` chain (a cycle is caught earlier).
_MAX_EXTENDS = 8

#: Renamed card keys and the legacy override key each still reads (theme
#: compat: a guild theme customized as ``cards: interactions.card`` before
#: the rename keeps applying to ``roleplay.card``).
_RENAMED_CARDS: dict[str, str] = {
    "roleplay.card": "interactions.card",
    "roleplay.manager": "interactions.manager",
}


#: The built-in theme, loaded once: the baseline every other theme extends.
_BASELINE: Theme | None = None


def baseline_theme() -> Theme:
    """The built-in theme (the baseline of every theme file).

    A theme file customizes the bot: the look & feel it does not define
    falls back to the built-in one (this module's contract). Without the
    baseline, a partial theme would leave every card without its accent
    (the color token unknown) or with a raw ``{emoji}`` placeholder.
    """
    global _BASELINE
    if _BASELINE is None:
        from rosemary.ui.theme import load_theme

        _BASELINE = load_theme(THEMES_DIR / "default" / "tokens.yaml")
    return _BASELINE


class RosemaryTheme(Theme):
    """A loadable theme: look & feel plus card overrides and pings."""

    def __init__(
        self,
        colors: dict[str, str],
        emojis: dict[str, str],
        markdown: dict[str, str] | None = None,
        styles: dict[str, dict[str, str]] | None = None,
        bump: dict[str, str] | None = None,
        medals: dict[str, str] | None = None,
        star_tier_emoji: dict[Any, str] | None = None,
        *,
        name: str = "",
        cards: dict[str, dict[str, Any]] | None = None,
        pings: dict[str, bool] | None = None,
        card_forms: dict[str, str] | None = None,
        color_panel: dict[str, str] | None = None,
        snapshot: dict[str, str] | None = None,
        panels: dict[str, Any] | None = None,
        deltas: dict[str, dict[str, Any]] | None = None,
        archetypes: dict[str, dict[str, Any]] | None = None,
        fragments: dict[str, dict[str, Any]] | None = None,
        extends: str | None = None,
    ) -> None:
        super().__init__(colors, emojis, markdown, styles, bump, medals, star_tier_emoji)
        self.name = name
        self.cards = cards or {}
        self.pings = pings or {}
        self.card_forms = card_forms or {}
        # The semantic card documents of the theme: the deltas over the
        # packaged document (the fields they do not declare are inherited).
        self.deltas = deltas or {}
        # The frames of the theme: the archetypes and the fragments its own
        # documents may name.
        self.archetypes = archetypes or {}
        self.fragments = fragments or {}
        # The theme this one extends (the chain's parent).
        self.extends = extends
        # Color panel image templates (HTML): ``html`` (page frame) +
        # ``item`` (one row per color). Missing/empty falls back to the
        # built-in layout in :mod:`rosemary.core.color_image`.
        self.color_panel = {
            str(k): str(v)
            for k, v in (color_panel or {}).items()
            if isinstance(v, str) and v.strip()
        }
        # Chat snapshot templates (HTML): ``page`` (frame) + ``bubble`` (one
        # message) + optional ``image`` (embedded attachment markup).
        # Missing/empty falls back to :mod:`rosemary.core.chat_image`.
        self.snapshot = {
            str(k): str(v)
            for k, v in (snapshot or {}).items()
            if isinstance(v, str) and v.strip()
        }
        # The chrome of the interactive panels (``panels.chrome``): the
        # header, the body, the footer and the navigation every panel
        # shares.
        self.panels = dict(panels or {})


@dataclass
class _Layers:
    """One layer of the chain: what a theme file or a package carries."""

    #: The look & feel (colors, emojis, markdown, styles...).
    tokens: dict[str, Any] = field(default_factory=dict)
    #: The legacy full cards (the raw Components V2 or the embed).
    cards: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The semantic card documents (the deltas over the packaged card).
    deltas: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The per-card mention toggles.
    pings: dict[str, bool] = field(default_factory=dict)
    #: The per-card form overrides.
    forms: dict[str, str] = field(default_factory=dict)
    #: The frames the theme's own documents may name.
    archetypes: dict[str, dict[str, Any]] = field(default_factory=dict)
    fragments: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The theme this one extends.
    extends: str | None = None


def load_theme_file(path: Path, *, name: str | None = None) -> RosemaryTheme:
    """Parse and fully validate one theme YAML file.

    Raises :class:`ThemeError` on YAML errors, bad tokens or invalid card
    documents: callers reject the theme instead of failing at send time.
    """
    theme_name = name if name is not None else path.stem
    layers = _file_layers(path, name=theme_name)
    return _build_theme(theme_name, layers)


def load_theme_package_theme(package: ThemePackage) -> RosemaryTheme:
    """The theme of one package (the manifest, the tokens and the cards)."""
    layers = _package_layers(package, is_base=False)
    return _build_theme(package.name, layers)


def _file_layers(path: Path, *, name: str) -> _Layers:
    """One theme file read as a layer of the chain."""
    if not _NAME_RE.match(name):
        raise ThemeError([CardIssue("theme_bad_name", (("name", name),))])
    data = _read_yaml(path)
    layers = _Layers(tokens=_parse_tokens(data))
    layers.extends = _extends_of(data)
    layers.cards, layers.deltas, layers.forms = _split_cards(data.get("cards"))
    layers.pings = _parse_pings(data.get("pings"))
    return layers


def _package_layers(package: ThemePackage, *, is_base: bool) -> _Layers:
    """One package read as a layer of the chain.

    The built-in package is the **base**: its documents are the fallback of
    every card, not a delta over themselves.
    """
    layers = _Layers(tokens=_parse_tokens(package.tokens))
    layers.extends = package.extends
    layers.archetypes = package.archetypes
    layers.fragments = package.fragments
    if not is_base:
        layers.cards, layers.deltas, layers.forms = _split_cards(package.documents)
    return layers


def _read_yaml(path: Path) -> dict[str, Any]:
    """One YAML file of a theme (the errors carry the stable codes)."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ThemeError([CardIssue("theme_yaml_invalid")]) from exc
    except OSError as exc:
        raise ThemeError([CardIssue("theme_read_failed")]) from exc
    if not isinstance(data, dict):
        raise ThemeError([CardIssue("theme_document_shape")])
    return data


def _extends_of(data: dict[str, Any]) -> str | None:
    """The theme this one extends (``None`` when it extends nothing)."""
    value = data.get("extends")
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.strip()
    if not _NAME_RE.match(name):
        raise ThemeError([CardIssue("theme_bad_name", (("name", name),))])
    return name


def _parse_tokens(data: dict[str, Any]) -> dict[str, Any]:
    """The look & feel of a theme, read from its tokens."""
    colors = _string_map(data.get("colors"), "theme_bad_color_token")
    for token, value in colors.items():
        if not _HEX_RE.match(value):
            raise ThemeError(
                [CardIssue("theme_bad_color_token", (("token", token),))]
            )
    styles: dict[str, dict[str, str]] = {}
    for style_name, fields in (data.get("styles") or {}).items():
        if not isinstance(fields, dict):
            continue
        styles[str(style_name)] = {
            str(key): str(value) for key, value in fields.items()
        }
    return {
        "colors": colors,
        "emojis": _string_map(data.get("emojis")),
        "markdown": _string_map(data.get("markdown")),
        "styles": styles,
        "bump": _string_map(data.get("bump")),
        "medals": _string_map(data.get("medals")),
        "star_tier_emoji": {
            str(key): str(value)
            for key, value in (data.get("star_tier_emoji") or {}).items()
            if isinstance(value, str)
        },
        "color_panel": {
            str(key): str(value)
            for key, value in (data.get("color_panel") or {}).items()
            if isinstance(key, str) and isinstance(value, str)
        },
        "snapshot": {
            str(key): str(value)
            for key, value in (data.get("snapshot") or {}).items()
            if isinstance(key, str) and isinstance(value, str)
        },
        "panels": dict(data.get("panels") or {}),
    }


def _parse_pings(section: Any) -> dict[str, bool]:
    """The per-card mention toggles of a theme."""
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ThemeError([CardIssue("theme_pings_shape")])
    return {
        str(key): bool(value)
        for key, value in section.items()
        if isinstance(value, bool)
    }


def _split_cards(
    section: Any,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    """The card entries of a theme, split by what they are.

    A theme may carry two kinds of card:

    * a **legacy full card** (the raw Components V2 list, the embed entry, the
      plain string): it replaces the whole card, exactly as before;
    * a **semantic document**: a delta over the packaged card (the fields it
      does not declare are inherited).

    The form-only entry (``cards.<key>.form: embed``) picks the shape of the
    default content without writing it.
    """
    if section is None:
        return {}, {}, {}
    if not isinstance(section, dict):
        raise ThemeError([CardIssue("theme_cards_shape")])
    cards: dict[str, dict[str, Any]] = {}
    deltas: dict[str, dict[str, Any]] = {}
    forms: dict[str, str] = {}
    for key, entry in section.items():
        if not isinstance(key, str) or not key:
            raise ThemeError([CardIssue("theme_cards_shape")])
        if _is_form_entry(entry):
            # A form-only override: the content comes from the card's default,
            # the guild only picks the shape it ships in.
            form = str(entry.get("form"))
            if form not in FORMS:
                raise ThemeError(
                    [CardIssue("theme_card_form", (("key", key), ("form", form)))]
                )
            forms[key] = form
            text = entry.get("text")
            if isinstance(text, str) and text.strip():
                cards[key] = {"kind": "text", "content": text}
            continue
        if is_semantic_document(entry):
            _validate_delta(key, entry)
            deltas[key] = dict(entry)
            continue
        cards[key] = _legacy_card(key, entry)
    return cards, deltas, forms


def _legacy_card(key: str, entry: Any) -> dict[str, Any]:
    """One legacy card entry: the raw Components V2, the embed or the text.

    A plain string is the plain-card shortcut: it becomes a document with a
    single text block (the card keeps its own surface).
    """
    from rosemary.core.embed_convert import convert_embed_entry, is_embed_entry

    if is_embed_entry(entry):
        return convert_embed_entry(key, entry)
    doc = convert_card_entry(key, entry)
    doc["kind"] = "v2"
    return doc


def _validate_delta(key: str, doc: dict[str, Any]) -> None:
    """A semantic document must speak the document language."""
    try:
        validate_keys(doc)
    except DocumentError as exc:
        raise ThemeError(
            [
                CardIssue("theme_card_invalid", (("key", key),)),
                CardIssue(exc.code, (("key", key),)),
            ]
        ) from exc


def _merge_tokens(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """The tokens of two layers, merged deeply (the overlay wins)."""
    out = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = _merge_tokens(current, value)
            continue
        out[key] = copy.deepcopy(value)
    return out


def _baseline_tokens() -> dict[str, Any]:
    """The tokens of the built-in theme (the baseline of every theme)."""
    theme = baseline_theme()
    return {
        "colors": dict(theme.colors),
        "emojis": dict(theme.emojis),
        "markdown": dict(theme.markdown),
        "styles": {name: dict(fields) for name, fields in theme.styles.items()},
        "bump": dict(theme.bump),
        "medals": dict(theme.medals),
        "star_tier_emoji": dict(theme.star_tier_emoji),
        "color_panel": dict(getattr(theme, "color_panel", {}) or {}),
        "snapshot": dict(getattr(theme, "snapshot", {}) or {}),
        "panels": dict(getattr(theme, "panels", {}) or {}),
    }


def _build_theme(name: str, layers: _Layers) -> RosemaryTheme:
    """The theme of one layer (or of a whole merged chain)."""
    from rosemary.core.theme_package import default_package

    tokens = _merge_tokens(_baseline_tokens(), layers.tokens)
    base = default_package()
    theme = RosemaryTheme(
        tokens["colors"],
        tokens["emojis"],
        tokens["markdown"],
        tokens["styles"],
        tokens["bump"],
        tokens["medals"],
        star_tier_emoji=tokens["star_tier_emoji"],
        name=name,
        cards=layers.cards,
        pings=layers.pings,
        card_forms=layers.forms,
        color_panel=tokens["color_panel"],
        snapshot=tokens["snapshot"],
        panels=tokens["panels"],
        deltas=layers.deltas,
        # The frames of the built-in package are always available: a theme
        # may name them without extending the default one.
        archetypes=merge(base.archetypes, layers.archetypes),
        fragments=merge(base.fragments, layers.fragments),
        extends=layers.extends,
    )
    for key, doc in layers.cards.items():
        _validate_card_colors(key, doc, theme)
    for key, doc in layers.deltas.items():
        _validate_delta_colors(key, doc, theme)
    return theme


def _string_map(raw: Any, code: str = "theme_document_shape") -> dict[str, str]:
    """``{str: str}`` from YAML, rejecting non-string values.

    Keys are coerced with ``str()``: YAML 1.1 parses bare numeric keys
    (``medals: 1: ...``) as ints, and a theme file must not be rejected for
    the same content the built-in loader tolerates."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ThemeError([CardIssue(code)])
    for _key, value in raw.items():
        if not isinstance(value, str):
            raise ThemeError([CardIssue(code)])
    return {str(key): value for key, value in raw.items()}


def _validate_card_colors(key: str, doc: dict[str, Any], theme: RosemaryTheme) -> None:
    """Color tokens inside a card must be hex or the theme's own palette."""

    def visit(node: Any) -> str | None:
        if isinstance(node, dict):
            color = node.get("color")
            if isinstance(color, str) and not _HEX_RE.match(color) and color not in theme.colors:
                return color
            for value in node.values():
                found = visit(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = visit(item)
                if found is not None:
                    return found
        return None

    if doc.get("kind") == "embed":
        # Embed docs: only the embed color token needs palette validation;
        # layout/limits were already checked by convert_embed_entry.
        color = (doc.get("embed") or {}).get("color")
        if (
            isinstance(color, str)
            and not _HEX_RE.match(color)
            and color not in theme.colors
        ):
            raise ThemeError(
                [
                    CardIssue("theme_card_invalid", (("key", key),)),
                    CardIssue("color_unknown", (("color", color),)),
                ]
            )
        return

    unknown = visit(doc.get("blocks", []))
    if unknown is not None:
        raise ThemeError(
            [
                CardIssue(
                    "theme_card_invalid",
                    (("key", key),),
                ),
                CardIssue("color_unknown", (("color", unknown),)),
            ]
        )
    # Full Discord layout validation (counts, nesting, urls...): hex colors
    # are self-contained, so the theme palette is not passed here.
    errors = validate_document(doc, draft=True)
    if errors:
        raise ThemeError([CardIssue("theme_card_invalid", (("key", key),))] + errors)


def _validate_delta_colors(key: str, doc: dict[str, Any], theme: RosemaryTheme) -> None:
    """The color of a semantic document must be hex or a theme token.

    A color that names a variable (the state chooses it) is left alone: the
    compiler resolves it at send time.
    """
    color = doc.get("color")
    if not isinstance(color, str):
        return
    for token in color if isinstance(color, list) else [color]:
        if not isinstance(token, str):
            continue
        value = token.strip()
        if not value or "{" in value:
            continue
        if _HEX_RE.match(value) or value in theme.colors:
            continue
        raise ThemeError(
            [
                CardIssue("theme_card_invalid", (("key", key),)),
                CardIssue("color_unknown", (("color", value),)),
            ]
        )


def _merge_layers(base: _Layers, overlay: _Layers) -> _Layers:
    """Two layers of the chain, merged (the overlay wins).

    The tokens merge deeply, the frames merge deeply and the card documents
    merge as documents (the fields the child does not declare are
    inherited). A semantic document wins over a legacy full card of the
    same key: the delta is the more specific layer.
    """
    out = _Layers(
        tokens=_merge_tokens(base.tokens, overlay.tokens),
        cards={**base.cards, **overlay.cards},
        pings={**base.pings, **overlay.pings},
        forms={**base.forms, **overlay.forms},
        archetypes=merge(base.archetypes, overlay.archetypes),
        fragments=merge(base.fragments, overlay.fragments),
        extends=overlay.extends,
    )
    for key, doc in base.deltas.items():
        out.deltas[key] = doc
    for key, doc in overlay.deltas.items():
        current = out.deltas.get(key)
        merged = merge(current, doc) if current else dict(doc)
        # A slot the child removes (``null``) keeps saying so: the null is
        # what removes the slot of the packaged document.
        out.deltas[key] = keep_removals(doc, merged)
    for key in out.deltas:
        out.cards.pop(key, None)
    return out


def _is_base_package(source: Path) -> bool:
    """Whether the source is the built-in package (the base of the cards)."""
    try:
        return source.resolve() == PACKAGE_DIR.resolve()
    except OSError:  # pragma: no cover - defensive
        return False


class ThemeStore:
    """Discovers, validates, imports and selects themes per guild."""

    def __init__(self, data_dir: Path | str, themes_dir: Path | None = None) -> None:
        self.themes_dir = Path(themes_dir) if themes_dir else THEMES_DIR
        self._data_dir = Path(data_dir)
        self._selection = GuildStorage(
            self._data_dir, filename="themes.json", use_defaults=False
        )
        # (guild_id, theme_name) -> RosemaryTheme; invalidated on every write.
        self._cache: dict[tuple[int, str], RosemaryTheme] = {}

    #: discovery ---------------------------------------------------------

    def _guild_dir(self, guild_id: int) -> Path:
        return self._data_dir / str(guild_id) / "themes"

    def list_global(self) -> list[str]:
        """Names of the global themes shipped in ``themes/``."""
        return self._names_in(self.themes_dir)

    def list_guild(self, guild_id: int) -> list[str]:
        """Names the guild imported (its own file wins on name clashes)."""
        return self._names_in(self._guild_dir(guild_id))

    def list_all(self, guild_id: int) -> list[str]:
        """Every selectable theme name for a guild, sorted."""
        return sorted(set(self.list_global()) | set(self.list_guild(guild_id)))

    @staticmethod
    def _names_in(directory: Path) -> list[str]:
        """The theme names of a folder: the files and the packages."""
        if not directory.is_dir():
            return []
        names = {
            path.stem
            for path in directory.glob("*.yaml")
            if path.is_file() and _NAME_RE.match(path.stem)
        }
        names.update(
            path.stem
            for path in directory.glob("*.yml")
            if path.is_file() and _NAME_RE.match(path.stem)
        )
        names.update(
            path.name
            for path in directory.iterdir()
            if path.is_dir()
            and _NAME_RE.match(path.name)
            and (path / "theme.yaml").is_file()
        )
        return sorted(names)

    def _source_for(self, guild_id: int | None, name: str) -> Path | None:
        """The source of ``name``: the guild's own first, then global.

        A source is a theme file (the old single-file format) or a theme
        package (a folder with a ``theme.yaml`` manifest).
        """
        if not _NAME_RE.match(name):
            return None
        candidates: list[Path] = []
        if guild_id is not None:
            local = self._guild_dir(guild_id)
            candidates += [local / f"{name}{suffix}" for suffix in (".yaml", ".yml")]
            candidates.append(local / name)
        candidates += [
            self.themes_dir / f"{name}{suffix}" for suffix in (".yaml", ".yml")
        ]
        candidates.append(self.themes_dir / name)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
            if candidate.is_dir() and (candidate / "theme.yaml").is_file():
                return candidate
        return None

    #: the path of the old API (the file of a theme) -----------------------

    def _path_for(self, guild_id: int | None, name: str) -> Path | None:
        """The file of ``name`` (the old API: the packages are not files)."""
        source = self._source_for(guild_id, name)
        if source is None or not source.is_file():
            return None
        return source

    def _chain(self, name: str, guild_id: int | None) -> list[tuple[str, Path]]:
        """The chain of a theme: the theme itself, then its parents.

        A cycle is rejected with a clear error and the chain never grows
        beyond the plan's depth.
        """
        chain: list[tuple[str, Path]] = []
        seen: list[str] = []
        current: str | None = name
        while current:
            if current in seen:
                raise ThemeError(
                    [CardIssue("theme_extends_cycle", (("name", current),))]
                )
            if len(chain) >= _MAX_EXTENDS:
                raise ThemeError(
                    [CardIssue("theme_extends_depth", (("name", current),))]
                )
            seen.append(current)
            source = self._source_for(guild_id, current)
            if source is None:
                raise ThemeError(
                    [CardIssue("theme_not_found", (("name", current),))]
                )
            chain.append((current, source))
            current = self._extends_of(source)
        return chain

    def _extends_of(self, source: Path) -> str | None:
        """The theme a source extends (``None`` when it extends nothing)."""
        if source.is_dir():
            package = load_theme_package(source)
            return package.extends
        return _extends_of(_read_yaml(source))

    def _read_layers(self, source: Path) -> _Layers:
        """One source of the chain read as a layer."""
        if source.is_dir():
            package = load_theme_package(source)
            return _package_layers(package, is_base=_is_base_package(source))
        return _file_layers(source, name=source.stem)

    def load(self, name: str, guild_id: int | None = None) -> RosemaryTheme:
        """Load and validate the theme ``name`` (guild file wins over global).

        The chain is merged from the root to the child: the child's tokens
        win, its card documents merge over the parent's (the fields it does
        not declare are inherited).
        """
        chain = self._chain(name, guild_id)
        layers = _Layers()
        for _name, source in reversed(chain):
            layer = self._read_layers(source)
            layers = _merge_layers(layers, layer)
        return _build_theme(name, layers)

    #: per-guild selection -----------------------------------------------

    async def get_active(self, guild_id: int) -> str | None:
        """The guild's selected theme name or ``None`` (built-in default)."""
        data = await self._selection.get(guild_id)
        value = data.get("active")
        return value if isinstance(value, str) and value else None

    async def set_active(self, guild_id: int, name: str) -> None:
        """Select a theme for the guild (must exist). Clears any cache."""
        if self._source_for(guild_id, name) is None:
            raise ThemeError([CardIssue("theme_not_found", (("name", name),))])
        await self._selection.set(guild_id, "active", name)
        self._cache.pop((guild_id, name), None)

    async def clear_active(self, guild_id: int) -> None:
        """Back to the built-in default theme."""
        await self._selection.delete_keys(guild_id, "active")

    def invalidate_guild(self, guild_id: int) -> None:
        """Drop every cached theme for this guild (files edited on disk).

        The next ``effective_theme`` re-reads the active file instead of
        returning the stale cached object."""
        for key in [k for k in self._cache if k[0] == guild_id]:
            self._cache.pop(key, None)

    async def effective_theme(self, bot, guild_id: int | None) -> Theme:
        """The guild's active theme, falling back to the built-in one.

        Loaded themes are cached per ``(guild_id, name)``; writes invalidate.
        A missing/corrupt active file falls back to the built-in theme so one
        deleted file can never break sends.
        """
        if guild_id is None:
            return bot.theme
        active = await self.get_active(guild_id)
        if active is None:
            return bot.theme
        cached = self._cache.get((guild_id, active))
        if cached is not None:
            return cached
        try:
            theme = self.load(active, guild_id=guild_id)
        except (ThemeError, OSError) as exc:
            log.warning(
                "Active theme %r unusable in guild %s: %s", active, guild_id, exc
            )
            return bot.theme
        self._cache[(guild_id, active)] = theme
        return theme

    #: import / export ----------------------------------------------------

    async def import_theme(self, guild_id: int, filename: str, payload: bytes) -> str:
        """Validate and store an uploaded theme; returns its name.

        The upload is parsed and fully validated *before* anything is
        written, so a rejected import leaves the guild's theme folder
        untouched. Two shapes are accepted: the single YAML file (the old
        format) and the ZIP of a theme package.
        """
        if filename.lower().endswith(".zip"):
            return await self._import_package(guild_id, filename, payload)
        stem = Path(filename).stem.lower().replace(" ", "_")
        if not _NAME_RE.match(stem):
            raise ThemeError([CardIssue("theme_bad_name", (("name", stem),))])
        if not filename.lower().endswith((".yaml", ".yml")):
            raise ThemeError([CardIssue("theme_bad_extension")])
        if len(payload) > _MAX_THEME_BYTES:
            raise ThemeError([CardIssue("theme_too_large")])
        target = self._guild_dir(guild_id) / f"{stem}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        try:
            self.load(stem, guild_id=guild_id)
        except ThemeError:
            target.unlink(missing_ok=True)
            raise
        self._cache.pop((guild_id, stem), None)
        return stem

    async def _import_package(
        self, guild_id: int, filename: str, payload: bytes
    ) -> str:
        """Import a theme package (the ZIP of a folder with ``theme.yaml``)."""
        if len(payload) > _MAX_PACKAGE_BYTES:
            raise ThemeError([CardIssue("theme_too_large")])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _extract_package(payload, root)
            source = _package_root(root)
            if source is None:
                raise ThemeError([CardIssue("theme_package_no_manifest")])
            package = load_theme_package(source)
            name = package.name.strip()
            if not _NAME_RE.match(name):
                raise ThemeError([CardIssue("theme_bad_name", (("name", name),))])
            if len(package.documents) > _MAX_PACKAGE_CARDS:
                raise ThemeError(
                    [
                        CardIssue(
                            "theme_too_many_cards",
                            (("count", len(package.documents), ("max", _MAX_PACKAGE_CARDS)),),
                        )
                    ]
                )
            # Validate the whole chain (the package and its parents) before
            # anything is written.
            target = self._guild_dir(guild_id) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
            try:
                self.load(name, guild_id=guild_id)
            except (ThemeError, OSError):
                shutil.rmtree(target, ignore_errors=True)
                raise
        self._cache.pop((guild_id, name), None)
        return name

    def export_theme(self, guild_id: int, name: str) -> tuple[str, bytes] | None:
        """The bytes of ``name`` (guild's own first, then global).

        A package is exported as a ZIP (the folder with its manifest); a
        single-file theme keeps its raw bytes.
        """
        source = self._source_for(guild_id, name)
        if source is None:
            return None
        if source.is_dir():
            return (f"{name}.zip", _zip_package(source))
        return (source.name, source.read_bytes())

    async def remove_guild_theme(self, guild_id: int, name: str) -> bool:
        """Delete a guild-imported theme; deselects it when active."""
        removed = False
        for suffix in (".yaml", ".yml"):
            candidate = self._guild_dir(guild_id) / f"{name}{suffix}"
            if candidate.is_file():
                candidate.unlink()
                removed = True
        package = self._guild_dir(guild_id) / name
        if package.is_dir():
            shutil.rmtree(package, ignore_errors=True)
            removed = True
        self._cache.pop((guild_id, name), None)
        if removed and await self.get_active(guild_id) == name:
            await self.clear_active(guild_id)
        return removed


def _extract_package(payload: bytes, target: Path) -> None:
    """Unpack a theme package, refusing anything that escapes the folder."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if len(names) > _MAX_PACKAGE_FILES:
                raise ThemeError([CardIssue("theme_package_too_many_files")])
            for name in names:
                path = Path(name)
                if path.is_absolute() or ".." in path.parts:
                    raise ThemeError([CardIssue("theme_package_bad_path")])
            archive.extractall(target)
    except zipfile.BadZipFile as exc:
        raise ThemeError([CardIssue("theme_zip_invalid")]) from exc


def _package_root(root: Path) -> Path | None:
    """The folder of the package (the manifest may sit inside one folder)."""
    if (root / "theme.yaml").is_file():
        return root
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "theme.yaml").is_file():
            return child
    return None


def _zip_package(source: Path) -> bytes:
    """The ZIP of a theme package (the folder with its manifest)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_dir():
                continue
            archive.write(path, path.relative_to(source).as_posix())
    return buffer.getvalue()


def theme_store(bot) -> ThemeStore:
    """The bot's store (created once in ``_setup``), or a transient one."""
    store = getattr(bot, "_theme_store", None)
    if store is not None:
        return store
    return ThemeStore(bot.storage.data_dir)


#: resolution ------------------------------------------------------------


async def preload_themes(bot) -> None:
    """Snapshot every guild's effective theme into ``bot._theme_cache``.

    ``theme_for`` is synchronous (mention checks run inside send paths), so the
    async work happens here: at boot, when a guild joins and after any
    ``/themes`` selection or import. Guilds without an active theme resolve to
    the built-in theme and are simply absent from the cache.
    """
    store = theme_store(bot)
    cache: dict[int, Theme] = getattr(bot, "_theme_cache", {}) or {}
    cache.clear()
    for guild in list(getattr(bot, "guilds", [])):
        try:
            theme = await store.effective_theme(bot, guild.id)
        except Exception as exc:  # defensive: one bad file never breaks boot
            log.warning("Theme preload failed for guild %s: %s", guild.id, exc)
            continue
        if theme is not bot.theme:
            cache[guild.id] = theme
    bot._theme_cache = cache


def theme_for(bot, guild_id: int | None) -> Theme:
    """The guild's effective theme, synchronously (see :func:`preload_themes`).

    Falls back to the built-in theme when the guild has no active theme or its
    snapshot is cold: a cold snapshot means the built-in look until the next
    preload, never a broken send.
    """
    if guild_id is None:
        return bot.theme
    cache = getattr(bot, "_theme_cache", None)
    if isinstance(cache, dict):
        theme = cache.get(guild_id)
        if theme is not None:
            return theme
    return bot.theme


async def card_document(bot, guild_id: int | None, key: str) -> dict[str, Any] | None:
    """The guild's themed override document for ``key`` (``None`` = default).

    Resolution: active theme file's ``cards.<key>`` -> ``None`` (the caller
    keeps its own catalog/builder default). Validates lazily too: files were
    already validated at load, this is belt-and-braces against hand edits.
    """
    if guild_id is None:
        return None
    store = theme_store(bot)
    theme = await store.effective_theme(bot, guild_id)
    cards = getattr(theme, "cards", None)
    doc = cards.get(key) if isinstance(cards, dict) else None
    if doc is None and isinstance(cards, dict):
        # Renamed cards keep reading the legacy override (interactions ->
        # roleplay): guild themes customized before the rename keep working.
        legacy = _RENAMED_CARDS.get(key)
        if legacy is not None:
            doc = cards.get(legacy)
    if doc is None:
        return None
    if is_embed_document(doc):
        # Embed docs carry no V2 blocks; shape and limits were fully
        # validated at load (convert_embed_entry): pass them through.
        return doc
    if validate_document(doc, draft=True):
        log.warning(
            "Themed card %s in %r is invalid; using default",
            key,
            getattr(theme, "name", "?"),
        )
        return None
    return doc


async def card_delta(bot, guild_id: int | None, key: str) -> dict[str, Any] | None:
    """The guild's semantic document for ``key`` (``None`` = no delta).

    A delta is a document that customizes a card without rewriting it: the
    fields it does not declare are inherited from the packaged document.

    Reads the guild's theme snapshot synchronously (like
    :func:`card_form`): the delta and the frames it resolves against come
    from the same theme object, and the snapshot is refreshed whenever the
    guild selects or imports a theme.
    """
    if guild_id is None:
        return None
    theme = theme_for(bot, guild_id)
    deltas = getattr(theme, "deltas", None)
    if not isinstance(deltas, dict):
        return None
    delta = deltas.get(key)
    return dict(delta) if isinstance(delta, dict) else None


def card_form(bot, guild_id: int | None, key: str) -> str | None:
    """The guild's form override for ``key`` (``None`` = the card's default).

    A theme may ship a card in another shape without rewriting its content
    ("cards.<key>.form: embed"). A themed *document* always wins over this:
    the override only decides the shape of the default content.

    Reads the guild's theme snapshot synchronously (like
    :func:`card_origin`): the snapshot is refreshed when a theme is imported
    or selected, and a cold snapshot simply keeps the card's default form.
    """
    if guild_id is None:
        return None
    theme = theme_for(bot, guild_id)
    forms = getattr(theme, "card_forms", None)
    form = forms.get(key) if isinstance(forms, dict) else None
    if form is None and isinstance(forms, dict):
        legacy = _RENAMED_CARDS.get(key)
        if legacy is not None:
            form = forms.get(legacy)
    return form if form in FORMS else None


def card_origin(bot, guild_id: int | None, key: str) -> tuple[str | None, str | None]:
    """``(theme_name, key)`` when the active theme overrides ``key``, else ``(None, None)``.

    Synchronous on purpose: reads the same snapshot :func:`theme_for` uses.
    """
    if guild_id is None:
        return (None, None)
    theme = theme_for(bot, guild_id)
    cards = getattr(theme, "cards", None)
    if isinstance(cards, dict) and key in cards:
        return (getattr(theme, "name", None) or "?", key)
    return (None, None)


def dump_selection(data_dir: Path | str, guild_id: int) -> dict[str, Any]:
    """Debug helper: the raw selection file for one guild."""
    path = Path(data_dir) / str(guild_id) / "themes.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
