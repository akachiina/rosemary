# AGENTS.md

Rosemary: modular, multilingual Discord bot (multi-server) on **py-cord 2.8.1**.
User-facing strings are pt-BR / en-US via i18n keys. The bot speaks **three
shapes**: plain text (acks and notices), classic embeds (the staff logs and
the DMs that stay) and Components V2 (the content of the channel and the
interactive UI).

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
- **Golden cards**: `tests/golden/cards/<key>.json` pins what every card
  renders (text, embed or V2 view), so a pipeline refactor cannot change what
  members see unnoticed. After a *deliberate* change to a card's visible
  output, re-run `../.venv/bin/python scripts/snapshot_cards.py` and review
  the diff. `tests/golden_cards.py` holds the shared render helpers (the script
  and the test both use it). The fixture shows what a member really receives:
  the composed lines (`EXTRA_LINES`, `EXTRA_LINES_BY_KEY`) and the state the
  send site computes (`STATE_BY_KEY`) are filled the same way the live send
  fills them. A message that carries a stack of embeds records the boxes in
  the `embeds` field (the `embed` field keeps the first one); the field only
  appears when there is more than one box.
- **Golden panels**: `tests/golden/panels/<name>.json` pins what every
  representative panel shows (the texts, the buttons and the selects, with the
  label, the description, the icon and the current default). After a
  *deliberate* change to a panel, re-run
  `../.venv/bin/python scripts/snapshot_panels.py` and review the diff.
  `tests/golden_panels.py` holds the shared helpers (the script and the test
  both use it).
- **Golden notices**: `tests/golden/notices.json` pins the plain-text layer in
  both catalogs: the copy the member reads, with the icon the copy itself
  carries (a theme emoji token). After a *deliberate* change to a notice,
  re-run `../.venv/bin/python scripts/snapshot_notices.py` and review the diff.
  `tests/golden_notices.py` holds the shared cases and render (the script and
  the test both use it); `tests/test_golden_notices.py` also fails when a copy
  carries no icon token, when the token is unknown to the theme, or when a line
  opens with two icons.
- **V2 markers**: `-#` (small text) and `#` (heading) are Components V2 only;
  in an embed or a plain text Discord shows them raw. `tests/test_no_v2_markers.py`
  freezes the cards that still leak one: the list only shrinks (remove the
  entry when a family is fixed), and a new leak fails the test.
- **Visual grammar**: `tests/test_visual_grammar.py` lints the goldens and the
  panels: no mention or timestamp token in an embed frame, `image` never equals
  `thumbnail`, every embed has a title or a description, a pinged embed carries
  a sentence in the content, a multiline text card opens with a bold title, no
  text card disguises a field as `> **Label:**`, the category picker marks the
  current category, every action button of the five panels has an icon, no
  golden leaks `{emojis/`, and slash command descriptions stay emoji-free.
  The embed rules read **every** box of a message (the stack of the
  `embeds:` slot) and every message of a sequence.
- **Icons and settings UX**: `tests/test_icon_system.py` requires a
  `category_*`/`type_*` token per registry value and pins `Theme.icon` (an
  asset reference resolves to the mention, an unknown asset to empty).
  `tests/test_settings_ux.py` pins the category picker, the search (accent- and
  case-insensitive, opens the edit screen) and the reset (removes the key and
  keeps the localized default).

## Architecture

- **The settings registry is declarative**: `settings/<category>.yaml` (17
  files, one per category) holds the 122 settings; `core/settings_loader.py`
  reads them into `SettingSpec`s (validating every entry) and
  `core/settings_spec.py` holds the vocabulary (`SettingType`,
  `SettingCategory`, `SettingSpec`, `MENU_TIMEOUT`). `core/settings.py` serves
  the registry (`SETTINGS`, `CATEGORIES`, `settings_for_category`), coerces the
  values (`coerce_value`, `format_value`) and reads/writes them per guild.
  `tests/settings_baseline.json` freezes the registry as it was before the
  move: `tests/test_settings_yaml.py` fails if a default, a constraint or the
  order inside a category changes. The schema of one entry:
  `category`, `type`, `default` (required) plus the optional `min`, `max`,
  `choices`, `duration`, `multiline`, `default_catalog_key` and
  `legacy_single_key`.
- **The command surface is one policy** (`core/command_surface.py`): a
  `Surface` declares the ACK (defer or not), the visibility (ephemeral or not),
  the permission the invoker needs and the copy of the failure (`card_key`);
  `run(ctx, surface, handler)` applies it. Every failure becomes a translated
  notice (`core/feedback.py`), never a traceback for the member:
  `general.no_permission`, `general.bot_no_permission` and the family's copy.
  The migrated family is the moderation one (`/ban`, `/kick`, `/mute`, `/warn`,
  `/warnings`, `/unwarn`, `/limpar` and the prefix aliases). A command whose
  answer is a panel declares `defer=False`: the panel is the answer.
