# AGENTS.md

Rosemary: modular, multilingual Discord bot (multi-server) on **py-cord 2.8.1**.
User-facing strings are pt-BR / en-US via i18n keys. Built-in visual is
Components V2 everywhere; classic embeds exist only as a per-card theme alternative.

## Setup / run

- This directory is its own git repo (`main`). **Ignore the parent repo's
  `cogs/`, `utils/`, `config/`, `bot.py` and its README** (legacy single-guild
  bot; the README describes that old setup).
- `requirements.txt` is the runtime source of truth (adds WeasyPrint, PyMuPDF,
  PyNaCl over the parent `pyproject.toml`). `rosemary/venv` is runtime-only
  (no pytest/ruff); dev tools live in the parent `.venv`.
- `pyproject.toml` (ruff + pytest config) lives in the **parent** repo, not here.
- `BOT_TOKEN` from `.env` (copy `.env.example`) or the environment; never echo
  or commit it. `.env`, `data/`, `venv/`, `*.log` are git-ignored.
- Run `python bot.py` from inside this dir, or `python -m rosemary` from the parent.
  Both bootstrap `sys.path` with the repo root (`# noqa: E402` imports after: keep).
  Paths to `data/`, `language/`, `.env`, themes resolve from `__file__`, never CWD.
- No AI attribution footer in commit messages.

## Verify

- Tests **must run from inside this dir** (`../.venv/bin/python -m pytest tests`):
  several files open `language/...` via CWD-relative paths and fail from the parent.
- Single test: `../.venv/bin/python -m pytest tests/test_<name>.py -q` (same dir).
- Lint **must run from the parent** (`./.venv/bin/ruff check rosemary`):
  config is in parent `pyproject.toml` (line-length 100, py311, E/F/W/I/UP/B/SIM).
  From inside this dir the `rosemary` path does not resolve (E902).
- No CI workflows or pre-commit config here; these local commands are the gate.
- `asyncio_mode = "auto"`: async tests need no decorator.
  `tests/conftest.py` is sys.path bootstrap only; each test file builds its own fakes.

## Architecture

- `bot.py` = `RosemaryBot` + `main()`. `_setup()` runs once in `on_ready`:
  registers cogs, loads `language/*.yaml` + `themes/default.yaml`, snapshots
  `_command_base_keys`/`_subcommand_base_keys`, and rebinds the shared `bump`
  group.
- Cogs registered explicitly in `_setup()` via `add_cog(Class(bot))`: no
  auto-discovery, no `def setup(bot)` hooks. Prefer an async `start()` hook for
  background work (called explicitly; py-cord never calls `cog_load` here).
- Commands sync **per guild, never globally**; `on_guild_join()` syncs new servers.
  Localized `cmd.name` must match `_COMMAND_NAME_RE` (lowercase); invalid names
  fall back to the base name. Missing `<base>.command.name` keys are swept in
  both catalogs by `test_guild_join.py`.
- Option descriptions localize like the command itself:
  `<command_key>.options.<option>.description` (for example
  `ban.command.options.member.description`). `_setup()` snapshots the
  decorator descriptions so a missing key restores English instead of leaking
  a raw dotted key; `_localize_options()` mutates the shared option per guild
  before `sync_commands()`. Add keys to BOTH catalogs (swept by
  `test_guild_join.py::test_every_option_has_a_localized_description`).
- Multi-word commands live in `SlashCommandGroup`s (`/roleplay beijar`), not flat
  names with underscores; simple top-level commands (`/ban`, `/debug`) are
  intentional. Single-cog groups are class attributes (birthday pattern);
  the cross-cog `bump` group is module-level, held by NEITHER cog, registered
  explicitly in `_setup()` + bound per subcommand via `rebind_shared_group_commands`
  (py-cord copies class-attr groups at class-def time, freezing later attachments,
  and only the `.cog` assignment re-runs option validation, which skips
  the leading `self`/`ctx` params).
- Subcommand localization reuses keys: `{group}_{sub}.command` (legacy flat key)
  then bare `{sub}.command`; group names localize like top-level names. A guild
  without a catalog key resets the shared subcommand to its decorator base name.
  Renamed setting/card keys need lazy migration (`_RENAMED_KEYS` in settings,
  `_RENAMED_CARDS` in themes, `_RENAMED_PINGS` in mentions).
- **Groups must be `core.localized_group.LocalizedSlashCommandGroup`, never
  `discord.SlashCommandGroup`**: one shared tree is renamed per guild before
  each sync, so py-cord's live-name `find` in `_invoke` resolved only the last
  synced guild (regression: `/boost painel` raised `'NoneType' object has no
  attribute 'invoke'`). The subclass resolves the incoming name through
  `bot._guild_subcommand_aliases`, built at sync time (stored only after a
  successful sync) and pre-seeded at boot by `reapply_command_localization`;
  `on_guild_remove` drops the guild map. `_sync_guild_commands` is serialized
  by `_command_sync_lock` for the same shared-tree reason.
