# AGENTS.md

Rosemary: modular, multilingual Discord bot (multi-server) on **py-cord 2.8.1**.
User-facing strings are pt-BR / en-US via i18n keys. Built-in visual is
Components V2 everywhere; classic embeds exist only as a per-card theme alternative.

## Setup / run

- This directory is its own git repo (`main`). **Ignore the parent repo's
  `cogs/`, `utils/`, `config/`, `bot.py`** (legacy single-guild bot).
- `requirements.txt` = runtime deps only. `rosemary/venv` is runtime-only
  (no pytest/ruff); dev tools live in the parent `.venv`.
- `pyproject.toml` (ruff + pytest config) lives in the **parent** repo, not here.
- Token from `.env` (copy `.env.example`); never echo or commit it.
  `.env`, `data/`, `venv/`, `*.log` are git-ignored.
- Run `python bot.py` from inside this dir, or `python -m rosemary` from the parent.
  Both bootstrap `sys.path` with the repo root (`# noqa: E402` imports after: keep).
  Paths to `data/`, `language/`, `.env`, themes resolve from `__file__`, never CWD.
- No AI attribution footer in commit messages.

## Verify

- Tests **must run from inside this dir** (`../.venv/bin/python -m pytest tests`):
  several files read `language/...` via CWD-relative paths and fail from the parent.
- Single test: `../.venv/bin/python -m pytest tests/test_<name>.py -q` (same dir).
- Lint **must run from the parent** (`./.venv/bin/ruff check rosemary`):
  config is in parent `pyproject.toml` (line-length 100, py311, E/F/W/I/UP/B/SIM).
- `asyncio_mode = "auto"`: async tests need no decorator.
  `tests/conftest.py` is sys.path bootstrap only; each test file builds its own fakes.

## Architecture

- `bot.py` = `RosemaryBot` + `main()`. `_setup()` runs once in `on_ready`:
  registers cogs, loads `language/*.yaml` + `themes/default.yaml`, snapshots
  `_command_base_keys` for per-guild re-localization.
- Cogs registered explicitly in `_setup()` via `add_cog(Class(bot))`: no
  auto-discovery, no `def setup(bot)` hooks. Cogs needing background work define
  async `start()` (called explicitly; py-cord never calls `cog_load` here).
- Commands sync **per guild, never globally**; `on_guild_join()` syncs new servers.
  Localized `cmd.name` must match `_COMMAND_NAME_RE` (lowercase); invalid falls back
  to the base name or the whole bulk sync 400s (swept by `test_guild_join.py`).
- Slash commands live in `SlashCommandGroup`s (`/roleplay beijar`), never flat
  with underscores. Single-cog groups are class attributes (birthday pattern);
  the cross-cog `bump` group is module-level, held by NEITHER cog, registered
  explicitly in `_setup()` + bound per subcommand via `rebind_shared_group_commands`
  (py-cord copies class-attr groups at class-def time, freezing later attachments,
  and only the `.cog` assignment re-runs option validation, which is what skips
  the leading `self`/`ctx` params).
- Subcommand localization reuses keys: `{group}_{sub}.command` (legacy flat key)
  then bare `{sub}.command`; group names localize like top-level names. New group
  = group `command.name`/`.description` in BOTH catalogs + snapshot in
  `_subcommand_base_keys` (automatic). Renamed setting/card keys need lazy
  migration (`_RENAMED_KEYS` in settings, `_RENAMED_CARDS`/`_RENAMED_PINGS` for themes).
- Persistence: one JSON file per guild, `data/<guild_id>/<filename>` via
  `GuildStorage`. Defaults merged on read (`use_defaults=False` skips even the
  `language` default). Writes are atomic (temp + rename).
- `SETTINGS` registry (`core/settings.py`) drives `/settings`; no per-setting UI
  code. New setting = `SettingSpec` + `settings.<key>.label`/`.description`
  (+ `.choices.*`) in BOTH catalogs. **No test enforces label parity**: resolve
  every label or the UI renders raw keys. Duration settings store seconds.
- Channel logs go through `core/debug.send_channel_log(...)` (no-op unless
  `logging.enabled` + channel configured).

## i18n (`core/i18n.py`, `language/en-US.yaml` + `pt-BR.yaml`)

- Sections flatten to dotted keys. `language_meta` is `/language` choice metadata,
  not a translation key. **en↔pt key parity enforced by `test_i18n.py`**: every new
  key goes in both catalogs.
- List-valued keys (e.g. `time_parser.*`) must use `translator.raw()`, never `t()`
  (`t()` calls `.format()` and crashes on lists).
- Quote YAML `on`/`off`/`yes`/`no`/`true`/`false` keys: unquoted they parse as
  booleans and `t()` falls back to the raw key (swept by `test_no_boolean_keys`).