- `bot.py` = `RosemaryBot` + `main()`. `_setup()` runs once in `on_ready`:
  registers cogs, loads `language/<code>/*.yaml` + `themes/default/tokens.yaml`,
  snapshots `_command_base_keys`/`_subcommand_base_keys`, and rebinds the shared
  `bump` group.
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
- Slash command and option descriptions carry **no emoji or theme token**: the
  command picker stays plain, and the visual hierarchy lives in `!help` (an
  embed whose group fields carry `shield`/`info`/`gear`/`speech`), in
  `/settings` and in the panels. The plain picker is swept by
  `tests/test_visual_grammar.py`.
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
- `SETTINGS` registry (`settings/<category>.yaml`, loaded by
  `core/settings_loader.py`) drives `/settings`; no per-setting UI
  code. New setting = an entry in the YAML of its category +
  `settings.<key>.label`/`.description`
  (+ `.choices.*`) in BOTH catalogs. `test_i18n.py` checks every registry key
  has that catalog copy; a missing entry renders its raw key. Duration settings
  store seconds. Message-template settings whose default is user-facing also set
  `default_catalog_key`; the UI resolves it only while the key is unsaved, so
  pt-BR never sees the English literal baked into `SettingSpec.default`.
  `core/settings.reset_setting` removes the stored key (and the renamed/scalar
  legacy keys) instead of writing `spec.default`, so a reset keeps that lazy
  default.
- **The settings menu is icon-driven**: the category picker shows
  `category_<value>` (with a short hint and `category_count`), each row shows
  `type_<value>`, and the current category is the select's `default`. A boolean
  flips in one click (the accessory is the current state, `success`/`danger`);
  the search modal folds accents and case, filters labels of every category and
  pages 25 results; the edit screen offers `Restaurar padrão` only while the key
  is stored.
- Staff logs go through `core/debug.send_log(bot, guild_id, "<event>", **vars)`
  (no-op unless `logging.enabled` + channel configured). The log events are
  **documents** (`themes/default/cards/logs.yaml`), not builders: the
  archetype `log_event` (the footer attributes the actor) or `log_named` (the
  actor is already in the content) carries the frame. Never render a log
  envelope by hand.
- When a moderation path posts its own log card, stamp the audit gateway
  (`AuditCog.note_bot_ban/kick/timeout`) **before** the destructive API call:
  delivery order is not guaranteed and the stamp suppresses the duplicate
  generic card. Existing calls live in `cogs/moderation.py` and `cogs/trap.py`.
- The stamp suppresses the **audit** card only. Event announcements
  (`events.ban_channel`, `events.leave_channel`) still go out for our own
  actions: the welcome cog skips them only when the event channel is the same
  as the staff log channel (`logging.channel`), where the moderation card
  already landed. A ban never posts the leave card (`_recently_banned`).

## i18n (`core/i18n.py`, `language/<code>/<feature>.yaml`)

- The catalogs are **modular**: one folder per language
  (`language/en-US/`, `language/pt-BR/`), one file per feature
  (`about.yaml`, `moderation.yaml`, `boost.yaml`...). The loader merges the
  feature files of a folder; the old single file (`language/<code>.yaml`) still
  loads, so a custom catalog works either way.
- Sections flatten to dotted keys. `language_meta` is `/language` choice metadata,
  not a translation key (it lives in `_meta.yaml`). **en↔pt key parity enforced
  by `test_i18n.py`**: every new key goes in both catalogs. Command metadata
  includes option descriptions under
  `<command_key>.options.<option>.description`; the `test_guild_join.py` sweep
  fails when any registered option is missing either catalog entry.
