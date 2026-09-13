# AGENTS.md

Rosemary — a modular, multilingual Discord bot for multiple servers, built on **py-cord 2.8.1**. All user-facing strings are Portuguese (pt-BR) or English (en-US), resolved via i18n keys. No embeds anywhere — everything visual is Components V2.

## Setup / run

- This directory is its own git repo (`main`). It was split out of the old single-guild bot — **ignore the parent repo's `cogs/`, `utils/`, `config/`, `bot.py`** (legacy).
- `requirements.txt` = runtime deps only (py-cord, yaml, aiosqlite, aiohttp, dotenv, PyNaCl). `rosemary/venv` is runtime-only (no pytest/ruff there); dev tools live in the parent `.venv`.
- `pyproject.toml` (ruff + pytest config) lives in the **parent** repo and is NOT inside this repo.
- Token comes from `.env` (copy `.env.example`); `.env`, `data/`, `venv/`, `*.log` are git-ignored.
- Run: `python bot.py` from inside this directory, or `python -m rosemary` from the parent. `bot.py`/`__main__.py` bootstrap `sys.path` with the repo root (`# noqa: E402` on the imports after — keep that pattern). Paths to `data/`, `language/`, `.env`, `theme.yaml` are resolved from `__file__`, never CWD.

## Verify

- Tests **must run from inside this dir** (`../.venv/bin/python -m pytest tests`) — several test files (`test_ui_polish.py`, `test_i18n.py`) read `language/...` via CWD-relative paths and fail from the parent. Full suite runs in seconds.
- Lint **must run from the parent** (`./.venv/bin/ruff check rosemary`) — config is in parent `pyproject.toml` (line-length 100, py311, selects E/F/W/I/UP/B/SIM, ignores B008), and the path argument only resolves from there.
- `asyncio_mode = "auto"` is set: async tests need no decorator. `tests/conftest.py` is sys.path bootstrap only (no fixtures); each test file builds its own fakes.

## Architecture

- `bot.py` = `RosemaryBot` (single entrypoint) + `main()`. `_setup()` runs once in `on_ready`: registers cogs, loads `language/*.yaml` + `ui/theme.yaml`, snapshots `_command_base_keys` (English decorator names) for per-guild re-localization.
- Cogs are registered explicitly in `_setup()` (no auto-discovery) via `add_cog(Class(bot))` — there are no `def setup(bot)` extension hooks. Commands sync **per guild, never globally** — `on_guild_join()` syncs newly joined servers; without it a new server gets no slash commands until restart. `/themes` (`/temas`) is the theme manager: import/select/export/remove/reload.
- i18n (`core/i18n.py`): YAML sections flatten to dotted keys (`about.title`). `language_meta` (name+flag) is metadata for `/language` choices, **not** a translation key. **en↔pt key parity is enforced by `test_i18n.py`** — always add new keys to both catalogs.
- **List-valued catalog keys (e.g. `time_parser.*`) must be read via `translator.raw()`, never `t()`** — `t()` calls `.format()` on the template and crashes on lists.
- **YAML 1.1 parses unquoted `on`/`off`/`yes`/`no`/`true`/`false` keys as booleans** — quote them in the catalogs (`"on": Ligados`); a boolean key flattens under `True`/`False` so `t()` falls back to the raw key. Swept by `test_i18n.py::test_no_boolean_keys`.
- **Emojis are never hardcoded**: `t()` auto-injects every `emojis:` entry from `ui/theme.yaml` as a format placeholder, so any `{key}` in a catalog resolves automatically; explicit kwargs override. Colors come from `theme.color("<name>")` (names defined in `ui/theme.yaml` `colors:`/`styles:`). Emojis passed as component `emoji=` must be RGI-valid — Discord 400s the whole payload (50035 `emoji.name: Invalid emoji`) otherwise; `theme.emojis` is swept by `test_theme.py::test_every_theme_emoji_is_discord_valid` (`check` is ✅, not text `✓`).
- Per-guild command localization: `_sync_guild_commands()` mutates the shared `cmd.name`/`cmd.description` from `<cmd>.command.name`/`.description` then re-syncs that guild. Runs on `on_ready` (all guilds) and after `/language`. Names are validated against `_COMMAND_NAME_RE` with fallback to the base name — a missing key would otherwise 400 the whole bulk sync (`test_guild_join.py` sweeps every command × both languages).
- Persistence is one JSON file per guild: `data/<guild_id>/<filename>` via `core/storage.py` `GuildStorage`. Defaults merged on read (`use_defaults=False` skips the `language` default). Writes are atomic (temp + rename). `data/` is git-ignored.
- Logging: `core/logging.py` writes `bot.log` (git-ignored) + colored console. Actions should send channel logs through `core/debug.send_channel_log(...)` (no-ops unless `logging.enabled` + channel configured).
- Boot extras in `_setup()`: `ThemeStore` (`core/themes.py`), `preload_themes(bot)` (per-guild theme snapshots for the synchronous `theme_for`), then `card_actions.sync_all_guilds(bot)` (re-register persistent `cardact:` dispatch views) and `panels.repaint_all(bot)` — order matters, repaints need cogs' stores ready.

