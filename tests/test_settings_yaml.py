"""The settings registry is declarative and it did not change.

The registry moved from Python to ``settings/*.yaml`` (one file per category).
``tests/settings_baseline.json`` freezes the registry as it was before the
move (every field of every spec), so the migration cannot change a default, a
constraint or a category unnoticed.

The loader also has to reject a broken file: a typo in a YAML file must fail
at boot with a clear message, never show up as a broken setting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rosemary.core.settings import MENU_TIMEOUT, SETTINGS, SettingCategory
from rosemary.core.settings_loader import SettingsError, load_settings
from rosemary.core.settings_spec import SettingType

BASELINE = Path(__file__).resolve().parent / "settings_baseline.json"


def _normalized(spec) -> dict:
    """The spec as plain data (the enums and the tuples become JSON)."""
    return {
        "category": spec.category.value,
        "type": spec.value_type.value,
        "default": list(spec.default) if isinstance(spec.default, tuple) else spec.default,
        "min": spec.min_value,
        "max": spec.max_value,
        "choices": list(spec.choices),
        "duration": bool(spec.is_duration),
        "multiline": bool(spec.multiline),
        "default_catalog_key": spec.default_catalog_key,
        "legacy_single_key": spec.legacy_single_key,
    }


def test_the_registry_has_the_expected_size():
    assert len(SETTINGS) == 125


def test_the_registry_matches_the_baseline():
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = {key: _normalized(spec) for key, spec in SETTINGS.items()}
    assert set(current) == set(baseline), (
        f"added: {sorted(set(current) - set(baseline))}, "
        f"removed: {sorted(set(baseline) - set(current))}"
    )
    changed = {
        key: (baseline[key], current[key])
        for key in baseline
        if baseline[key] != current[key]
    }
    assert changed == {}, f"the registry changed: {changed}"


def test_every_category_has_a_file():
    directory = Path(__file__).resolve().parents[1] / "settings"
    files = {path.stem for path in directory.glob("*.yaml")}
    assert files == {category.value for category in SettingCategory}


def test_every_category_keeps_its_order():
    """The menu shows the settings of a category in the file's order."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    for category in SettingCategory:
        keys = [key for key, spec in SETTINGS.items() if spec.category is category]
        expected = [key for key in baseline if baseline[key]["category"] == category.value]
        assert keys == expected, category.value


def test_the_menu_timeout_is_the_hard_cap():
    spec = SETTINGS["general.menu_timeout"]
    assert spec.default == MENU_TIMEOUT
    assert spec.max_value == MENU_TIMEOUT
    assert spec.is_duration


def test_the_registry_has_no_python_specs():
    """The specs live in YAML: the module holds no registry literal."""
    source = (Path(__file__).resolve().parents[1] / "core" / "settings.py").read_text(
        encoding="utf-8"
    )
    assert "_spec(" not in source


def _write(directory: Path, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "general.yaml").write_text(body, encoding="utf-8")


def test_the_loader_rejects_an_unknown_category(tmp_path):
    _write(tmp_path, "settings:\n  a.b:\n    category: nope\n    type: string\n    default: x\n")
    with pytest.raises(SettingsError, match="unknown category"):
        load_settings(tmp_path)


def test_the_loader_rejects_an_unknown_type(tmp_path):
    _write(tmp_path, "settings:\n  a.b:\n    category: general\n    type: nope\n    default: x\n")
    with pytest.raises(SettingsError, match="unknown type"):
        load_settings(tmp_path)


def test_the_loader_rejects_an_unknown_key(tmp_path):
    _write(
        tmp_path,
        "settings:\n  a.b:\n    category: general\n    type: string\n    default: x\n    nope: 1\n",
    )
    with pytest.raises(SettingsError, match="unknown keys"):
        load_settings(tmp_path)


def test_the_loader_rejects_a_duplicate_key(tmp_path):
    _write(tmp_path, "settings:\n  a.b:\n    category: general\n    type: string\n    default: x\n")
    (tmp_path / "other.yaml").write_text(
        "settings:\n  a.b:\n    category: general\n    type: string\n    default: y\n",
        encoding="utf-8",
    )
    with pytest.raises(SettingsError, match="declared twice"):
        load_settings(tmp_path)


def test_the_loader_rejects_an_inverted_range(tmp_path):
    _write(
        tmp_path,
        "settings:\n  a.b:\n    category: general\n    type: integer\n    default: 5\n"
        "    min: 10\n    max: 1\n",
    )
    with pytest.raises(SettingsError, match="min greater than max"):
        load_settings(tmp_path)


def test_the_loader_rejects_a_default_outside_the_range(tmp_path):
    _write(
        tmp_path,
        "settings:\n  a.b:\n    category: general\n    type: integer\n    default: 50\n"
        "    min: 1\n    max: 10\n",
    )
    with pytest.raises(SettingsError, match="above its max"):
        load_settings(tmp_path)


def test_the_loader_rejects_a_choice_without_choices(tmp_path):
    _write(
        tmp_path,
        "settings:\n  a.b:\n    category: general\n    type: choice\n    default: x\n",
    )
    with pytest.raises(SettingsError, match="needs a choice list"):
        load_settings(tmp_path)


def test_the_loader_rejects_an_empty_choice_list(tmp_path):
    _write(
        tmp_path,
        "settings:\n  a.b:\n    category: general\n    type: choice\n    default: x\n"
        "    choices: []\n",
    )
    with pytest.raises(SettingsError, match="non-empty choice list"):
        load_settings(tmp_path)


def test_the_loader_rejects_a_default_outside_the_choices(tmp_path):
    _write(
        tmp_path,
        "settings:\n  a.b:\n    category: general\n    type: choice\n    default: z\n"
        "    choices: [\"x\", \"y\"]\n",
    )
    with pytest.raises(SettingsError, match="unlisted choice"):
        load_settings(tmp_path)


def test_the_loader_rejects_a_duration_that_is_not_an_integer(tmp_path):
    _write(
        tmp_path,
        "settings:\n  a.b:\n    category: general\n    type: string\n    default: x\n"
        "    duration: true\n",
    )
    with pytest.raises(SettingsError, match="duration but not an integer"):
        load_settings(tmp_path)


def test_the_loader_rejects_a_file_without_settings(tmp_path):
    _write(tmp_path, "other: 1\n")
    with pytest.raises(SettingsError, match="no 'settings' mapping"):
        load_settings(tmp_path)


def test_the_loader_rejects_an_empty_directory(tmp_path):
    with pytest.raises(SettingsError, match="declares no setting"):
        load_settings(tmp_path)


def test_every_type_is_used():
    """The vocabulary of the loader is the vocabulary of the registry."""
    used = {spec.value_type for spec in SETTINGS.values()}
    assert used <= set(SettingType)