- `tests/catalog_reference.json` pins the hash of the flattened keys of each
  catalog: the split into feature files must not lose a key. Regenerate it with
  `../.venv/bin/python scripts/split_catalog.py` after a deliberate change (the
  script's `--check` compares the copy too).
- The contract of a card is its document's `variables`, **not** a
  `card.<key>.placeholders` hint: the old hint blocks are gone. The labels live
  in `variables.<name>.label`/`.description` (both catalogs).
- List-valued keys (e.g. `time_parser.*`) must use `translator.raw()`, never `t()`
  (`t()` calls `.format()` and crashes on lists).
- Quote YAML `on`/`off`/`yes`/`no`/`true`/`false` keys: unquoted they parse as
  booleans and `t()` falls back to the raw key (swept by `test_no_boolean_keys`).
- Multi-line templates must use **double quotes**: single-quoted YAML keeps `\n`
  literal (regression: `test_log_templates_use_real_newlines`).
- PT-BR wording: keep Discord community terms untranslated (`Starboard`, `Card`,
  `Bump`, `Cooldown`, `Boost`, `Warn`, `Ticket`, `Embed`, `Leaderboard`).
  Say `servidor`, never `guilda`. Mirror vocabulary already in `pt-BR/`.

## The three layers (text, embed, Components V2)

Rosemary speaks three shapes, and every message picks one:

- **plain text** (`core/feedback.py`): acknowledgements, confirmations and
  notices. `respond(target, body)` sends the copy **verbatim**: the layer adds
  no prefix, no icon and no tone, so the code never decides how a notice looks.
  The **catalog owns the icon**: the copy opens with an emoji token
  (`{ban}`, `{kick}`, `{mute}`, `{warning}`, `{success}`, `{error}`...) and the
  theme's `emojis:` gives that token its art, so a guild restyles every notice
  at once by editing one list. **One icon per notice**: the copy brings it and
  nobody adds a second mark. The result/verb and the value that changed are
  **bold**, the mention is not: `🔇 @membro foi **silenciado** por **1h**.` A
  text card with more than one thing opens with a **bold title**, one item per
  line, fields as `• **Label:** value`; it never uses `> **Label:**` or a
  heading marker. The icon stays **outside** the bold (`❌ **Título**`): the
  title is the text that follows the mark. DMs and text cards follow the same
  rule: their copy belongs to the card, not to a command answer, so the icon
  is in the copy too. A notice **never notifies**: the copy may name the member
  the bot is about to ban, so every text send carries `AllowedMentions.none()`
  (the Components V2 container it replaced never pinged).
- **classic embed**: the staff logs (the logs, the audit cards and the staff
  records) and the DMs that stay as they are (the event DMs and the
  partnership notices). A card that speaks to the member through a DM
  declares its sentence in the **`content_line`** slot: the line always
  rides the `content` of the message, unlike `ping_line`, which the theme's
  `pings:` toggle gates.
- **Components V2**: the content the bot posts in the channel (the social
  cards, the listings, the reports and the event announcements) and the
  interactive UI (the menus and panels): settings, themes, debug, cleaner,
  boost, roleplay, colors, confessions review and the ticket panel.

The rule of the three layers decides the shape of every message:

1. an **ack or a notice** is plain text (`core/feedback.py`);
2. a **DM** keeps the shape the card declares (embed or text);
3. the **content of the group** (what the bot posts in a channel) is a
   Components V2 card;
4. the **staff log** (the `logs.*`, `audit.*`, `moderation.logs.*` and
   `partnerships.audit` cards) is a classic embed.

The unit of the change is the whole group of commands: a card never changes
shape alone, the family changes together.

The **form** is part of the card's contract: the document declares it (`card`
(Components V2), `embed` or `text`), and the registry projects it into the
`CardSpec` (`core/card_specs.py` is a loader: one spec per document of the
default package).

Resolution order (`core/card_resolution.py::get_effective_card`):

1. the theme's **delta** (`cards.<key>`, a semantic document) wins: the fields
   it does not declare are inherited from the packaged document;
2. the theme's **legacy full card** (`cards.<key>`, the raw Components V2 of
   the old themes) replaces the card whole;
3. the theme's **form override** (`cards.<key>.form: embed`) picks the shape of
   the default content;
4. the card's own default form.

A themed document made **only of text blocks** carries no layout: the card
keeps its own surface (a text card stays one line, an embed card stays an
embed) and its own tone and contract (`Card.copy_only` in
`core/design/ir.py`). A themed **structural** document (container/section) is
the opposite: the theme asked for a rich layout, so it ships as Components V2.

- The generic converters live in `core/forms.py` (V2 to embed, V2 to text).
  A card that needs a bespoke layout in another form owns it in the document
  (the IR writes every form from the same semantic model), so no card needs
  a form-specific builder anymore.
- `CardPayload` carries the three shapes (`content`, `embed`, `view`); it
  lives in `core/card_payload.py` (with `send_card`, `is_v2_message` and
  `is_v2_payload`). Send sites use `payload.message_kwargs()` and never
  branch on the form. A message that carries a stack of embeds also has
  `embeds` (the whole stack), the first box being `embed`.
- The pipeline is three modules: `core/card_resolution.py` (which document
  answers: the theme's delta, the theme's legacy card, else the package),
  `core/design/compile.py` (the placeholders resolved once) and
  `core/card_service.py` (the send: the payload, the mention policy and the
  repaint).
- Code fallbacks (the rare "no document resolved" path) use
  `core/card_service.py::simple_payload`: it follows the card's own form, so
  the surface never changes under the member.
- An **embed never notifies**: when a card's ping is essential (policy
  `mention_default != "none"` plus the theme's `pings:` toggle), the mention
  tokens travel in the message `content` (`core/forms.py::embed_content_for_pings`).