- Multi-line templates must use **double quotes**: single-quoted YAML keeps `\n`
  literal (regression: `test_log_templates_use_real_newlines`).
- PT-BR wording: keep Discord community terms untranslated (`Starboard`, `Card`,
  `Bump`, `Cooldown`, `Boost`, `Warn`, `Ticket`, `Embed`, `Leaderboard`).
  Say `servidor`, never `guilda`. Mirror vocabulary already in `pt-BR.yaml`.

## Cards, themes, mentions

- **One theme file**: `themes/default.yaml` feeds BOTH the built-in fallback
  (`bot.theme`) and the selectable "default" in `/themes`. A theme = one YAML with
  `colors`/`emojis`/`markdown`/`styles` + optional `cards:`/`pings:`/`color_panel:`.
  Active theme resolves via sync `theme_for(bot, guild_id)` (boot snapshot).
- New customizable card = `CardSpec` in `core/card_specs.py` + `card.<key>.title`
  in BOTH catalogs + `mention_default` + exact `variables` contract in
  `VARIABLES_BY_KEY` (enforced by `test_variables.py`). Labels in
  `variables.<name>.label`/`.description` (both catalogs); samples in `core/variables.py`.
- **`cards:` values are raw Components V2 JSON** validated at load (40 components,
  depth 5, 4000 chars); invalid cards log and fall back to the built-in default,
  never break a send. `embed: {...}` + optional `buttons:` is the per-card classic
  alternative. Send sites use `CardPayload.message_kwargs()` and never branch on form.
- **Never add a seed-map entry for a card that has a default builder**: the seed
  shadows the builder when registration order slips, and tests pass green on the
  wrong path (regression: starboard author/avatar/gallery).
- **Every registered builder must accept `**variables`**: live sends forward
  send-site variables and a closed signature degrades to raw-key fallback
  (swept by `test_audit.py::test_all_builders_signatures_accept_variables`).
  Builder stat labels (`card.<key>.<name>_label`) must exist verbatim in both
  catalogs; a name mismatch leaks raw keys into live cards.
- **Mentions are content**: ping position is wherever `{@name}` appears in card
  text. The renderer collects `<@id>`/`<@&id>` tokens and builds `AllowedMentions`:
  never pass `allowed_mentions` by hand for card sends. Per-card toggles live in
  the theme `pings:` section. Every `CardSpec` render must go through
  `render_card_message` (or explicit `trace_card_path`).
- **Emojis never hardcoded**: `t()` auto-injects every `emojis:` theme entry as a
  format placeholder (explicit variables win, so never name an emoji token like a
  variable). Colors via `theme.color("<name>")`. Component `emoji=` must be
  RGI-valid or Discord 400s the whole payload.
- Panels (`core/panels.py`): panels register repaint callback + setting keys of
  interest; `on_theme_changed` repaints + re-syncs dispatch, `repaint_all` at boot.
  Every code path editing theme files must call `ThemeStore.invalidate_guild`
  (themes cache per `(guild_id, name)`).
- `cardact:` button ids (`core/card_actions.py`) are deterministic content hashes:
  stable across restarts so dispatch views match posted messages.

## Interaction / py-cord gotchas

- ACK every interaction (`defer()`/`send_message`/modal) before slow work; past 3s
  Discord shows "application did not respond". Guard with
  `if not interaction.response.is_done()`.
- py-cord `BaseView.on_error` only prints to stderr; `RosemaryBot.on_view_error`
  logs + ACKs `general.unknown_error`. Never swallow `discord.HTTPException`
  around `interaction.edit()` with bare `return`: a 400 looks like "click does nothing".
- When a click "does nothing" offline-green, check `bot.log` first: raw
  `Select`/`Button` without bound callback (use `MenuView.make_*` factories),
  `cardact:` id divergence, or dispatch views with no real children (py-cord's
  ViewStore indexes only `walk_children()` results).
- Never `disable_all_items()` before `rerender()`: `prepare()` rebuilds
  everything, wiping flags. Model terminal states in `build_items()`.
- Cogs whose slash commands take `discord.Option` annotations must omit
  `from __future__ import annotations` (`moderation.py`, `invites.py`, `info.py`):
  py-cord inspects annotations via `inspect.signature` and strings break option
  parsing (swept by `tests/test_commands.py`).
- py-cord `Member` uses `communication_disabled_until` (discord.py:
  `timed_out_until`). Always resolve via `cogs/audit.py:_timeout_until`
  (getattr fallback), never direct access. `MagicMock(spec=discord.Member)` hides
  this: assigning a discord.py name creates a phantom that passes offline and
  crashes live. Regression tests assert the live shape.
- Dashes are banned as prose punctuation in tracked files: em dashes (U+2014) and
  `--`. Rewrite with comma, colon, parentheses or period. Hyphens stay inside
  words and as list bullets.
