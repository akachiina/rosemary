# AGENTS.md

Rosemary — a modular, multilingual Discord bot for multiple servers, built on **py-cord 2.8.1**. All user-facing strings are Portuguese (pt-BR) or English (en-US), resolved via i18n keys. No embeds anywhere — everything visual is Components V2.

## Setup / run

- This directory is its own git repo (`main`). It was split out of the old single-guild bot — **ignore the parent repo's `cogs/`, `utils/`, `config/`, `bot.py`** (legacy).
- `requirements.txt` = runtime deps only (py-cord, yaml, aiosqlite, aiohttp, dotenv, PyNaCl). `rosemary/venv` is runtime-only (no pytest/ruff there); dev tools live in the parent `.venv`.
- `pyproject.toml` (ruff + pytest config) lives in the **parent** repo and is NOT inside this repo.
- Token comes from `.env` (copy `.env.example`); `.env`, `data/`, `venv/`, `*.log` are git-ignored.
- Run: `python bot.py` from inside this directory, or `python -m rosemary` from the parent. `bot.py`/`__main__.py` bootstrap `sys.path` with the repo root (`# noqa: E402` on the imports after — keep that pattern). Paths to `data/`, `language/`, `.env`, `theme.yaml` are resolved from `__file__`, never CWD.

## Verify

- Tests **must run from inside this dir** (`../.venv/bin/python -m pytest tests`) — `test_customize.py` reads `language/...` via CWD-relative paths and fails from the parent. Full suite (~300 tests) runs in seconds.
- Lint **must run from the parent** (`./.venv/bin/ruff check rosemary`) — config is in parent `pyproject.toml` (line-length 100, py311, selects E/F/W/I/UP/B/SIM, ignores B008), and the path argument only resolves from there.
- `asyncio_mode = "auto"` is set: async tests need no decorator. `tests/conftest.py` is sys.path bootstrap only (no fixtures); each test file builds its own fakes.

## Architecture

- `bot.py` = `RosemaryBot` (single entrypoint) + `main()`. `_setup()` runs once in `on_ready`: registers cogs, loads `language/*.yaml` + `ui/theme.yaml`, snapshots `_command_base_keys` (English decorator names) for per-guild re-localization.
- Cogs are registered explicitly in `_setup()` (no auto-discovery), each with `def setup(bot)`. Commands sync **per guild, never globally** — `on_guild_join()` syncs newly joined servers; without it a new server gets no slash commands until restart.
- i18n (`core/i18n.py`): YAML sections flatten to dotted keys (`about.title`). `language_meta` (name+flag) is metadata for `/language` choices, **not** a translation key. **en↔pt key parity is enforced by `test_i18n.py`** — always add new keys to both catalogs.
- **List-valued catalog keys (e.g. `time_parser.*`) must be read via `translator.raw()`, never `t()`** — `t()` calls `.format()` on the template and crashes on lists.
- **Emojis are never hardcoded**: `t()` auto-injects every `emojis:` entry from `ui/theme.yaml` as a format placeholder, so any `{key}` in a catalog resolves automatically; explicit kwargs override. Colors come from `theme.color("<name>")` (names defined in `ui/theme.yaml` `colors:`/`styles:`).
- Per-guild command localization: `_sync_guild_commands()` mutates the shared `cmd.name`/`cmd.description` from `<cmd>.command.name`/`.description` then re-syncs that guild. Runs on `on_ready` (all guilds) and after `/language`.
- Persistence is one JSON file per guild: `data/<guild_id>/<filename>` via `core/storage.py` `GuildStorage`. Defaults merged on read (`use_defaults=False` skips the `language` default). Writes are atomic (temp + rename). `data/` is git-ignored.
- Logging: `core/logging.py` writes `bot.log` (git-ignored) + colored console. Actions should send channel logs through `core/debug.send_channel_log(...)` (no-ops unless `logging.enabled` + channel configured).

## Mentions (`core/mentions.py`)

- Every `CardSpec` declares `mention_default`; guilds override per card in `/customize` (persisted in `mentions.json` via `MentionStore`, missing key = spec default, no migration ever needed). Modes: `none` / `single` / `winner_auto` / `role` / `all`.
- **`<@id>` inside Components V2 `TextDisplay` still pings** — every public `channel.send`/`ctx.respond` carrying mentions must pass an explicit `allowed_mentions` from `mentions_for(...)`. Ephemeral responses don't need it.
- `winner_auto` pings one user only when `source="auto"` (weekly post); manual `/bump_leaderboard` passes `source="command"` → silent. `single`/`role` ping only the passed candidate ids.
- `send_channel_log(..., card_key=..., mention_user_ids=[...])` resolves the log card's policy (log cards default to `none`); calls without `card_key` never ping.
- Adding a customizable card = `CardSpec` in `core/card_specs.py` + `card.<key>.title`/`.placeholders` in BOTH catalogs (**enforced by `test_customize.py`**) + a sensible `mention_default`.