- DMs travel the same pipeline (`ui/boost_dm.py::send`): a text card is one
  line, an embed card an embed, a themed V2 override a view. DMs never send
  `allowed_mentions`. A themed sequence sends **every** message, in order
  (`send` and `send_preview` both walk the sequence).
- `core/card_service.py::render_card_message` resolves both `{name}` and
  `{@name}` placeholders in the text form: a raw `{ms}` never ships.

### The topology of a card (`messages:`)

A card is not always one message. A document may declare a **sequence**: each
entry of `messages:` is a whole card (its own shape, its own slots), and the
keys outside `messages` (`variables`, `style`, `actor`) are the metadata of
the card, inherited by every message that does not declare its own.

- The sequence is **opt-in**: a document without `messages:` renders one
  message, exactly as before.
- `core/card_service.py::render_card_messages` returns the whole sequence
  (a `RenderedMessage` per entry, with its own mention policy);
  `render_card_message` is the wrapper that returns the **first** message
  (the repaint path and the send sites that ship one message).
- `core/card_payload.py::send_cards` sends every message, in order.
- A theme's delta customizes the **first** message; a delta that declares
  `messages:` replaces the topology whole.
- The sequences are **one-shot** (the DMs and the announcements): the
  repaint never rebuilds them, it edits the first message.

### The boxes of a message (`embeds:`)

A message may carry **several** classic embeds, stacked. The `embeds:` slot
is the plural of the embed: each entry is a whole card of its own (its own
title, body, fields and accent), so a theme writes the boxes a message
carries without touching Python.

```yaml
boost.dm.invite:
  form: embed
  content_line: '{boost.dm.invite}'   # the message, not a box
  actions: [...]                      # the message, not a box
  embeds:                             # the boxes of the message
  - title: '{boost.dm.invite_role}'
    description: '{boost.dm.invite_body}'
  - title: '{boost.dm.invite_terms}'
    description: '{...}'
```

- The list **decides**: when a document declares `embeds:`, the top slots
  (`title`, `description`, `fields`, `color`...) are ignored. The top slots
  are the short form of a list of one box, so every card that never declared
  the slot keeps shipping exactly what it shipped before.
- The stack only lives in the classic embed form (`card_bad_embeds`
  otherwise): Discord's Components V2 carries no embed at all.
- The slots of the **message** (`actions`, `content_line`, `ping_line`,
  `variables`, `actor`, `form`, `messages`, `embeds`...) may not live inside
  a box (`card_bad_embed`): the buttons and the content line belong to the
  message that carries the boxes.
- Discord carries **ten** embeds at most: a longer list is a document error
  (`card_too_many_embeds`), caught by the reader (and by the theme lint).
- A box that says nothing does not ship (an empty frame is not a card).
- The buttons of the message ride the **first** box; the mention policy is
  one per message: every box collects into the same policy.
- `CardPayload` carries the stack (`embeds`) and its first box (`embed`),
  both in sync; `message_kwargs()` sends `embed=` for one box and
  `embeds=[...]` for many.
- The stack lives in the model as `Card.embeds` (a tuple of cards), it is
  compiled with the parent (`core/design/compile.py`) and drawn by
  `card_to_embeds` / `resolve_embeds`.

### Repaint follows the message

A **repaint** (editing a message the bot posted before) keeps the shape the
message already has: Discord's `IS_COMPONENTS_V2` flag is immutable, so a V2
message can never carry content or an embed, and a classic message never
becomes V2 (an edit that ignores this ends in a 400, error 50035).

- `core/card_service.py::align_to_message` owns the rule: a V2 message gets
  the card rendered again in the V2 form (`render_card_message(form="card")`),
  a classic message gets it rendered again in the classic form
  (`form="embed"`), so a message posted before the card changed shape keeps
  its shape and stays up to date. A card that says nothing in the message's
  form skips the repaint (the old content stays).
- `is_v2_message` reads the flag, `is_v2_payload` reads the rendered view
  (both in `core/card_payload.py`).
- Attach the interactive rows **after** the alignment: the row rides the
  payload the edit finally sends (suggestions: the vote row).

### The Card IR (`core/design/`)

The semantic model behind the three forms (the UI System v2 plan, P1). A card
says *what* it is; the serializer says how Discord writes it. The package
holds **only** the model and its two ends (the meta de 900 linhas):

- `core/design/ir.py`: the model (`Card`, `Field`, `Author`, `Action`, `Text`,
  `Divider`, `Gallery`, `Thumbnail`, `Row`, `Section`, `Container`) and the
  block converters. A card is native in one shape: `blocks` (V2), `slots`
  (semantic fields) or `content` (plain text). The `content_line` slot is the
  sentence an embed ships in the message `content`: it is not a field and
  never becomes one.
