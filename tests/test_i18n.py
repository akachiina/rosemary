"""Tests for ``rosemary.core.i18n`` (Translator) and the theme wiring."""

from __future__ import annotations

from pathlib import Path

import yaml
from catalog import catalog_files, flatten, load_catalog

from rosemary.core.i18n import Translator, _flatten
from rosemary.ui.theme import load_theme

ROOT = Path(__file__).resolve().parents[2]
LANG_DIR = ROOT / "rosemary" / "language"


def _resolver(language: str):
    async def resolve(guild_id):
        return language

    return resolve


def _read_keys(code: str) -> set[str]:
    return set(_flatten(load_catalog(code)))


async def test_catalogs_load_and_key_parity():
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert tr.available_languages == ["en-US", "pt-BR"]
    assert _read_keys("en-US") == _read_keys("pt-BR")
    assert _read_keys("en-US")  # non-empty catalog


def test_the_modular_catalogs_keep_the_reference_keys():
    """The split into feature files must not lose a single key.

    ``tests/catalog_reference.json`` pins the hash of the flattened keys of
    each catalog: the split is a mechanical move, so the **shape** of the
    catalog is frozen. The copy may change (a new wording), the key set may
    not (a lost key is a silent hole in the UI).
    """
    import hashlib
    import json

    reference = json.loads(
        (Path(__file__).resolve().parent / "catalog_reference.json").read_text(
            encoding="utf-8"
        )
    )
    for code, expected in reference.items():
        flat = flatten(load_catalog(code))
        raw = json.dumps(sorted(flat), ensure_ascii=False)
        found = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert found == expected["keys"], f"{code}: the catalog lost keys"


def test_every_catalog_file_carries_its_feature():
    """The modular layout: one file per feature, the sections never split."""
    for code in ("en-US", "pt-BR"):
        files = catalog_files(code)
        assert len(files) > 1, f"{code}: the catalog is not split"
        for path in files:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            assert isinstance(data, dict) and data, f"{path}: empty catalog file"


def test_no_boolean_keys():
    """YAML 1.1 parses unquoted on/off/yes/no/true/false as booleans.

    A boolean key flattens to e.g. ``cards.editor.mentions.true``, which no
    code requests by name: ``t()`` then falls back to the raw key and the UI
    renders it literally (the cards.editor.mentions.on/off bug).
    """

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                assert isinstance(key, str), (
                    f"boolean/None key parsed in catalog at {path} "
                    f"({key!r}); quote the key in both YAML files"
                )
                walk(value, f"{path}.{key}")

    for code in ("en-US", "pt-BR"):
        walk(load_catalog(code), code)


def test_no_duplicate_yaml_keys():
    """PyYAML silently keeps the LAST duplicate key, dropping the earlier
    subtree: a duplicated ``card.audit`` block once discarded the shared
    reason/moderator line fragments (live cards rendered without them)."""

    class DupCheckLoader(yaml.SafeLoader):
        pass

    def construct(loader, node):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=True)
            assert key not in seen, f"{code}: duplicate YAML key {key!r}"
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=True)

    DupCheckLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct
    )

    for code in ("en-US", "pt-BR"):
        for path in catalog_files(code):
            yaml.load(path.read_text(encoding="utf-8"), Loader=DupCheckLoader)


def test_settings_descriptions_fit_modal_placeholder():
    """Discord modal placeholders cap at 100 chars, so every settings
    description must stay within the limit (bump.detection_text regression)."""
    for code in ("en-US", "pt-BR"):
        data = load_catalog(code)
        for key, spec in (data.get("settings") or {}).items():
            if isinstance(spec, dict) and "description" in spec:
                assert len(spec["description"]) <= 100, (
                    f"{code} settings.{key}.description exceeds 100 chars"
                )


