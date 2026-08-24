# AGENTS.md

Rosemary — a modular, multilingual Discord bot for multiple servers, built on **py-cord 2.8.1**. All user-facing strings are Portuguese (pt-BR) or English (en-US), resolved via i18n keys. No embeds anywhere — everything visual is Components V2.

## Setup / run

- This directory is its own git repo (`main`). It was split out of the old single-guild bot — **ignore the parent repo's `cogs/`, `utils/`, `config/`, `bot.py`** (legacy).
- `requirements.txt` = runtime deps only (py-cord, yaml, aiosqlite, aiohttp, dotenv, PyNaCl).
- `pyproject.toml` (ruff + pytest config) lives in the **parent** repo and is NOT inside this repo; `pytest`/`ruff` are installed in the parent `.venv`, not in `rosemary/venv`. Run lint/tests from the parent as `../.venv/bin/...`, or add your own `pyproject.toml` here.
- Token comes from `.env` (copy `.env.example`); `rosemary/.env` is git-ignored.
- Run: `python bot.py` from inside this directory. `bot.py`/`__main__.py` bootstrap `sys.path` with the repo root, so it also works as `python -m rosemary` from the parent. Paths to `data/`, `language/`, `.env`, `theme.yaml` are resolved from `__file__`, never CWD.

## Verify

- `../.venv/bin/python -m pytest rosemary/tests` — 88 tests, all pass. `tests/` inside this dir also runs directly (`pytest tests/`).
- `../.venv/bin/ruff check rosemary` — clean. Config in parent `pyproject.toml`: line-length 100, py311, selects E/F/W/I/UP/B/SIM, ignores B008.
- Config/bootstrap quirks: `bot.py`/`__main__.py`/`tests/conftest.py` insert `sys.path` entries with `# noqa: E402` after the bootstrap — keep that pattern; do not "clean up" the imports to the top.

## Architecture

- `bot.py` = `RosemaryBot` (single entrypoint) + `main()`. `RosemaryBot._setup()` runs once in `on_ready`: registers cogs, loads `language/*.yaml` + `ui/theme.yaml`, snapshots `_command_base_keys` (English decorator names) for per-guild re-localization.
- Cogs are registered explicitly in `_setup()` (no auto-discovery), each with `def setup(bot)`.
- i18n (`core/i18n.py`): YAML sections flatten to dotted keys (`about.title`). `language_meta` (name+flag) is metadata for `/language` choices, **not** a translation key. **en↔pt key parity is enforced by `test_i18n.py`** — always add new keys to both catalogs.
- **Emojis are never hardcoded**: `t()` auto-injects every `emojis:` entry from `ui/theme.yaml` as a format placeholder, so any `{key}` in a catalog resolves automatically; explicit kwargs override. Colors come from `theme.color("<name>")` (names defined in `ui/theme.yaml` `colors:`/`styles:`).
- Per-guild command localization: `reapply_command_localization()` mutates the shared `cmd.name`/`cmd.description` from `<cmd>.command.name`/`.description` then re-syncs every guild. Runs on `on_ready` and after `/language`.
- Persistence is one JSON file per guild: `data/<guild_id>/<filename>` via `core/storage.py` `GuildStorage`. Defaults merged on read (`use_defaults=False` skips the `language` default). Writes are atomic (temp + rename). `data/` is git-ignored.
- Logging: `core/logging.py` writes `bot.log` (git-ignored) + colored console. Actions should send channel logs through `core/debug.send_channel_log(...)` (no-ops unless `logging.enabled` + channel configured).

## Settings system (`core/settings.py`)

- `SETTINGS` registry is the single source of truth for `/settings`; every spec has a `SettingCategory` and `SettingType` (STRING/INTEGER/BOOLEAN/CHOICE/CHANNEL/ROLE) plus optional `min_value`/`max_value`/`choices`/`is_duration`.
- **Duration settings** (`is_duration=True`, e.g. `bump.cooldown`) store **seconds** but are edited/displayed via `core/time_parser.TimeParser` (`"2h"`, `"90m"`, `"1d"`). `coerce_value()`/`format_value()` handle them; keep that consistent.
- Adding a setting = add a `SettingSpec` + matching `settings.<key>.label`/`.description` (and `.choices.*`) in BOTH catalogs; the UI is driven entirely by registry + catalogs, no per-setting UI code.

## Feature: Boost roles + Bump (Disboard)

- `cogs/boost_roles.py` + `core/boost_roles.py` (`BoostRoleStore`): boost roles, invites, member panels. `cogs/bump_reminder.py` + `cogs/bump_leaderboard.py` + `core/bump.py` (`BumpStore`): weekly Disboard bump tracking. **Only Disboard is supported** (hardcoded bot ID in `bump_reminder.py`; mention it in copy).
- These stores use `GuildStorage(use_defaults=False)` with their own filename to avoid leaking the `language` default.

## Gotchas

- `cogs/moderation.py` **intentionally omits `from __future__ import annotations`** — py-cord inspects slash-command annotations with `inspect.signature` and stringified annotations break option parsing. Do not add it back. (Other modules do use it.)
- `/settings` menu (`ui/settings_menu.py`) renders ROLE via role-select, CHANNEL via channel-select, BOOLEAN via buttons, INTEGER/STRING via modal, CHOICE via select. Keep that mapping when adding types.
- Boost invite DMs are three messages: plain text, then a buttons-only `DesignerView` (persistent, `custom_id` unique per invite — never send that view with `content=`, V2 forbids it; `_edit` swaps in a TextDisplay instead), then the role-preview card. Other UI uses `DesignerView`/`designer_container` (Components V2). No embeds.
- Never echo or commit the token (`.env`), and keep `data/` and `venv/` out of git.