- `core/design/compile.py`: the placeholders resolved **once** (`{name}` and
  `{@name}`) and the mention ids collected. The `color` slot resolves too
  (the state may choose the token): a hex is accepted as it is, a token that
  never arrived is dropped and the card ships without an accent.
- `core/design/serialize.py`: the model written in each form. The V2
  serializer owns the markdown dialect (`#` heading, `-#` small text); the
  embed serializer writes neither.
- The **readers** live outside the package, in `core/documents/`: the strict
  document (`semantic.py`), the legacy one (`legacy.py`), the templates
  (`templates.py`) and the merge of the layers (`merge.py`). The loader of the
  theme packages is `core/theme_package.py`.
- `core/card_service.py::render_card_message` is the **only** pipeline: it
  resolves the card (`core/card_resolution.py::get_effective_card`), compiles
  it (`core/design/compile.py`) and writes it in its form. There is no legacy
  pipeline left, so there is no migration lever: every card goes through the
  model.
- The renderers take a `resolved=True` flag (`core/cards.py::build_items`,
  `resolve_embed`, `build_embed_view`): the text is final, so nothing is
  validated nor substituted twice.

### The interactive UI (`ui/kit.py`, `ui/runtime.py`)

The panels are not cards: they are screens assembled in Python. The kit owns
the chrome and the runtime owns the lifecycle, so every panel answers the same
way.

- `ui/kit.py`: `Panel` (the base of every menu), `Navigation`, `Chrome`,
  `Notice`, `EmptyState`, `Confirm`, `BUTTON_STYLES` and
  `menu_heading_items`. A panel provides the content (`sections()`,
  `actions()`, `compose()`); the frame (header, body, footer) and the
  navigation come from the theme's `panels.chrome`.
- `Panel.set_screen(key, body_key=None, **vars)` sets the title/body/color and
  resets the one-shot state (`lines`, `accent`, `nav_extra`). Every migrated
  panel uses it.
- `Navigation.row` follows the order of `panel.nav_actions`: the chrome only
  supplies the labels, the styles and the ids. `nav_extra` rides at the end.
- `ui/runtime.py`: `ack`, `open`, `update`, `close`, `measured`. The ACK
  (through `core/interactions.ack`) reports a dead token, so a handler stops
  before mutating anything: `if not await self._ack(interaction): return`.
- The migrated panels: `settings_menu`, `boost_menu`, `colors_menu`,
  `roleplay_menu`, `cogs/themes.py`, `cogs/cleaner.py`. `ui/colors_panel.py`
  stays a persistent `MenuView` (the dispatcher and the chunk builder, not a
  chrome panel).
- The surfaces that **accompany a card** (the confession review, the ticket
  panel/actions, the debug panel) are not `Panel`s: the card is the frame.
  They use the kit's lifecycle (`_ack`, `runtime.close`) and
  `core/card_service.py::simple_payload` for the fallback.
- The fallbacks never build a container by hand: tickets, the confession
  review, the boost DM preview and the color listing render through
  `render_card_message`/`simple_payload`, so the card's form decides.
- `ui/confirm.py` and `ui/heading.py` are shims: they re-export `kit.Confirm`
  and `kit.menu_heading_items` (the old imports keep working).
- The panel goldens live in `tests/golden/panels/`: `scripts/snapshot_panels.py`
  writes them (settings, boost, roleplay, colors, cleaner) and
  `tests/test_golden_panels.py` compares the current render against them.

**Line count:** `ui/` is 6445 lines (the baseline was 6016; the kit and the
runtime add 881, the migrations removed ~900, P7 added the icon resolver and the
settings search/reset). The old target of 4500 was
reviewed and dropped: it could only be reached by moving domain logic out of
`ui/` (metric cosmetic). The P5 criterion is qualitative: no hand-built chrome,
one lifecycle, the duplication removed.

### The default package (`themes/default/`)

The cards' defaults are **documents**, not code. The package lives in
`themes/default/`: `archetypes.yaml` (the frames), `fragments.yaml` (the
reusable pieces) and `cards/*.yaml` (one document per card family).

- `core/theme_package.py`: the loader. `default_package()` reads the built-in
  package once per process; `Package.card_keys()` lists the keys and
  `Package.metadata()` the contract (form, category, mention, variables).
- `core/documents/semantic.py`: a document read into the Card IR. Unknown keys
  are an error (`card_unknown_key`), never a silent drop.
- `core/documents/merge.py`: the layers (archetype, fragments, the card's own
  keys). Maps merge, scalars: the last wins, lists replace unless they ask
  to append (`$append`), `null` removes, a cycle raises `card_cycle`.
- A document is **semantic**: it says what the card is (title, body, fields,
  footer, author, thumbnail) and points at the catalog for the copy
  (`{card.audit.ban.title}`, `{log.field.target.label}`). The copy never
  carries the markdown dialect: the serializer writes it.
