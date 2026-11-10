"""Split the language catalogs by feature (the plan's P3).

The catalogs are one file per language today: a big YAML with every
feature's strings. This script splits them into one file per feature
(``language/<code>/<feature>.yaml``) and writes the reference hash, so the
test can prove the split lost nothing.

Usage::

    python scripts/split_catalog.py            # split + write the reference
    python scripts/split_catalog.py --check    # only check the reference

The mapping below is the whole migration: every top-level section of the
catalog lands in exactly one feature file. A section that is not listed is
an error (the script refuses to guess).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
LANGUAGE_DIR = ROOT / "language"
REFERENCE = ROOT / "tests" / "catalog_reference.json"

#: The feature files and the top-level sections they carry.
FEATURES: dict[str, tuple[str, ...]] = {
    "_meta": ("language_meta",),
    "about": (
        "about",
        "actions",
        "channelinfo",
        "emojiinfo",
        "help",
        "info",
        "info_channel",
        "info_emoji",
        "info_role",
        "info_server",
        "info_user",
        "serverinfo",
        "userinfo",
    ),
    "audit": ("audit", "errors", "log"),
    "moderation": (
        "anti_invite",
        "ban",
        "cleaner",
        "confirm",
        "kick",
        "limpar",
        "moderation",
        "mute",
        "trap",
        "unwarn",
        "warn",
        "warnings",
    ),
    "boost": ("boost", "boost_admin", "boost_panel"),
    "bump": (
        "bump",
        "bump_admin",
        "bump_admin_add_test",
        "bump_admin_reset_week",
        "bump_admin_test_leaderboard",
        "bump_leaderboard",
        "bump_stats",
        "bump_status",
        "bump_test",
        "bump_test_reminder",
    ),
    "suggestions": (
        "suggest",
        "suggest_admin",
        "suggest_admin_approve",
        "suggest_admin_reject",
        "suggest_send",
        "suggestions",
    ),
    "confessions": (
        "confess",
        "confess_ban",
        "confess_banned",
        "confess_reply",
        "confess_send",
        "confess_unban",
        "confessions",
    ),
    "invites": (
        "invited",
        "invites",
        "invites_admin",
        "invites_admin_blacklist",
        "invites_admin_bonus",
        "invites_admin_codes",
        "invites_admin_label",
        "invites_admin_reset",
        "invites_admin_sync",
        "invites_leaderboard",
        "invites_stats",
        "my_invite",
        "server_invites",
        "who_invited",
    ),
    "partnerships": (
        "partnerships",
        "partnerships_admin",
        "partnerships_admin_add",
        "partnerships_admin_audit",
        "partnerships_admin_clean",
        "partnerships_admin_edit",
        "partnerships_admin_invite",
        "partnerships_admin_list",
        "partnerships_admin_remove",
        "partnerships_renew",
    ),
    "tickets": ("tickets", "tickets_admin", "tickets_admin_panel"),
    "roleplay": (
        "roleplay",
        "roleplay_attack",
        "roleplay_bite",
        "roleplay_cuddle",
        "roleplay_dance",
        "roleplay_highfive",
        "roleplay_hug",
        "roleplay_kiss",
        "roleplay_pat",
        "roleplay_poke",
        "roleplay_punch",
        "roleplay_slap",
        "roleplay_stare",
    ),
    "starboard": ("starboard",),
    "events": ("events",),
    "birthdays": (
        "birthday",
        "birthday_clear",
        "birthday_list",
        "birthday_set",
        "birthdays",
    ),
    "reminders": (
        "reminders",
        "reminders_delete",
        "reminders_list",
        "reminders_save",
        "reminders_send",
    ),
    "colors": ("colors", "colors_my", "colors_panel"),
    "themes": ("themes",),
    "settings": ("settings",),
    "language": ("language",),
    "menu": (
        "broadcast",
        "broadcast_start",
        "broadcast_stop",
        "card",
        "debug",
        "general",
        "menu",
        "ping",
        "time_parser",
        "update",
        "updater",
        "variables",
    ),
}


def flatten(data: dict, prefix: str = "") -> dict:
    """Flatten a nested dict into dotted-key entries."""
    flat: dict = {}
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten(value, dotted))
        else:
            flat[dotted] = value
    return flat


def digest(flat: dict) -> str:
    """The hash of a flattened catalog (the reference of the split)."""
    raw = json.dumps(flat, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def keys_digest(flat: dict) -> str:
    """The hash of the flattened **keys** (the shape of the catalog).

    The keys hash is the promise of the split: the modular layout carries
    exactly the keys the single file carried. The content hash pins the
    copy too (the script's ``--check`` compares it).
    """
    raw = json.dumps(sorted(flat), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_catalog(path: Path) -> dict:
    """One catalog file (the old single-file format)."""
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: the catalog must be a mapping")
    return data


def deep_merge(base: dict, overlay: dict) -> dict:
    """Two mappings merged deeply (the overlay wins on the leaves)."""
    out = dict(base)
    for key, value in overlay.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = deep_merge(current, value)
            continue
        out[key] = value
    return out


def load_catalog(code: str) -> dict:
    """One catalog: the feature files (the modular layout) or the old file."""
    folder = LANGUAGE_DIR / code
    if folder.is_dir():
        merged: dict = {}
        for path in sorted(folder.glob("*.yaml")):
            merged = deep_merge(merged, read_catalog(path))
        return merged
    return read_catalog(LANGUAGE_DIR / f"{code}.yaml")


def split(code: str) -> dict:
    """Split one catalog into its feature files; returns the sections."""
    source = LANGUAGE_DIR / f"{code}.yaml"
    data = read_catalog(source)
    known = {key for keys in FEATURES.values() for key in keys}
    missing = sorted(set(data) - known)
    extra = sorted(known - set(data))
    if missing:
        raise SystemExit(f"{code}: sections without a feature file: {missing}")
    if extra:
        raise SystemExit(f"{code}: feature files without a section: {extra}")
    target = LANGUAGE_DIR / code
    target.mkdir(parents=True, exist_ok=True)
    for feature, keys in FEATURES.items():
        section = {key: data[key] for key in keys if key in data}
        if not section:
            continue
        path = target / f"{feature}.yaml"
        path.write_text(
            yaml.safe_dump(section, allow_unicode=True, sort_keys=False, width=4096),
            encoding="utf-8",
        )
    # The round trip must be lossless: the split is a mechanical move.
    merged: dict = {}
    for path in sorted(target.glob("*.yaml")):
        merged = deep_merge(merged, read_catalog(path))
    if flatten(merged) != flatten(data):
        raise SystemExit(f"{code}: the split changed the catalog")
    return merged


def reference_for(code: str) -> dict[str, str]:
    """The reference of one catalog: the hash of its keys and of its copy."""
    flat = flatten(load_catalog(code))
    return {"keys": keys_digest(flat), "catalog": digest(flat)}


def main(argv: list[str]) -> int:
    """Split the catalogs (or only check the reference)."""
    if "--check" in argv:
        reference = json.loads(REFERENCE.read_text(encoding="utf-8"))
        failed = False
        for code in sorted(reference):
            found = reference_for(code)
            for what, value in found.items():
                expected = reference[code].get(what)
                if value != expected:
                    print(f"{code}: the {what} changed ({value} != {expected})")
                    failed = True
        return 1 if failed else 0
    codes = sorted({path.stem for path in LANGUAGE_DIR.glob("*.yaml")})
    codes += sorted(path.name for path in LANGUAGE_DIR.iterdir() if path.is_dir())
    reference = {}
    for code in sorted(set(codes)):
        if (LANGUAGE_DIR / f"{code}.yaml").is_file() and not (
            LANGUAGE_DIR / code
        ).is_dir():
            split(code)
        reference[code] = reference_for(code)
        print(f"{code}: {len(reference[code])} hashes written")
    REFERENCE.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE.write_text(
        json.dumps(reference, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
