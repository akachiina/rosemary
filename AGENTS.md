# AGENTS.md

Rosemary: modular, multilingual Discord bot for multiple servers, built on **py-cord 2.8.1**. User-facing strings are Portuguese (pt-BR) or English (en-US) via i18n keys. Built-in visual is Components V2 everywhere; classic embeds exist only as a **per-card theme alternative**.

## Setup / run

- This directory is its own git repo (`main`), split out of the old single-guild bot: **ignore the parent repo's `cogs/`, `utils/`, `config/`, `bot.py`** (legacy).
- `requirements.txt` = runtime deps only. `rosemary/venv` is runtime-only (no pytest/ruff); dev tools live in the parent `.venv`.
- `pyproject.toml` (ruff + pytest config) lives in the **parent** repo, not here.
- Token from `.env` (copy `.env.example`); `.env`, `data/`, `venv/`, `*.log` are git-ignored.
- Run: `python bot.py` from inside this directory, or `python -m rosemary` from the parent. Both bootstrap `sys.path` with the repo root (`# noqa: E402` on imports after: keep that pattern). Paths to `data/`, `language/`, `.env`, `theme.yaml` resolve from `__file__`, never CWD.
- **No AI attribution footer in commit messages**: do not add `Generated with...` / `Co-Authored-By` trailers.

## Verify

- Tests **must run from inside this dir** (`../.venv/bin/python -m pytest tests`): several test files read `language/...` via CWD-relative paths and fail from the parent.
- Lint **must run from the parent** (`./.venv/bin/ruff check rosemary`): config is in parent `pyproject.toml` (line-length 100, py311, E/F/W/I/UP/B/SIM, ignores B008).
- `asyncio_mode = "auto"`: async tests need no decorator. `tests/conftest.py` is sys.path bootstrap only; each test file builds its own fakes.

## Architecture