- A text slot may hold **alternatives** (a list): the first one whose
  variables are all known wins, otherwise the one with the fewest unknown
  placeholders. It is how the log footer says "the actor when there is one"
  and how the reply card says "the parent reference and the number, or the
  number alone".
- The **body** of a card is a sequence of paragraphs (`Card.paragraphs`): the
  `description` slot holds one, the `paragraphs` slot holds several. Each
  form joins them its own way (the embed with a line break, the V2 with one
  block per paragraph), and the form never rewrites the copy: what the
  catalog writes is what the member reads. An **empty paragraph disappears**
  (the line whose data never arrived does not exist), so a card that says
  more than one thing never shows half a line.
- The **footer** is frame, not copy: the card without a footer is still a
  card, and the footer never makes a card say something. A multi-line footer
  becomes one small (`-#`) line per line in the V2 (the marker belongs to
  the line) and keeps the line breaks in the embed.
- A **field** may carry a `gate`: the row disappears when the variable it
  names never arrived. It is how a composed value says "this line is only
  about that".
- The `color` slot names a theme token and resolves like any other slot: the
  **state** chooses it (the document writes the alternatives:
  `color: ['{state_color}', 'warning']`) and the theme keeps the meaning. A
  hex is accepted as it is. A token that never arrived is dropped and the
  card ships without an accent bar.
- A card with no copy at all (the catalog has none for it) renders as
  `None`: the call site keeps its own fallback instead of shipping an empty
  frame.
- The `actor` slot names who did it: it resolves first and answers `{actor}`
  everywhere in the card. An unresolved actor never overwrites the value the
  send site already sent.
- A URL slot (thumbnail, author_icon, image) with an unresolved placeholder is
  **dropped**, not rendered broken. The same for the author line and for a
  **link button whose URL never arrived**: a link with no URL would 400 the
  whole payload, so the button does not exist. A button that never declared a
  URL stays (it is a code-owned picker).
- `legacy_body` keeps the old theme override alive: the body card a guild
  wrote under `cards.<key>.description` still wins over the packaged body,
  and the document's frame (title, color, footer) stays.
- `core/card_specs.py` derives the specs from the package: the document
  declares the shape, the category, the mention policy and the variable
  contract.
- `core/log_specs.py` and `core/log_cards.py` are gone: the 75 staff log
  events are documents (`themes/default/cards/logs.yaml`). The archetypes
  `log_event` (the footer attributes the actor) and `log_named` (the actor is
  already in the content) carry the frame.
- The audit family lives in `themes/default/cards/audit.yaml` too. The
  archetype `audit_event` carries the shared frame (the member owns the
  author line, the footer names the actor). The optional lines of a body
  (the reason, the moderator, the transcript, the attachments) are composed
  by the send site and arrive as variables: the card never shows half a
  line.