## Settings system (`core/settings.py`)

- `SETTINGS` registry is the single source of truth for `/settings`; every spec has a `SettingCategory` and `SettingType` (STRING/INTEGER/BOOLEAN/CHOICE/CHANNEL/ROLE) plus optional `min_value`/`max_value`/`choices`/`is_duration`.
- **Duration settings** (`is_duration=True`, e.g. `bump.cooldown`) store **seconds** but are edited/displayed via `core/time_parser.TimeParser` (`"2h"`, `"90m"`, `"1d"`). `coerce_value()`/`format_value()` handle them; keep that consistent.
- Adding a setting = add a `SettingSpec` + matching `settings.<key>.label`/`.description` (and `.choices.*`) in BOTH catalogs; the UI is driven entirely by registry + catalogs, no per-setting UI code.

## Feature: Boost roles + Bump (Disboard)

- `cogs/boost_roles.py` + `core/boost_roles.py` (`BoostRoleStore`): boost roles, invites, member panels. `cogs/bump_reminder.py` + `cogs/bump_leaderboard.py` + `core/bump.py` (`BumpStore`): weekly Disboard bump tracking. **Only Disboard is supported** (hardcoded bot ID in `bump_reminder.py`; mention it in copy).
- These stores use `GuildStorage(use_defaults=False)` with their own filename to avoid leaking the `language` default.

## Ported features (from the legacy single-guild bot)

- `cogs/invites.py` + `core/invites.py` (`InviteStore`): attribution by invite-uses diff, regular/bonus/fake/left counters (`total = regular - left - fake + bonus`), ranking, personal links, blacklist, labels. `{inviter}` placeholder available in welcome cards.
- `cogs/tickets.py` + `core/tickets.py`: panel with 4 fixed types, close/transcript/reopen/delete buttons (persistent views re-registered in `start()`), text transcripts to the log channel. Ticket must be closed before delete.
- `cogs/partnerships.py` + `core/partnerships.py`: ads, hourly renewal loop (warn then remove), rep self-renew with repost, audit (read-only) + orphan cleanup.
- Small: `cogs/utility.py` (`/ping`, `/serverinfo`), `cogs/cleaner.py` (`/limpar` keyword purge), `cogs/anti_invite.py` (external-link filter), `cogs/reminders.py` + `cogs/broadcast.py` (mass-DM, admin, **disabled by default**), `cogs/updater.py` (`/update`: clean-tree check + data backup + reset + restart, disabled by default).
- Rule for every port: settings in `core/settings.py` + i18n in BOTH catalogs + `CardSpec`s with `mention_default` + `allowed_mentions` on public sends + `send_channel_log(card_key=...)` + tests with the real catalogs and `caplog` asserting zero format warnings.

## Gotchas

- `cogs/moderation.py` **intentionally omits `from __future__ import annotations`** — py-cord inspects slash-command annotations with `inspect.signature` and stringified annotations break option parsing. Do not add it back. (Other modules do use it.) `tests/test_commands.py` guards this.
- **Moderation durations are `timedelta | None`**: ban/kick/warn always pass `None`. Never call `TimeParser.format_duration(duration)` unguarded outside mute-only branches, and `mute.success` requires `{duration}` — use the dedicated branch in `_execute_action`. `tests/test_moderation.py` runs all 4 actions × notify/silent against the real catalogs with `caplog` asserting zero format warnings.
- `/settings` menu (`ui/settings_menu.py`) renders ROLE via role-select, CHANNEL via channel-select, BOOLEAN via buttons, INTEGER/STRING via modal, CHOICE via select. Keep that mapping when adding types.
- Boost invite DMs are three messages: plain text, then a buttons-only `DesignerView` (persistent, `custom_id` unique per invite — never send that view with `content=`, V2 forbids it; `_edit` swaps in a TextDisplay instead), then the role-preview card. Other UI uses `DesignerView`/`designer_container` (Components V2). No embeds.
- Never echo or commit the token (`.env`), and keep `data/` and `venv/` out of git.
