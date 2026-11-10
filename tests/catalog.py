"""The language catalogs, read from the modular layout (or the old file).

The catalogs live in ``language/<code>/<feature>.yaml`` (one file per
feature, the modular layout of the plan's P3). The old single file
(``language/<code>.yaml``) still loads, so the tests read the catalogs
through this helper and never care which shape is on the disk.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

#: The folder of the catalogs (resolved from the file, never from the CWD).
LANG_DIR = Path(__file__).resolve().parents[1] / "language"


def catalog_files(code: str) -> list[Path]:
    """The files of one catalog: the feature files, or the old single file."""
    folder = LANG_DIR / code
    if folder.is_dir():
        return sorted(folder.glob("*.yaml"))
    single = LANG_DIR / f"{code}.yaml"
    return [single] if single.is_file() else []


def read_catalog(path: Path) -> dict[str, Any]:
    """One catalog file read (an empty file is an empty section)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise AssertionError(f"{path}: the catalog must be a mapping")
    return data


def load_catalog(code: str) -> dict[str, Any]:
    """One catalog as a mapping (the feature files merged)."""
    merged: dict[str, Any] = {}
    for path in catalog_files(code):
        merged = deep_merge(merged, read_catalog(path))
    return merged


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested mapping into dotted-key entries."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten(value, dotted))
        else:
            flat[dotted] = value
    return flat


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Two mappings merged deeply (the overlay wins on the leaves)."""
    out = dict(base)
    for key, value in overlay.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = deep_merge(current, value)
            continue
        out[key] = value
    return out


@cache
def _cached_catalog(code: str) -> dict[str, Any]:
    """One catalog, cached (the files never change during a test run)."""
    return load_catalog(code)


def catalog_text(code: str, key: str) -> str:
    """One catalog string (the dotted path), or the key when it is missing."""
    node = _cached_catalog(code)
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node if isinstance(node, str) else key