- The names of a card's contract are **reserved**: when the send site does
  not provide one, the placeholder stays literal instead of falling back to
  a theme emoji with the same name (without it, a missing `{user}` rendered
  the theme's "user" emoji as the author). The contract lists what the send
  site **provides**: a name the copy uses as an emoji token (`members`,
  `user`) is not part of it, or the theme's icon would be reserved away.
- A slot that is exactly one catalog reference (`{a.b.c}`) names the copy of
  that key: when the catalog has none, the slot stays **empty** (the card
  has nothing to say and the send site keeps its own fallback) instead of
  shipping the raw key to the member.
- The notices live in `themes/default/cards/notices.yaml` (30 cards: the
  acknowledgements, the channel notices, the confirmations and the plain-text
  DMs). The archetype `notice` keeps the surface (one line) and the `style`
  slot names the tone of the notice (the theme owns it). The icon is **not**
  in the document: it is in the copy (a theme emoji token), so the text form
  ships the catalog's words verbatim. A notice that says more than one thing
  uses the slots (the ping answers with a title and the latency): the text
  serializer joins them, the join is the dialect of the form, never the copy.
- The **event DMs** are embeds now (`themes/default/cards/content.yaml`): the
  boost invite, the three bump winners and the six partnership notices. Each
  one declares its `content_line` (the sentence the member reads first) and
  the embed carries the details. The moderation notices (ban, kick, mute,
  warn, trap) and the confession ban stay **text**: the title opens in bold
  and the fields ride as bullets (`• **Label:** value`).
- The boost invite is **one message**: the content line, the embed with the
  role and the accept/decline buttons. A Components V2 message cannot carry
  content nor an embed, so `BoostInviteView` is a classic `discord.ui.View`
  (`ui/boost_invite.py`): the outcome replaces the content line and disables
  the buttons, the embed stays.
- The listings live in `themes/default/cards/lists.yaml` (20 cards: the
  invites, the leaderboards, the birthdays, the partnerships, the about
  card). The archetype `listing` keeps the frame (an embed, no ping, the
  full editor). The title and the body come either from the catalog (the
  copy of the feature) or from the send site (the `{title}` and `{body}`
  variables). A body of several paragraphs uses the `paragraphs` slot (the
  about card: the text and the version line).
- The listings' goldens changed for `bump.no_bumps`, `bump.reminder` and
  `trap.notice`: the legacy converter broke the body into lines to hunt the
  fields and dropped the blank lines with it, so the card showed less than
  the copy (and less than the code fallback of the same card). The IR keeps
  the copy as the catalog writes it.
- The content family lives in `themes/default/cards/content.yaml`: the
  starboard, the suggestions (the card and the three DMs), the confessions
  (the card, the reply and the three DMs), the roleplay, the help list and
  the warnings report. The archetype `info_card` carries the frame of a
  subject card (the image rides the title, the body tells the story) and
  `panel` the frame of an interactive card (the components live in code, the
  document owns the heading, the color and the body).
- There is no **builder** left: every card is a document, so the document is
  the only source of truth (the shape, the category, the mention policy and
  the contract). A card that needs a new slot adds it to the document (and
  the catalogs), never to a builder in Python.
- The contract is the **copy** the send site delivers: the variables the
  document reads. A color token the state chooses (`state_color`) is not
  part of it (the theme owns what the token means), and neither is a name
  the copy uses as an emoji token.
- The composed lines (the reason, the moderator, the motive) are written by
  the send site and arrive as variables (`reason_line`, `moderator_line`):
  the card never shows half a line. The golden harness fills them the same
  way (`EXTRA_LINES`, `EXTRA_LINES_BY_KEY`).
- The state a card shows (the ramp of the starboard, the balance of the
  suggestions, the style of the bump) is computed by the send site and
  handed to the document as a token (`state_color`): the golden harness
  computes it too (`STATE_BY_KEY`), so the fixture shows the color a member
  really receives.
- `core/debug.py::send_log` injects what the document asks for: the `actor`
  (from the `actor` slot) and the old `<variable>_label` names, so a theme
  written before the fields migration keeps rendering.

## Cards, themes, mentions

- A theme is a **package**: a folder with `theme.yaml` (the manifest: the
  name, the version, the parent), `tokens.yaml` (the look & feel),
  `archetypes.yaml`/`fragments.yaml` (the frames) and `cards/*.yaml` (the
  documents). The built-in one lives in `themes/default/` and feeds BOTH the
  fallback (`bot.theme`) and the selectable "default" in `/themes`. The old
  single file (`themes/<name>.yaml`) still loads: a theme = one YAML with
  `colors`/`emojis`/`markdown`/`styles` + optional `cards:`/`pings:`/
  `color_panel:`/`snapshot:`. Active theme resolves via sync
  `theme_for(bot, guild_id)` (boot snapshot).
- **A theme extends another one** (`extends: <name>`): the chain merges from
  the root to the child (the tokens deeply, the card documents as documents:
  the fields the child does not declare are inherited). A cycle is rejected
  (`theme_extends_cycle`), a chain deeper than 8 too (`theme_extends_depth`),
  a missing parent too (`theme_not_found`).
- A theme's card entry is either a **legacy full card** (the raw Components
  V2, the embed entry, the plain string: it replaces the whole card) or a
  **semantic document** (a delta over the packaged document: the fields it
  does not declare are inherited). A delta wins over a legacy card of the
  same key. `{"form": ...}` only picks the shape of the default content.
- **`/themes` inspects a theme**: *validate* reports what is wrong with it
  (the cards it names, the copy it points at, the variables it fills),
  *diff* says what it changes against the built-in one and *preview* renders
  one of its cards in each shape (the plain text, the classic embed, the
  Components V2). The logic lives in `core/theme_tools.py`.
- **A guild theme extends the built-in one**: the tokens it does not define
  (`colors`, `emojis`, `markdown`, `styles`) keep the built-in value
  (`core/themes.py::baseline_theme`). A theme customizes the bot, it never
  leaves it half-dressed: without the baseline, a partial theme would take
  every card's accent away (the color token unknown) and ship raw
  `{emoji}` placeholders. The theme's own tokens always win.
- New customizable card = a document in `themes/default/cards/*.yaml` (the
  shape, the category, the mention policy and the contract) + the copy in
  BOTH catalogs. Labels in `variables.<name>.label`/`.description` (both
  catalogs); samples in `core/variables.py`.
- **`cards:` values are raw Components V2 JSON** validated at load (40 components,
  depth 5, 4000 chars); invalid cards log and fall back to the built-in default,
  never break a send. `embed: {...}` + optional `buttons:` is the per-card classic
  alternative and `form: card|embed|text` only picks the shape of the default
  content. Send sites use `CardPayload.message_kwargs()` and never branch on form.