def test_every_setting_has_catalog_copy():
    """Every registry entry needs label/description (and choice labels).

    ``/settings`` renders these keys directly; a missing entry falls back to
    the raw dotted key instead of a user-facing string.
    """
    from rosemary.core.settings import SETTINGS

    for code in ("en-US", "pt-BR"):
        data = load_catalog(code)
        catalog = data.get("settings") or {}
        for key, spec in SETTINGS.items():
            entry = catalog.get(key)
            assert isinstance(entry, dict), f"{code} settings.{key} is missing"
            for field in ("label", "description"):
                value = entry.get(field)
                assert isinstance(value, str) and value.strip(), (
                    f"{code} settings.{key}.{field} is missing"
                )
            choices = entry.get("choices") or {}
            for choice in spec.choices:
                assert choice in choices, (
                    f"{code} settings.{key}.choices.{choice} is missing"
                )


async def test_default_catalog_keys_resolve():
    """Message-template defaults must exist in both catalogs.

    ``SettingsMenuView`` resolves ``default_catalog_key`` before the first
    save; a missing key would show the raw dotted key instead of the default.
    """
    from rosemary.core.settings import SETTINGS

    tr = Translator(LANG_DIR)
    for code in ("en-US", "pt-BR"):
        tr.resolver = _resolver(code)
        for spec in SETTINGS.values():
            if not spec.default_catalog_key:
                continue
            value = await tr.raw(1, spec.default_catalog_key)
            assert isinstance(value, str) and value != spec.default_catalog_key, (
                f"{code} lacks {spec.default_catalog_key}"
            )


async def test_t_default():
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert await tr.t(1, "about.title") == "About Rosemary"


async def test_t_per_guild():
    tr = Translator(LANG_DIR, resolver=_resolver("pt-BR"))
    assert await tr.t(1, "about.title") == "Sobre a Rosemary"


async def test_t_fallback_raw_key():
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert await tr.t(1, "no.such.key") == "no.such.key"


async def test_t_fallback_language():
    # Unknown catalog code resolves to the default language text, not the raw key.
    tr = Translator(LANG_DIR, resolver=_resolver("xx-XX"))
    assert await tr.t(1, "about.title") == "About Rosemary"


async def test_t_formatting():
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert await tr.t(1, "about.version", version="9.9.9", channel="stable") == (
        "Version: `9.9.9` (`stable`)"
    )
    # Missing variable: formatting error is swallowed, template returned unchanged.
    assert await tr.t(1, "about.version") == "Version: `{version}` (`{channel}`)"


async def test_missing_languages_dir_no_crash(tmp_path):
    tr = Translator(tmp_path / "nope", resolver=_resolver("en-US"))
    assert tr.available_languages == []
    assert await tr.t(1, "about.title") == "about.title"


async def test_language_meta_is_not_a_translation_key():
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert await tr.t(1, "language_meta.name") == "language_meta.name"
    assert await tr.t(1, "language_meta") == "language_meta"


async def test_language_display_uses_meta():
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert tr.language_display("en-US") == "🇺🇸 English"
    assert tr.language_display("pt-BR") == "🇧🇷 Português do Brasil"


async def test_command_metadata_is_localized():
    en = Translator(LANG_DIR, resolver=_resolver("en-US"))
    assert await en.t(1, "about.command.name") == "about"
    assert await en.t(1, "about.command.description") == "About Rosemary"
    assert await en.t(1, "language.command.name") == "language"
    assert await en.t(1, "language.command.description") == "Set the server language"

    pt = Translator(LANG_DIR, resolver=_resolver("pt-BR"))
    assert await pt.t(1, "about.command.name") == "sobre"
    assert await pt.t(1, "language.command.name") == "idioma"
    assert await pt.t(1, "language.command.description") == "Define o idioma do servidor"


async def test_theme_emojis_are_auto_injected():
    theme = load_theme()
    assert theme.emojis["success"] == "✅"
    tr = Translator(LANG_DIR, resolver=_resolver("en-US"), default_placeholders=theme.emojis)
    assert await tr.t(1, "settings.saved", setting="theme") == "✅ Value set for **theme**."