- Persistence: one JSON file per guild, `data/<guild_id>/<filename>` via
  `GuildStorage`. Defaults merged on read (`use_defaults=False` skips even the
  `language` default). Writes are atomic (temp + rename).
- `SETTINGS` registry (`core/settings.py`) drives `/settings`; no per-setting UI
  code. New setting = `SettingSpec` + `settings.<key>.label`/`.description`
  (+ `.choices.*`) in BOTH catalogs. `test_i18n.py` checks every registry key
  has that catalog copy; a missing entry renders its raw key. Duration settings
  store seconds. Message-template settings whose default is user-facing also set
  `default_catalog_key`; the UI resolves it only while the key is unsaved, so
  pt-BR never sees the English literal baked into `SettingSpec.default`.
- Staff logs go through `core/debug.send_log(bot, guild_id, "<event>", **vars)`
  (no-op unless `logging.enabled` + channel configured). `LogSpec` in
  `core/log_specs.py` owns title/body/severity/layout/footer; `core/log_cards.py`
  registers one default builder per event. Never render a log envelope by hand.
- When a moderation path posts its own log card, stamp the audit gateway
  (`AuditCog.note_bot_ban/kick/timeout`) **before** the destructive API call:
  delivery order is not guaranteed and the stamp suppresses the duplicate
  generic card. Existing calls live in `cogs/moderation.py` and `cogs/trap.py`.
- The stamp suppresses the **audit** card only. Event announcements
  (`events.ban_channel`, `events.leave_channel`) still go out for our own
  actions: the welcome cog skips them only when the event channel is the same
  as the staff log channel (`logging.channel`), where the moderation card
  already landed. A ban never posts the leave card (`_recently_banned`).

## i18n (`core/i18n.py`, `language/en-US.yaml` + `pt-BR.yaml`)

- Sections flatten to dotted keys. `language_meta` is `/language` choice metadata,
  not a translation key. **en↔pt key parity enforced by `test_i18n.py`**: every new
  key goes in both catalogs. Command metadata includes option descriptions under
  `<command_key>.options.<option>.description`; the `test_guild_join.py` sweep
  fails when any registered option is missing either catalog entry.
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

- `themes/default.yaml` is the single source for BOTH the built-in fallback
  (`bot.theme`) and the selectable "default" in `/themes`. `themes/` may hold
  extra importable themes (e.g. `ridiculous_theme.yaml` demo). A theme = one YAML
  with `colors`/`emojis`/`markdown`/`styles` + optional
  `cards:`/`pings:`/`color_panel:`/`snapshot:`.
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
- Log events are CardSpecs too: `cards.<event>` is a full-card override, while
  the legacy `cards.<event>.description` stays a **body-only** override and old
  `pings:<event>.description` toggles map to the new event key. Shared field
  labels live in `log.field.*.label` (both catalogs) and carry theme emoji
  tokens. `avatar_var` switches a default log frame to the audit-style section
  with a right-side thumbnail; call sites pass the matching avatar URL.
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
- **Machine values render as inline code in every card**: snowflake IDs, command
  usages (`!ban`, `/configuracoes`), the text-command prefix, version/channel and
  file names sit inside single backticks. Mentions, `<t:...>` timestamps, plain
  counters and links never do. Swept in both catalogs and the info-family builder
  footers by `tests/test_card_formatting.py`.
- Panels (`core/panels.py`): panels register repaint callback + setting keys of
  interest; `on_theme_changed` repaints + re-syncs dispatch, `repaint_all` at boot.
  Every code path editing theme files must call `ThemeStore.invalidate_guild`
  (themes cache per `(guild_id, name)`).
- `cardact:` button ids are `cardact:<card_key>:<button_id>` from the stored
  card, not generated hashes: keep button ids stable or old messages stop
  dispatching. `core/card_actions.py` rebuilds views from themed docs.

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
- Cogs with `discord.Option` annotations must omit
  `from __future__ import annotations`: py-cord never evaluates stringified
  annotations, so options become raw strings and crash at invocation.
  `tests/test_commands.py` only covers the cogs it registers; add new
  option-bearing cogs there.
- py-cord `Member` uses `communication_disabled_until` (discord.py:
  `timed_out_until`). Always resolve via `cogs/audit.py:_timeout_until`
  (getattr fallback), never direct access. `MagicMock(spec=discord.Member)` hides
  this: assigning a discord.py name creates a phantom that passes offline and
  crashes live. Regression tests assert the live shape.
- Dashes are banned as prose punctuation in tracked files: em dashes (U+2014) and
  `--`. Rewrite with comma, colon, parentheses or period. Hyphens stay inside
  words and as list bullets.