- `bot.py` = `RosemaryBot` + `main()`. `_setup()` runs once in `on_ready`: registers cogs, loads `language/*.yaml` + `themes/default.yaml` (the single theme file, see Themes), snapshots `_command_base_keys` for per-guild re-localization.
- Cogs registered explicitly in `_setup()` via `add_cog(Class(bot))`: no auto-discovery, no `def setup(bot)` hooks. Commands sync **per guild, never globally**; `on_guild_join()` syncs newly joined servers.
- i18n (`core/i18n.py`): YAML sections flatten to dotted keys. `language_meta` is metadata for `/language` choices, not a translation key. **en↔pt key parity enforced by `test_i18n.py`**: add new keys to both catalogs.
- **PT-BR wording**: keep Discord pt-BR community terms untranslated (`Starboard`, `Card`, `Bump`, `Cooldown`, `Boost`, `Warn`, `Ticket`, `Embed`, `Leaderboard`). Say `servidor`, never `guilda`. Mirror vocabulary already in `pt-BR.yaml`.
- **List-valued catalog keys (e.g. `time_parser.*`) must use `translator.raw()`, never `t()`**: `t()` calls `.format()` and crashes on lists.
- **Quote YAML `on`/`off`/`yes`/`no`/`true`/`false` keys**: unquoted they parse as booleans and `t()` falls back to the raw key. Swept by `test_no_boolean_keys`.
- **Emojis never hardcoded**: `t()` auto-injects every `emojis:` entry from `themes/default.yaml` as a format placeholder; colors via `theme.color("<name>")`. Component `emoji=` must be RGI-valid or Discord 400s the whole payload (swept by `test_every_theme_emoji_is_discord_valid`).
- Per-guild command localization mutates shared `cmd.name`/`cmd.description` from `<cmd>.command.name`/`.description`, validated against `_COMMAND_NAME_RE` with fallback to the base name (a missing/invalid key would 400 the whole bulk sync; swept by `test_guild_join.py`).
- Persistence: one JSON file per guild, `data/<guild_id>/<filename>` via `GuildStorage`. Defaults merged on read (`use_defaults=False` skips the `language` default). Writes are atomic (temp + rename).
- Channel logs go through `core/debug.send_channel_log(...)` (no-ops unless `logging.enabled` + channel configured).
- Boot order in `_setup()` matters: `ThemeStore` -> `preload_themes(bot)` (snapshot for sync `theme_for`) -> `card_actions.sync_all_guilds(bot)` -> `panels.repaint_all(bot)` (needs cogs' stores ready).

## Mentions (`core/mentions.py`)

- **Mentions are content**: ping position is wherever `{@name}` appears in card text. The renderer collects `<@id>`/`<@&id>` tokens from resolved text and builds `AllowedMentions`: never pass `allowed_mentions` by hand for card sends.
- Per-card pings toggle in the theme file (`pings:` section; missing key = spec's `mention_default != "none"`). Entry points: `allowed_for_ids`, `allowed_for_text`, `allowed_for_document`, `render_card_message` (view + allowed in one call). `silent=True` forces no ping.
- `send_channel_log(..., card_key=..., mention_user_ids=[...])`: log cards default to off; calls without `card_key` never ping.
- New customizable card = `CardSpec` in `core/card_specs.py` + `card.<key>.title` in BOTH catalogs + `mention_default` + exact `variables` contract in `VARIABLES_BY_KEY` (**enforced by `test_variables.py`**). Labels in `variables.<name>.label/.description` (both catalogs); samples in `core/variables.py`. No in-Discord editor: customization is via theme files.

## Themes (`core/themes.py` + `cogs/themes.py`)

- **One theme file**: `themes/default.yaml` feeds BOTH the built-in fallback (`bot.theme`, loaded by `bot.py` and `ui/theme.load_theme()`) and the selectable "default" in `/themes`. `ui/theme.yaml` is gone; `THEMES_DIR` (`core/themes.py`) resolves from the package root (`parent.parent`, same trick as `language/`). A theme = one YAML with `colors`/`emojis`/`markdown`/`styles` + optional `cards:`, `pings:`, `color_panel:` (HTML templates for the color-panel image). Locations: `themes/` (global) + `data/<id>/themes/` (per-guild imports) + `themes.json` selection. Active theme resolves via sync `theme_for(bot, guild_id)` (boot snapshot; falls back to built-in `bot.theme`).
- YAML 1.1 parses bare numeric keys as ints (`medals: 1:`): theme loaders coerce keys with `str()`; do not reject otherwise-valid files over key type.
- **`cards:` values are raw Components V2 JSON** converted at load by `core/v2_convert.py`; Discord limits validated at load (40 components, depth 5, 4000 chars). Invalid themed card logs and falls back to the built-in default: never breaks a send.
- **Embed form is a per-card alternative** (`core/embed_convert.py`): a `cards:` entry as `embed: {...}` + optional sibling `buttons:` (link + `cardact:` both work). Same placeholder/ping behavior; invalid embeds degrade at load, never at send. Send sites use `CardPayload.message_kwargs()` and never branch on the card form.
- Card keys are dotted paths; resolution: themed override -> registered default builder (`set_default_builder`) -> catalog seed.
- **Never add a seed-map entry for a card that has a default builder**: the seed wins over nothing but shadows nothing either; still, its existence keeps `default_document` from the builder when the builder registration is skipped (import order), and it makes tests pass green while exercising the wrong path. The starboard card lost its author/avatar/gallery this way (regression: `test_content_cards.py::test_starboard_card_resolves_via_builder_not_seed`, which also pins `starboard.card` out of `SEED_PARTS_BY_KEY`; same pin for `utility.serverinfo`, whose rich builder renders the stat columns/banner/footer).
- **Every `CardSpec` render must trace its path** via `render_card_message` (or explicit `trace_card_path` for hand-built views). Interactive menus trace their heading too (`settings.title`, `themes.title`, `debug.title`, `boost.home`). Stay silent for: internal panel repaints, DMs, `bump.leaderboard` (uses `allowed_for_ids(user_ids=[winner_id])` to avoid over-pinging the top-10).
- **Menus expose only their heading** to themes (`card_service.menu_heading_items`); a themed doc containing a container is rejected (nesting 400s), falling back to the built-in heading. Interactive components stay in code (need registered callbacks).
- **`cardact:` buttons** (`ACTIONS` in `core/card_actions.py`): custom ids are deterministic hashes of content: stable across restarts so dispatch views match posted messages; divergence = silent dead clicks (regression: `test_panels.py`).
- **Panel repaint registry** (`core/panels.py`): panels register repaint callback + setting keys of interest. `on_theme_changed` repaints panels and re-syncs dispatch; `on_setting_changed` repaints only interested panels; `repaint_all` at boot. Repaint = `get_partial_message().edit()` with repost fallback.
- **Every code path editing theme files must call `ThemeStore.invalidate_guild`** (or write through the store): themes cache per `(guild_id, name)` and selection returns stale objects otherwise. `/themes` exposes this as reload.

## Settings (`core/settings.py`)

- `SETTINGS` registry drives `/settings`; no per-setting UI code. Adding a setting = `SettingSpec` + `settings.<key>.label`/`.description` (and `.choices.*`) in BOTH catalogs. **No test enforces label parity**: resolve every label or the UI renders raw keys.
- **Duration settings** (`is_duration=True`) store **seconds**, edited/displayed via `TimeParser` (`"2h"`, `"90m"`). Keep `coerce_value()`/`format_value()` consistent.
- `ITEMS_PER_PAGE = 7` (Discord 40-component cap, enforced by test); registry order = display order, essentials first. Nav row is always `[Close, Prev?, Next?]`, Close at index 0.
- **`CHANNEL_LIST`** = ordered channel/category ids on a dedicated screen (per-row Remove, batch Add via multi-select `max_values=25`, Clear all, 10/page). The select only collects into `_pending_adds`; Confirm persists via `_apply_value(..., stay_in_edit=True)`. Scalar renames set `legacy_single_key`: `get_setting` lazily seeds the list until first save. Regression: `tests/test_channel_list.py`.

## Event Log (`cogs/audit.py`)

- One master channel (`audit.channel`, `audit.enabled` default **off**) + per-event BOOLEAN toggles. Never add per-event channels. All listeners wrap in try/except + `log.exception`.
- Events: ban, unban, message_delete, message_edit, bulk_delete, nickname, avatar (best-effort), roles, timeout, voice_join/voice_leave (one toggle for the pair).
- Message content travels already fenced via `_fence()` at the send site: never re-fence in catalogs. Bots and the bot's own deletes are skipped.
- Bulk deletes coalesce (`BULK_FLUSH_SECONDS`, re-arming) into ONE card + `.txt` transcript (`audit.bulk_file_enabled`). `/limpar` stamps `note_purge_context()` for attribution and single-delete suppression. Ban/unban moderator+reason come from one best-effort audit-log lookup. Regression: `tests/test_audit.py`.

## Color Panel (`cogs/colors.py` + `core/colors.py` + `core/color_image.py`)

- Color-Chan style: members pick color roles from a posted panel; picking again removes (toggle), picking another switches (drops the previous color role). `/my_colors` lists entries; `/colors_panel` (admin) posts the panel.
- Settings category `colors` (`colors.enabled` default **off**, `colors.panel_channel`, `colors.picker` = `buttons`/`select`, `colors.per_container` 3 to 25, default 10). Manager screen opens from the category page button (pattern: anti-invite's batch attach + tickets' manager): reorder up/down, edit name/hex modals, batch role attach, create role from scratch, delete confirm. Every mutation repaints the posted panel via the `core/panels.py` registry.
- Persistence: `ColorStore` (`data/<id>/colors.json`), ordered entry list (order = image and button order), `MAX_COLORS = 25` (Discord hard cap: 5 rows of 5 buttons, 25 select options). Entries link to roles by `role_id` (None = renders but cannot be picked). The live role color wins over stored hex at render time.
- First manager open seeds 12 pastels (names from `colors.defaults.<slug>` in both catalogs); seed flag prevents re-seeding.
- Panel layout: themed frame card `colors.panel` + one V2 container per `per_container` chunk, each with its own image and that chunk's numbered buttons (numbers run continuously across chunks). Picker views are persistent (`colors_pick:<id>` re-registered at boot); embed-form themes get classic rows instead (embeds cannot host V2 galleries).
- Panel image: theme `color_panel.html`/`item` HTML templates (placeholders `{number}`/`{name}`/`{color}`) rendered by `core/color_image.render_panel` (WeasyPrint to PDF, PyMuPDF raster with alpha, cropped to content, transparent background). Optional deps: a missing renderer or a broken template posts the panel imageless (warning only, never breaks sends). Galleries reference files via `attachment://name.png`; `CardPayload(files=...)` carries the `discord.File` list and `message_kwargs()` forwards it (works on send and edit).
- Regression: `tests/test_colors.py` (store, image bytes, chunk numbering, pick/toggle flows, catalog parity).

## Boost + Bump (Disboard)

- `core/boost_roles.py` (`BoostRoleStore`), `core/bump.py` (`BumpStore`). **Only Disboard is supported** (hardcoded bot ID; mention it in copy). Stores use `GuildStorage(use_defaults=False)` with their own filename.
- `bump.schedule.*`: fixed daily open/close (overnight ranges OK). Lock ownership (`schedule` vs `camping`) lives in the store: schedule wins; enforcement is transition-only via a minute loop.
- Outage resilience is layered (`tests/test_bump_outage.py`): transient send failures (OSError/TimeoutError) retry `SEND_RETRY_ATTEMPTS` times; `on_connect` debounced recovery; minute-loop `_sweep_guild` as floor. HTTP errors never retried; a live task always wins over recovery (no doubles).

## Ported features

- `invites.py`: attribution diffs pre-join `self._cache` BEFORE overwriting it: refreshing first made every join `unknown` (regression: `test_ui_polish.py`). Counters: `total = regular - left - fake + bonus`.
- `tickets.py`: 4 fixed types, close/transcript/reopen/delete (views re-registered in `start()`), must close before delete. Panel posts via `core/panels.py`; type select hidden when the theme renders `open_ticket` buttons.
- `partnerships.py`: ads, hourly renewal loop (warn then remove), rep self-renew with repost, read-only audit + orphan cleanup.
- Small: `utility.py` (`/ping`, `/serverinfo` with a registered rich builder: server-name heading, icon accessory, stat columns, banner gallery skipped when absent, `-# ID` footer), `cleaner.py` (`/limpar`), `anti_invite.py`, `reminders.py` + `broadcast.py` (admin, **disabled by default**), `updater.py` (`stable` follows latest `vX.Y.Z` tag, `git` follows `origin/<branch>`; manual use always admin-allowed, `enabled` gates only the auto loop).
- Rule for every port: settings in registry + i18n in BOTH catalogs + `CardSpec` with `mention_default` + `allowed_mentions` on public sends + `send_channel_log(card_key=...)` + tests with real catalogs and `caplog` asserting zero format warnings.

## Visual standards

- Log bodies use compact fields (`**Alvo:** @x`, one line per field), never the old double-line shape. Unknown fields render a translated placeholder, never bare `-`. Titles carry a theme emoji token.
- **YAML quoting**: single-quoted strings keep `\n` literal: multi-line templates must use **double quotes**. Regression: `test_log_templates_use_real_newlines`.
- Event cards (welcome/leave/ban): container -> section with member avatar thumbnail (`{user_avatar}`), emoji `#` title, body, divider, `-# {server} · #{count}` footer. Built by `cogs/welcome.py:default_event_document`; sends via `render_card_message`.
- **Author footer is the standard attribution line** for cards that point at a person (starboard, audit): divider + small (`-#`) `card.author_footer.text` (`by {user} · {timestamp}`, both catalogs). Single source of truth: `core/cards.py` (`author_footer_template`/`author_footer_text`/`author_footer_blocks`): never inline the template string elsewhere. V2 has no native footer and its thumbnail is always right-side; an embed footer (`footer.icon_url`, left side) exists only in theme-written `embed:` cards. Welcome/leave/ban keep their own server/count footer. The `{timestamp}` variable formats as a Discord relative timestamp at the send site.
- **Starboard color is a continuous ramp** (`ui/theme.py`: `Theme.star_ramp`/`star_ramp_config`/`star_title_emoji`): theme `styles.star_ramp` (`from_color`/`to_color`/`limit`, token names or hex) blends the accent per star until `limit` then holds: the legacy weak-yellow-to-strong feel, not discrete tiers. Title emoji climbs theme `star_tier_emoji:` milestones (highest threshold wins; plain `star` below the first). Guild themes without a ramp fall back to the `star_tier_*` styles; ramp config must carry hex-resolvable colors or the fallback kicks in. Documents receive `#rrggbb` strings (schema contract): never a `Colour` object. Live updates recolor on star changes via the edit path (2s debounce).
- Default builders may receive send-site variables: `default_document(bot, guild_id, key, variables)` forwards `**variables`, so builders must take `**kwargs` and can react to content (starboard skips the gallery when `{image_url}` resolves empty: `_build_gallery` drops galleries whose urls all resolve empty). Seeded previews without variables still work.
- **Every registered builder must accept `**variables`**: a closed signature crashes on live sends (`default_document` swallows the TypeError, the card degrades to raw-key fallback). Regression: `test_audit.py::test_all_builders_signatures_accept_variables` sweeps `_DEFAULT_BUILDERS`; `default_document` additionally filters kwargs by signature (`_bind_builder_kwargs`) so legacy builders degrade instead of crashing. The audit/welcome/birthdays builders broke exactly this way (live: `audit.message_delete` posted raw `audit.message_delete.title` keys).

## Gotchas

- **Dashes are banned as prose punctuation in every tracked file**: em dashes (U+2014) and the double-hyphen stand-in `--`. Rewrite the sentence with real punctuation (comma, colon, parentheses or a period). Hyphens stay only inside words ("multi-file") and as list bullets ("- item"). Grep for `\x{2014}` and for spaced `: ` over `*.py`/`*.yaml`/`*.md` must stay empty.
- **py-cord vs discord.py attribute names**: py-cord `Member` uses `communication_disabled_until`; discord.py uses `timed_out_until`. Always resolve via `cogs/audit.py:_timeout_until` (getattr fallback), never direct attribute access: direct access crashed every `on_member_update` live while offline mocks with `spec` hid it.
- `MagicMock(spec=discord.Member)` hides missing attributes: accessing a non-existent py-cord attribute raises `AttributeError`, but manually assigning a discord.py name creates a phantom that passes offline and crashes live. Regression tests assert the live shape.
- ACK every interaction (`defer()`/`send_message`/modal) before slow work: past 3s Discord shows "application did not respond". Guard with `if not interaction.response.is_done()`.
- py-cord `BaseView.on_error` only prints to stderr; `RosemaryBot.on_view_error` logs + ACKs `general.unknown_error`. Never swallow `discord.HTTPException` around `interaction.edit()` with bare `return`: a 400 looks like "click does nothing".
- Offline menu repros can pass while live fails: Discord rejects bad payloads only on the HTTP call. When a click "does nothing", check `bot.log` first. Common causes: raw `Select`/`Button` without bound callback (use `MenuView.make_*` factories); `cardact:` id divergence.
- Pre-restart ephemeral panels look alive but their view is gone (silent no-op). Re-send or repaint on boot. `add_view` only indexes real view children: dispatch-only fallback views need stub components.
- Never `disable_all_items()` before `rerender()`: `prepare()` rebuilds everything, wiping flags. Model terminal states in `build_items()`; reuse `ui/confirm.py:ConfirmView`.
- Stale-content symptoms (old theme, missing inviter) usually mean a cache was refreshed before its consumer read it. Prove with an offline repro before patching.
- `cogs/moderation.py` **omits `from __future__ import annotations`**: py-cord inspects annotations via `inspect.signature` and stringified annotations break option parsing. Guarded by `tests/test_commands.py`. Moderation durations are `timedelta | None` (ban/kick/warn pass `None`); never call `TimeParser.format_duration` unguarded outside mute branches.
- `/settings` menu type mapping: ROLE -> role-select, CHANNEL -> channel-select, CATEGORY -> channel-select filtered to `ChannelType.category`, BOOLEAN -> buttons, INTEGER/STRING -> modal, CHOICE -> select.
- Boost invite DMs are three messages: plain text, buttons-only `DesignerView` (unique `custom_id`; never send with `content=`, V2 forbids it), role-preview card. Bot code never builds embeds; embeds come only from theme `cards:` entries.
- Never echo or commit the token.