## Mentions (`core/mentions.py`)

- **Mentions are content**: the position of a ping is wherever `{@name}` (or legacy `{name}` for a mention variable) appears in the card text. The renderer collects the `<@id>`/`<@&id>` tokens the resolved text actually contains and builds `AllowedMentions` from them — never pass `allowed_mentions` by hand for card sends.
- Per-card pings toggle in the guild's **theme file** (`pings:` section, on/off per card key; missing key = `pings_default(key)` = spec's `mention_default != "none"`; `core/mentions.py:theme_pings`). Entry points: `allowed_for_ids` (candidates by id), `allowed_for_text` (tokens in already-resolved text), `allowed_for_document` (tokens a document resolves to), `render_card_message` in `core/card_service.py` (view + allowed in one call). `silent=True` forces no ping (manual `/bump_leaderboard` winner).
- `send_channel_log(..., card_key=..., mention_user_ids=[...])` resolves pings from the description text plus the passed ids; log cards default to off; calls without `card_key` never ping.
- Adding a customizable card = `CardSpec` in `core/card_specs.py` + `card.<key>.title` in BOTH catalogs + a sensible `mention_default` + `variables` contract in `VARIABLES_BY_KEY` (send site must pass exactly those; **enforced by `test_variables.py`**). Placeholder labels live in `variables.<name>.label/.description` (BOTH catalogs); samples in `core/variables.py`. Guilds customize the card through a **theme file's `cards:` section** (see Themes below) — there is no in-Discord editor anymore.

## Themes (`core/themes.py` + `cogs/themes.py`)

