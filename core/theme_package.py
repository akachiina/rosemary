"""The default card package: the documents every card falls back to.

The built-in package lives in ``themes/default/``:

.. code-block:: text

    themes/default/archetypes.yaml   the frames (log_event, announcement...)
    themes/default/fragments.yaml    the reusable pieces (attribution...)
    themes/default/cards/*.yaml      the documents, one file per family

A document is semantic: it says what the card is (title, body, fields,
footer, author, thumbnail) and points at the catalog for the copy. The
package is loaded once per process: the documents are static, and a send must
never pay for a disk read.

A broken package never breaks a send: the loader logs the problem and
returns what it could read.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from rosemary.core.documents.merge import DocumentError, resolve_document

log = logging.getLogger(__name__)

#: The built-in package: ``rosemary/themes/default``.
PACKAGE_DIR = Path(__file__).resolve().parents[1] / "themes" / "default"


@dataclasses.dataclass(frozen=True)
class ThemePackage:
    """One theme package, read from the disk (the manifest and the layers).

    A package is the new shape of a theme: a directory with a manifest
    (``theme.yaml``), the look & feel (``tokens.yaml``), the frames
    (``archetypes.yaml`` / ``fragments.yaml``) and the card documents
    (``cards/*.yaml``).

    The documents are **unresolved** here: the archetype and the fragments
    are merged when the package is resolved (:func:`load_package`), so a
    theme can carry a delta that names its own frames.
    """

    name: str = ""
    extends: str | None = None
    version: int = 2
    tokens: dict[str, Any] = dataclasses.field(default_factory=dict)
    archetypes: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    fragments: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    documents: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)

    def document(self, key: str) -> dict[str, Any] | None:
        """The document of ``key`` (``None`` when the package has none)."""
        return self.documents.get(key)


@dataclasses.dataclass(frozen=True)
class Package:
    """The resolved documents of one package, plus their archetypes."""

    archetypes: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    fragments: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    documents: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)

    def document(self, key: str) -> dict[str, Any] | None:
        """The document of ``key`` (``None`` when the package has none)."""
        return self.documents.get(key)

    def card_keys(self) -> tuple[str, ...]:
        """Every card key the package carries."""
        return tuple(sorted(self.documents))

    def metadata(self, key: str) -> dict[str, Any]:
        """The contract of a card: variables, mention policy, category, form.

        A sequence document declares its metadata outside ``messages``; the
        shape, the actor and the tone of the card are the ones of its first
        message (the main one), so the editor and the fallback keep reading
        one card per key.
        """
        doc = self.documents.get(key) or {}
        first = _first_message(doc)
        form = str(first.get("form") or doc.get("form") or "embed")
        if first.get("embeds"):
            # A stack of embeds only lives in the classic embed form: the
            # list decides the shape too.
            form = "embed"
        return {
            "variables": tuple(doc.get("variables") or ()),
            "mention": str(doc.get("mention") or "none"),
            "category": str(doc.get("category") or "general"),
            "form": form,
            "actor": str(first.get("actor") or doc.get("actor") or ""),
            "rich": bool(doc.get("rich", True)),
            "style": str(first.get("style") or doc.get("style") or "info"),
            "legacy_body": doc.get("legacy_body"),
        }


def _first_message(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    """The message that carries the card's own shape (the first one)."""
    raw = doc.get("messages")
    if isinstance(raw, list) and raw and isinstance(raw[0], Mapping):
        return raw[0]
    return doc


def load_theme_package(directory: Path | str) -> ThemePackage:
    """Read a theme package from the disk (the raw layers, unresolved).

    A missing manifest is not an error: the directory is then read as a bare
    layer folder (the built-in package grew its manifest later).
    """
    root = Path(directory)
    manifest = _read_file(root / "theme.yaml")
    manifest = manifest if isinstance(manifest, dict) else {}
    name = str(manifest.get("name") or root.name)
    extends = manifest.get("extends")
    version = manifest.get("version")
    tokens = _read_file(root / "tokens.yaml")
    return ThemePackage(
        name=name,
        extends=str(extends).strip() if isinstance(extends, str) and extends.strip() else None,
        version=int(version) if isinstance(version, int) else 2,
        tokens=tokens if isinstance(tokens, dict) else {},
        archetypes=_read_section(root / "archetypes.yaml", "archetypes"),
        fragments=_read_section(root / "fragments.yaml", "fragments"),
        documents=_read_documents(root),
    )


def _read_documents(root: Path) -> dict[str, dict[str, Any]]:
    """The card documents of a package (``cards/*.yaml``), unresolved."""
    documents: dict[str, dict[str, Any]] = {}
    cards_dir = root / "cards"
    if not cards_dir.is_dir():
        return documents
    for path in sorted(cards_dir.glob("*.yaml")):
        raw = _read_file(path)
        if not isinstance(raw, dict):
            log.error("card package %s is not a mapping; ignored", path.name)
            continue
        for key, doc in raw.items():
            if not isinstance(key, str) or not isinstance(doc, dict):
                log.error("card package %s has a broken entry %r; ignored", path.name, key)
                continue
            documents[key] = doc
    return documents


def load_package(directory: Path | str = PACKAGE_DIR) -> Package:
    """Read a package from the disk and resolve its documents."""
    raw = load_theme_package(directory)
    documents: dict[str, dict[str, Any]] = {}
    for key, doc in raw.documents.items():
        try:
            documents[key] = resolve_document(doc, raw.archetypes, raw.fragments)
        except DocumentError as exc:
            log.error("card package %s: %s (%s)", key, exc, exc.code)
    return Package(
        archetypes=raw.archetypes, fragments=raw.fragments, documents=documents
    )


def _read_section(path: Path, section: str) -> dict[str, dict[str, Any]]:
    """One section of a package file (``archetypes:`` / ``fragments:``)."""
    raw = _read_file(path)
    if raw is None:
        return {}
    found = raw.get(section)
    if not isinstance(found, dict):
        if found is not None:
            log.error("package file %s: %s is not a mapping", path.name, section)
        return {}
    return {
        str(name): dict(body)
        for name, body in found.items()
        if isinstance(body, dict)
    }


def _read_file(path: Path) -> Any:
    """One YAML file of the package (``None`` when it is missing or broken)."""
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.error("could not read the card package file %s: %s", path.name, exc)
        return None


_PACKAGE: Package | None = None


def default_package() -> Package:
    """The built-in package (loaded once per process)."""
    global _PACKAGE
    if _PACKAGE is None:
        _PACKAGE = load_package()
    return _PACKAGE


def reload_package(directory: Path | str = PACKAGE_DIR) -> Package:
    """Read the package again (the tests and the ``/temas`` lint)."""
    global _PACKAGE
    _PACKAGE = load_package(directory)
    return _PACKAGE


def document_metadata(package: Package, key: str) -> Mapping[str, Any]:
    """The contract of a card (see :meth:`Package.metadata`)."""
    return package.metadata(key)


def legacy_body_key(doc: Mapping[str, Any], key: str = "") -> str:
    """The theme key of the old body card of a document (``""`` when none).

    Before the documents, a guild could customize the body of a card under
    the key of its catalog copy (``cards.<event>.description``). That
    override still wins over the packaged body.
    """
    flag = doc.get("legacy_body", True)
    if flag is False:
        return ""
    if isinstance(flag, str):
        return flag.strip()
    return f"{key}.description" if key else ""


__all__ = [
    "PACKAGE_DIR",
    "Package",
    "ThemePackage",
    "default_package",
    "document_metadata",
    "legacy_body_key",
    "load_package",
    "load_theme_package",
    "reload_package",
]