- Log events are CardSpecs too: `cards.<event>` is a full-card override, while
  the legacy `cards.<event>.description` stays a **body-only** override and old
  `pings:<event>.description` toggles map to the new event key. Shared field
  labels live in `log.field.*.label` (both catalogs) and carry theme emoji
  tokens. `avatar_var` switches a default log frame to the audit-style section
  with a right-side thumbnail; call sites pass the matching avatar URL.
- **No card may drop the variables the send site delivers**: the contract of
  the document is what the send site fills, and a placeholder outside it stays
  literal (swept by
  `test_audit.py::test_every_card_renders_with_the_variables_of_its_contract`).
  The stat labels (`card.<key>.<name>_label`) must exist verbatim in both
  catalogs; a name mismatch leaks raw keys into live cards.
- **Mentions are content**: ping position is wherever `{@name}` appears in card
  text. The renderer collects `<@id>`/`<@&id>` tokens and builds `AllowedMentions`:
  never pass `allowed_mentions` by hand for card sends. Per-card toggles live in
  the theme `pings:` section. Every `CardSpec` render must go through
  `render_card_message` (or explicit `trace_card_path`, in `core/debug.py`). An
  embed card moves the essential ping to the message `content` (an embed never
  notifies).
- **Emojis never hardcoded, icons are tokens**: `t()` auto-injects every
  `emojis:` theme entry as a format placeholder (explicit variables win, so never
  name an emoji token like a variable). Colors via `theme.color("<name>")`. A UI
  icon is a token resolved by `Theme.icon(name)`: the value is Unicode
  (`"🛡️"`) or an asset reference (`"{emojis/shield}"`). The reference resolves
  with `core/assets.resolve_refs` to the real application-emoji mention, so the
  same token works in a card, a button, a select, the chrome and a markdown
  template. An unknown token, or an asset that was never uploaded, resolves to
  empty: the raw `{emojis/...}` never ships. Every `SettingCategory` has a
  `category_<value>` token and every `SettingType` a `type_<value>` token; the
  close of a themed panel is `secondary` with the `door` token. Component
  `emoji=` must be RGI-valid or Discord 400s the whole payload.
- **Custom emoji recipe**: drop `assets/emojis/<name>.png` (an application
  emoji), use `{emojis/<name>}` as the value of a theme token, and restart the
  bot: the boot uploads only the files Discord does not have yet. Per-server
  emoji (`assets/emojis/<server>/`) stays reserved.
- **Machine values render as inline code in every card**: snowflake IDs, command
  usages (`!ban`, `/configuracoes`), the text-command prefix, version/channel and
  file names sit inside single backticks. Mentions, `<t:...>` timestamps, plain
  counters and links never do. Swept in both catalogs and the info-family
  footers by `tests/test_card_formatting.py`.
- Panels (`core/panels.py`): panels register repaint callback + setting keys of
  interest; `on_theme_changed` repaints + re-syncs dispatch, `repaint_all` at boot.
  Every code path editing theme files must call `ThemeStore.invalidate_guild`
  (themes cache per `(guild_id, name)`).
- The heading of an interactive menu is theme-customizable: `ui/heading.py`
  (`menu_heading_items`) renders the theme's own document and splices it in
  place of the code heading. Only the theme's override answers there (the
  packaged document is the frame the menu code already draws), and a heading
  that carries a container is rejected (the menu brings its own).
- `cardact:` button ids are `cardact:<card_key>:<button_id>` from the stored
  card, not generated hashes: keep button ids stable or old messages stop
  dispatching. `core/card_actions.py` rebuilds views from themed docs.

## Interaction / py-cord gotchas

- ACK every interaction (`defer()`/`send_message`/modal) before slow work; past 3s
  Discord shows "application did not respond". Guard with
  `if not interaction.response.is_done()`.
- Interaction tokens live 15 minutes. A gateway RESUME replays the clicks made
  while the bot was offline, and Discord answers 404/10062 ("Unknown
  interaction") to those dead tokens. ACK through `core/interactions.ack()`
  (or `MenuView._ack`): it returns False for a dead token, so the handler stops
  before the work and the log shows a warning instead of a traceback.
- py-cord `BaseView.on_error` only prints to stderr; `RosemaryBot.on_view_error`
  logs + ACKs `general.unknown_error`. Never swallow `discord.HTTPException`
  around `interaction.edit()` with bare `return`: a 400 looks like "click does nothing".
- Modal submits follow the same rule: ACK through `core/interactions.ack()` and
  stop when it returns False (a replayed submit must not post). The modals built
  by `ui/modals.py` carry `report_modal_error`, which routes the failure to
  `RosemaryBot.on_modal_error` (log + ACK) instead of py-cord's raw
  "Ignoring exception in modal" print.
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