async def test_asset_references_resolve_in_any_catalog(tmp_path, monkeypatch):
    """A catalog may reference a shipped asset: it is an emoji, not a variable.

    ``str.format`` would choke on ``{emojis/op_emoji}``, so the reference is
    resolved before the format: an unknown one renders as nothing.
    """
    from rosemary.core import assets

    (tmp_path / "en-US.yaml").write_text(
        "mark: 'OP {emojis/op_emoji}'\nmissing: 'OP {emojis/nope}'\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(assets.REGISTRY._mentions, "emojis/op_emoji", "<:op_emoji:7>")
    tr = Translator(tmp_path, resolver=_resolver("en-US"))
    assert await tr.t(1, "mark") == "OP <:op_emoji:7>"

    monkeypatch.delitem(assets.REGISTRY._mentions, "emojis/op_emoji")
    assert await tr.t(1, "mark") == "OP "
    # An unknown reference never leaks its raw text.
    assert await tr.t(1, "missing") == "OP "


async def test_leaderboard_week_end_timestamp_renders():
    """The 'week ends in' line renders a relative timestamp in both catalogs."""
    from rosemary.core.cards import safe_format_mentions

    theme = load_theme()
    pt = Translator(LANG_DIR, resolver=_resolver("pt-BR"), default_placeholders=theme.emojis)
    # The winner line is a `{@winner_mention}` content mention: resolved with
    # safe_format_mentions, never t() (str.format chokes on the marker).
    template = await pt.raw(1, "bump.messages.leaderboard_description")
    desc, mentions = safe_format_mentions(
        template,
        {
            **theme.emojis,
            "start_timestamp": 111,
            "end_timestamp": 222,
            "next_reset_timestamp": 333,
            "winner_mention": "<@1>",
            "winner_count": 1,
            "role_name": "Bump-MVP",
            "leaderboard_list": "- x",
        },
    )
    assert "<t:333:R>" in desc
    assert "⏳" in desc
    assert "<@1>" in desc
    assert mentions.get("winner_mention") == ("user", 1)
    assert await pt.t(1, "bump.stats.week_end", next_reset_ts=444) == (
        "**⏳ A semana termina em:** <t:444:R>"
    )

    en = Translator(LANG_DIR, resolver=_resolver("en-US"), default_placeholders=theme.emojis)
    assert await en.t(1, "bump.stats.week_end", next_reset_ts=444) == (
        "**⏳ The week ends in:** <t:444:R>"
    )


async def test_arbitrary_placeholder_from_theme(tmp_path):
    # A user-added emoji key in theme.yaml is usable in any string without code.
    (tmp_path / "en-US.yaml").write_text(
        "x:\n  msg: \"emoji: {emoji_23}\"\n", encoding="utf-8"
    )
    tr = Translator(
        tmp_path,
        resolver=_resolver("en-US"),
        default_placeholders={"emoji_23": "🔥"},
    )
    assert await tr.t(1, "x.msg") == "emoji: 🔥"


async def test_explicit_variables_override_placeholders(tmp_path):
    (tmp_path / "en-US.yaml").write_text(
        "x:\n  msg: \"emoji: {success}\"\n", encoding="utf-8"
    )
    tr = Translator(
        tmp_path,
        resolver=_resolver("en-US"),
        default_placeholders={"success": "✅"},
    )
    assert await tr.t(1, "x.msg", success="custom") == "emoji: custom"


async def test_unknown_placeholder_does_not_crash(tmp_path):
    (tmp_path / "en-US.yaml").write_text(
        "x:\n  msg: \"emoji: {emoji_99}\"\n", encoding="utf-8"
    )
    tr = Translator(tmp_path, resolver=_resolver("en-US"))
    assert await tr.t(1, "x.msg") == "emoji: {emoji_99}"


async def test_invalid_message_multiline_available():
    theme = load_theme()
    tr = Translator(
        LANG_DIR, resolver=_resolver("pt-BR"), default_placeholders=theme.emojis
    )
    message = await tr.t(
        1,
        "language.invalid",
        language="xx-XX",
        available="- 🇺🇸 English\n- 🇧🇷 Português do Brasil",
    )
    assert "Disponíveis:" in message
    assert "\n- 🇺🇸 English\n- 🇧🇷 Português do Brasil" in message
    assert message.startswith("Idioma **desconhecido**")


def _document_label_keys(document: dict) -> list[str]:
    """The ``log.field.*.label`` keys a document asks for.

    The short form (``- target: "{member}"``) names the field, the long form
    (``- {label: "{log.field.target.label}", ...}``) carries the key.
    """
    keys: list[str] = []
    for field in document.get("fields") or []:
        if not isinstance(field, dict):
            continue
        label = field.get("label")
        if isinstance(label, str):
            key = label.strip().removeprefix("{").removesuffix("}")
            if key.startswith("log.field."):
                keys.append(key)
            continue
        name = next(iter(field), "")
        if name and name != "label":
            keys.append(f"log.field.{name}.label")
    return keys


def test_every_log_field_label_resolves_in_both_catalogs():
    """A missing ``log.field.*.label`` leaks the raw key into live log cards."""
    from rosemary.core.theme_package import default_package

    package = default_package()
    for code in ("en-US", "pt-BR"):
        data = load_catalog(code)
        missing = []
        for key in package.card_keys():
            for label in _document_label_keys(package.document(key) or {}):
                node = data
                for part in label.split("."):
                    node = node.get(part) if isinstance(node, dict) else None
                if not isinstance(node, str) or not node.strip():
                    missing.append(f"{code}:{key}:{label}")
        assert missing == []


def test_field_lines_carry_theme_emoji_token():
    """Field-style log bodies and panels use ``**{emoji} Label:**``.

    One meaning = one emoji (the semantic table in AGENTS.md), and the token
    must exist in the theme, otherwise the line renders with empty braces.
    Prose bodies (no labels) are intentionally exempt.
    """
    theme = load_theme()

    pt = load_catalog("pt-BR")
    en = load_catalog("en-US")

    expectations = {
        # Audit bodies: label fields carry the semantic emoji.
        ("card", "audit", "message_delete", "body"): ("people", "speech"),
        ("card", "audit", "message_edit", "body"): ("people", "speech"),
        # bulk_delete's moderator arrives via the optional {moderator_line}
        # fragment (pre-resolved at the send site), so the body guarantees
        # only the fixed lines' tokens.
        ("card", "audit", "bulk_delete", "body"): ("numbers",),
        ("card", "audit", "nickname", "body"): ("pencil",),
        ("card", "audit", "avatar", "body"): ("frame",),
        # Cleaner panel lines.
        ("cleaner", "progress_criterion"): ("search",),
        ("cleaner", "progress_current"): ("speech",),
        ("cleaner", "progress_done"): ("numbers",),
        ("cleaner", "progress_scanned"): ("search",),
        ("cleaner", "progress_counts"): ("trash",),
        ("cleaner", "progress_elapsed"): ("clock",),
    }
    for code, data in (("pt-BR", pt), ("en-US", en)):
        for path, tokens in expectations.items():
            node = data
            for part in path:
                node = node[part]
            for token in tokens:
                assert f"{{{token}}}" in node, f"{code}:{'.'.join(path)} missing {token}"
                assert theme.emojis.get(token), f"theme emoji {token} missing"

        # Shared log-field labels flow into **{label}:** bodies.
        for label_key, token in (
            ("target", "people"),
            ("moderator", "gear"),
            ("reason", "scroll"),
            ("duration", "clock"),
        ):
            value = data["log"]["field"][label_key]["label"]
            assert f"{{{token}}}" in value, f"{code}:log.field.{label_key}"

        # ban/unban bodies keep the prose first line; the moderator rides in
        # the optional {moderator_line} fragment, which carries {gear}.
        for event in ("ban", "unban"):
            body = data["card"]["audit"][event]["body"]
            assert "{moderator_line}" in body, f"{code}:card.audit.{event}.body"
        fragment = data["card"]["audit"]["moderator_line"]
        assert "{gear}" in fragment, "card.audit.moderator_line"
        assert theme.emojis.get("gear")
