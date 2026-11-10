"""The loader of the settings registry: ``settings/*.yaml`` -> specs.

The registry is declarative: one file per category, one entry per setting.
The loader merges the files (in the category order, so the menu keeps its
display order) and validates every entry, so a typo in a YAML file fails at
boot with a clear message instead of showing up as a broken setting.

The schema of one entry (``settings/moderation.yaml``)::

    moderation.warn_limit:
      category: moderation
      type: integer
      default: 3
      min: 1
      max: 10

The optional keys are ``choices`` (a list of strings), ``duration`` (an integer
setting whose value is a number of seconds), ``multiline`` (a string setting
edited in a paragraph modal), ``default_catalog_key`` (the catalog key whose
localized text is the effective default) and ``legacy_single_key`` (the scalar
setting a list setting inherits from).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from rosemary.core.settings_spec import SettingCategory, SettingSpec, SettingType

log = logging.getLogger(__name__)

#: The directory of the settings package (resolved from the module, never
#: from the CWD).
SETTINGS_DIR = Path(__file__).resolve().parents[1] / "settings"

#: The keys one entry may carry.
_ENTRY_KEYS = frozenset(
    {
        "category",
        "type",
        "default",
        "min",
        "max",
        "choices",
        "duration",
        "multiline",
        "default_catalog_key",
        "legacy_single_key",
    }
)


class SettingsError(Exception):
    """A settings file is invalid (unknown key, bad type, bad constraint)."""


def _category_files(directory: Path) -> list[Path]:
    """The settings files, in the category order (the menu's display order)."""
    files = {path.stem: path for path in directory.glob("*.yaml")}
    ordered = [
        files[category.value] for category in SettingCategory if category.value in files
    ]
    # A file whose name is not a category would be silently ignored: list it.
    extra = sorted(path for name, path in files.items() if name not in set(
        category.value for category in SettingCategory
    ))
    return ordered + extra


def _spec_from_entry(key: str, entry: Any, source: Path) -> SettingSpec:
    """Build one spec from one YAML entry (validating it)."""
    if not isinstance(entry, dict):
        raise SettingsError(f"{source}: setting {key!r} is not a mapping")

    unknown = sorted(set(entry) - _ENTRY_KEYS)
    if unknown:
        raise SettingsError(f"{source}: setting {key!r} has unknown keys: {unknown}")

    raw_category = entry.get("category")
    try:
        category = SettingCategory(str(raw_category))
    except ValueError:
        raise SettingsError(
            f"{source}: setting {key!r} has an unknown category {raw_category!r}"
        ) from None

    raw_type = entry.get("type")
    try:
        value_type = SettingType(str(raw_type))
    except ValueError:
        raise SettingsError(
            f"{source}: setting {key!r} has an unknown type {raw_type!r}"
        ) from None

    if "default" not in entry:
        raise SettingsError(f"{source}: setting {key!r} has no default")

    default = entry["default"]
    if value_type is SettingType.CHANNEL_LIST:
        if not isinstance(default, list):
            raise SettingsError(f"{source}: setting {key!r} must default to a list")
        default = tuple(default)
    elif value_type is SettingType.BOOLEAN:
        if not isinstance(default, bool):
            raise SettingsError(f"{source}: setting {key!r} must default to a boolean")
    elif value_type is SettingType.INTEGER:
        if not isinstance(default, int) or isinstance(default, bool):
            raise SettingsError(f"{source}: setting {key!r} must default to an integer")
    elif value_type is SettingType.STRING or value_type is SettingType.CHOICE:
        if not isinstance(default, str):
            raise SettingsError(f"{source}: setting {key!r} must default to a string")
    elif default is not None and not isinstance(default, int):
        raise SettingsError(
            f"{source}: setting {key!r} must default to null or an id"
        )

    min_value = entry.get("min")
    max_value = entry.get("max")
    for name, bound in (("min", min_value), ("max", max_value)):
        if bound is not None and (not isinstance(bound, int) or isinstance(bound, bool)):
            raise SettingsError(f"{source}: setting {key!r} has a non-integer {name}")
    if min_value is not None and max_value is not None and min_value > max_value:
        raise SettingsError(f"{source}: setting {key!r} has min greater than max")
    if value_type is SettingType.INTEGER:
        if min_value is not None and default < min_value:
            raise SettingsError(f"{source}: setting {key!r} is below its min")
        if max_value is not None and default > max_value:
            raise SettingsError(f"{source}: setting {key!r} is above its max")

    raw_choices = entry.get("choices")
    choices: tuple[str, ...] = ()
    if raw_choices is not None:
        if value_type is not SettingType.CHOICE:
            raise SettingsError(
                f"{source}: setting {key!r} lists choices but is not a choice"
            )
        if not isinstance(raw_choices, list) or not raw_choices:
            raise SettingsError(f"{source}: setting {key!r} needs a non-empty choice list")
        if not all(isinstance(choice, str) for choice in raw_choices):
            raise SettingsError(f"{source}: setting {key!r} has a non-string choice")
        choices = tuple(raw_choices)
        if default not in choices:
            raise SettingsError(f"{source}: setting {key!r} defaults to an unlisted choice")
    elif value_type is SettingType.CHOICE:
        raise SettingsError(f"{source}: setting {key!r} needs a choice list")

    is_duration = bool(entry.get("duration", False))
    if is_duration and value_type is not SettingType.INTEGER:
        raise SettingsError(f"{source}: setting {key!r} is a duration but not an integer")

    multiline = bool(entry.get("multiline", False))
    if multiline and value_type is not SettingType.STRING:
        raise SettingsError(f"{source}: setting {key!r} is multiline but not a string")

    default_catalog_key = entry.get("default_catalog_key")
    if default_catalog_key is not None and value_type is not SettingType.STRING:
        raise SettingsError(
            f"{source}: setting {key!r} has a default catalog key but is not a string"
        )

    legacy_single_key = entry.get("legacy_single_key")
    if legacy_single_key is not None and value_type is not SettingType.CHANNEL_LIST:
        raise SettingsError(
            f"{source}: setting {key!r} has a legacy key but is not a channel list"
        )

    return SettingSpec(
        key=key,
        category=category,
        value_type=value_type,
        default=default,
        min_value=min_value,
        max_value=max_value,
        choices=choices,
        is_duration=is_duration,
        multiline=multiline,
        default_catalog_key=default_catalog_key,
        legacy_single_key=legacy_single_key,
    )


def load_settings(directory: Path | None = None) -> dict[str, SettingSpec]:
    """Load the registry: every YAML file of the package, merged.

    The files are read in the category order, so the menu keeps its display
    order and the order of the settings inside a category is the order of the
    file. A duplicate key (the same setting in two files) is an error.
    """
    root = Path(directory) if directory is not None else SETTINGS_DIR
    if not root.is_dir():
        raise SettingsError(f"the settings directory {root} does not exist")

    registry: dict[str, SettingSpec] = {}
    for path in _category_files(root):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if document is None:
            continue
        if not isinstance(document, dict) or "settings" not in document:
            raise SettingsError(f"{path}: the file has no 'settings' mapping")
        entries = document["settings"]
        if not isinstance(entries, dict):
            raise SettingsError(f"{path}: 'settings' is not a mapping")
        for key, entry in entries.items():
            if key in registry:
                raise SettingsError(f"{path}: setting {key!r} is declared twice")
            registry[key] = _spec_from_entry(str(key), entry, path)

    if not registry:
        raise SettingsError(f"the settings directory {root} declares no setting")
    return registry