- The old `/personalizar` card editor was **removed** (too complex for Discord's UI); customization is now **theme files**. A theme = one YAML carrying `colors`/`emojis`/`markdown`/`styles` (same shape as `ui/theme.yaml`) + optional `cards:` and `pings:` sections.
- **Locations**: `themes/` (global, repo) + `data/<id>/themes/` (per-guild imports) + `themes.json` selection. The active theme provides emoji/color resolution for **every** render (`theme_for(bot, guild_id)` — synchronous from the boot snapshot; falls back to the built-in `bot.theme`).
- **`cards:` values are raw Discord Components V2 JSON** (`type: 17` containers, `accent_color`, `components`...) converted at load by `core/v2_convert.py` (names or type numbers, string shortcut for one-text cards). Full Discord limits validated at load/import (40 components, depth 5, 4000 chars); an invalid themed card **never breaks a send** — it logs and falls back to the built-in default.
- Card keys are dotted paths (`about.card`, `events.welcome`, `tickets.panel`). Resolution: themed override → feature-registered default builder (`set_default_builder`) → catalog seed. `maybe_view`/`maybe_flat_text`/`text_or`/`render_card_message` all read the active theme.
- **Every card that renders a `CardSpec` must trace its path** — the whole point of `debug.card_paths` is discovery for theme customization, and ephemeral cards (`/invites`, `/quem_convidou`, `cleaner.confirm`, `updater.confirm`) are exactly what admins look up. Route through `render_card_message`; sites that build the themed view themselves (confirm cards with button rows) call `card_service.trace_card_path(bot, guild_id, key)` explicitly. True non-cards stay silent: built-in menus (`/settings`, `/temas`, boost menus, lists), internal panel repaints (a theme change would trace N times), DMs (moderation/partnership/boost direct messages), and `bump.leaderboard` (pings policy is `winner_auto` — winner only — while its CONTENT mentions the whole top-10, so content-derived mentions would over-ping; it uses `allowed_for_ids(user_ids=[winner_id])`).
- **`cardact:` action buttons**: theme buttons with `cardact:<action>:<arg>` labels do real bot actions (`ACTIONS` in `core/card_actions.py`: `open_ticket`, `dismiss`). Custom ids are **deterministic hashes** of `(card_key, block id, label, url, action)` — stable across loads/restarts so persistent dispatch views match posted messages; button clicks die silently when they diverge (regression: `test_panels.py`).
- **Panel repaint registry** (`core/panels.py`): persistent panels (tickets panel) register a repaint callback + the setting keys they care about. `on_theme_changed` (fired by `/themes` and reload) repaints panels **and** re-syncs `cardact` dispatch; `on_setting_changed` repaints only interested panels; `repaint_all` at boot. Repaint = `get_partial_message().edit()` with repost fallback.
- `/themes` panel: import (attach `.yaml`, validated before writing), select, export, remove (imported only), reset to built-in, **reload files** (`ThemeStore.invalidate_guild` — re-reads edited YAMLs without restart). Any panel change fires `on_theme_changed`.
- Setting `debug.card_paths` (Logging category): when on, every card send posts `card.<key> ← theme "X"` to the log channel (never pings; `card_service._trace_card_path`).

## Settings system (`core/settings.py`)

- `SETTINGS` registry is the single source of truth for `/settings`; every spec has a `SettingCategory` and `SettingType` (STRING/INTEGER/BOOLEAN/CHOICE/CHANNEL/ROLE) plus optional `min_value`/`max_value`/`choices`/`is_duration`.
- **Duration settings** (`is_duration=True`, e.g. `bump.cooldown`) store **seconds** but are edited/displayed via `core/time_parser.TimeParser` (`"2h"`, `"90m"`, `"1d"`). `coerce_value()`/`format_value()` handle them; keep that consistent.
- Adding a setting = add a `SettingSpec` + matching `settings.<key>.label`/`.description` (and `.choices.*`) in BOTH catalogs; the UI is driven entirely by registry + catalogs, no per-setting UI code. **No test enforces settings-label parity** — after adding specs, resolve every `settings.<key>.label/.description` in both languages or `/settings` renders raw keys.
- `/settings` shows `ITEMS_PER_PAGE = 7` settings per page (Discord 40-component cap enforced by test); registry order = display order, so keep essentials first (locked by `test_bump_essentials_come_first`-style tests). Nav row is always `[Close, Prev?, Next?]` — Close pinned at index 0.

## Feature: Boost roles + Bump (Disboard)

- `cogs/boost_roles.py` + `core/boost_roles.py` (`BoostRoleStore`): boost roles, invites, member panels. `cogs/bump_reminder.py` + `cogs/bump_leaderboard.py` + `core/bump.py` (`BumpStore`): weekly Disboard bump tracking. **Only Disboard is supported** (hardcoded bot ID in `bump_reminder.py`; mention it in copy).
- `bump.schedule.*`: fixed daily open/close (defaults 06:00/23:00, overnight ranges OK) with open/close messages. Lock ownership (`schedule` vs `camping`) lives in the bump store — the schedule always wins over anti-camping unlocks; enforcement is transition-only via a minute loop.
- These stores use `GuildStorage(use_defaults=False)` with their own filename to avoid leaking the `language` default.

## Ported features (from the legacy single-guild bot)

- `cogs/invites.py` + `core/invites.py` (`InviteStore`): attribution by invite-uses diff, regular/bonus/fake/left counters (`total = regular - left - fake + bonus`), ranking, personal links, blacklist, labels. `{inviter}` placeholder available in welcome cards. **Attribution races the cache**: `on_member_join` must diff the pre-join `self._cache` against the fresh snapshot BEFORE overwriting the cache — refreshing first made every join `unknown` (regression: `test_ui_polish.py`).
- `cogs/tickets.py` + `core/tickets.py`: panel with 4 fixed types (`TICKET_TYPES`), close/transcript/reopen/delete buttons (persistent views re-registered in `start()`), text transcripts to the log channel. Ticket must be closed before delete. Panel posts/repaints through `core/panels.py` (theme change or `tickets.*` settings trigger a repaint); the type select is hidden when the active theme already renders `open_ticket` buttons.
- `cogs/partnerships.py` + `core/partnerships.py`: ads, hourly renewal loop (warn then remove), rep self-renew with repost, audit (read-only) + orphan cleanup.
- Small: `cogs/utility.py` (`/ping`, `/serverinfo`), `cogs/cleaner.py` (`/limpar` keyword purge), `cogs/anti_invite.py` (external-link filter), `cogs/reminders.py` + `cogs/broadcast.py` (mass-DM, admin, **disabled by default**), `cogs/updater.py` (`/update`: two channels — `stable` follows latest `vX.Y.Z` tag, `git` follows `origin/<branch>`; manual use is always admin-allowed, `updater.enabled` gates only the auto-update loop with `updater.check_interval_hours`).
- Rule for every port: settings in `core/settings.py` + i18n in BOTH catalogs + `CardSpec`s with `mention_default` + `allowed_mentions` on public sends + `send_channel_log(card_key=...)` + tests with the real catalogs and `caplog` asserting zero format warnings.

## Visual standards (cards & logs)

- Log bodies use the **compact field format** (`**Alvo:** @x` one line per field), matching the settings-change card — never the old `**Label:**\nvalue` double-line shape. Unknown fields render a translated placeholder (`invites.unknown`), never a bare `-`. Titles carry a theme emoji token (`{ban} Banimento`).
- **YAML quoting pitfall**: single-quoted strings keep `\n` literal — multi-line templates must use **double quotes** (real newline) in the catalogs. Regression: `test_ui_polish.py::test_log_templates_use_real_newlines`.
- Event cards (welcome/leave/ban) default layout: container (brand/warning/danger) → **section with the member's avatar as thumbnail accessory** (`{user_avatar}`; in all three contracts), emoji `#` title, body, divider, `-# {server} · #{count}` footer. Built by `cogs/welcome.py:default_event_document`; sends go through `render_card_message` so themes can override them like any card.

## Gotchas

- Every interaction must be ACKed (`defer()`/`send_message`/modal) before slow network work — past 3s Discord shows "the application did not respond" even when the task later succeeds. Guard with `if not interaction.response.is_done()`.
- py-cord's default `BaseView.on_error` only prints to stderr (never reaches `bot.log`). `RosemaryBot.on_view_error` does `log.exception` with the item custom_id and ACKs the interaction with `general.unknown_error`. Never swallow `discord.HTTPException` around `interaction.edit()` with a bare `return` — a 400 (e.g. invalid emoji) looks exactly like "click does nothing".
- Offline repros of menus/editor can pass while live fails: payload **building** is client-side; Discord rejects bad payloads only on the HTTP call (`interaction.edit`). When a click "does nothing", check `bot.log` first. Recurring causes of "click does nothing": a raw `discord.ui.Select`/`Button` added without a bound callback (use the `MenuView.make_*` factories), and `cardact:` custom_id divergence between the posted message and the boot-registered dispatch views (ids must be content-derived, see Themes).
- Pre-restart ephemeral panels look alive but their live view is gone — py-cord silently no-ops the click. Re-send or repaint them on boot (`panels.repaint_all`). py-cord's `add_view` only indexes ids that exist as real view children — a dispatch-only fallback view needs stub components.
- Never `disable_all_items()` before `rerender()`: `prepare()` clears and rebuilds every component, wiping the flags. Model terminal states in `build_items()` instead — `ui/confirm.py:ConfirmView` does this (full card content plus result, button rows dropped). Reuse it for new confirm dialogs.
- Theme-store caching: themes are cached per `(guild_id, name)`; **every** code path that edits theme files must call `ThemeStore.invalidate_guild` (or write through the store) or selection returns the stale object. `/themes` exposes this as the reload button.
- Attribution/debug discipline: when a live symptom shows *stale content* (old theme, missing inviter), suspect a cache refreshed before its consumer reads it — both the invite `unknown` bug and the theme-reselect bug were "cache written too early". Prove it with an offline repro before patching.
- `cogs/moderation.py` **intentionally omits `from __future__ import annotations`** — py-cord inspects slash-command annotations with `inspect.signature` and stringified annotations break option parsing. Do not add it back. (Other modules do use it.) `tests/test_commands.py` guards this.
- **Moderation durations are `timedelta | None`**: ban/kick/warn always pass `None`. Never call `TimeParser.format_duration(duration)` unguarded outside mute-only branches, and `mute.success` requires `{duration}` — use the dedicated branch in `_execute_action`. `tests/test_moderation.py` runs all 4 actions × notify/silent against the real catalogs with `caplog` asserting zero format warnings.
- `/settings` menu (`ui/settings_menu.py`) renders ROLE via role-select, CHANNEL via channel-select (all types), CATEGORY via channel-select filtered to `ChannelType.category`, BOOLEAN via buttons, INTEGER/STRING via modal, CHOICE via select. Keep that mapping when adding types.
- Boost invite DMs are three messages: plain text, then a buttons-only `DesignerView` (persistent, `custom_id` unique per invite — never send that view with `content=`, V2 forbids it; `_edit` swaps in a TextDisplay instead), then the role-preview card. Other UI uses `DesignerView`/`designer_container` (Components V2). No embeds.
- Never echo or commit the token (`.env`), and keep `data/` and `venv/` out of git.
