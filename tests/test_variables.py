"""Variable contract enforcement for customizable cards.

Every CardSpec declares the placeholder names its send site provides
(``CardSpec.variables``). These tests lock the triangle shut: specs only use
registered variables, hints only document contracted names or theme emojis,
and previews cover every contracted name.
"""

from __future__ import annotations

import re

import yaml

import rosemary.core.card_specs  # noqa: F401  (fills the registry)
from rosemary.core.cards import all_cards
from rosemary.core.variables import ALIASES, VARIABLES, samples_for
from rosemary.ui.theme import load_theme


def _catalog(code: str) -> dict:
    with open(f"language/{code}.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _nested(catalog: dict, key: str) -> dict:
    node = catalog.get("card") or {}
    for part in key.split("."):
        if not isinstance(node, dict):
            return {}
        node = node.get(part) or {}
    return node if isinstance(node, dict) else {}


def test_every_spec_variable_is_registered():
    unknown = []
    for spec in all_cards():
        for name in spec.variables:
            if name not in VARIABLES and name not in ALIASES:
                unknown.append(f"{spec.key}:{name}")
    assert unknown == []


def test_preview_covers_every_contract():
    gaps = []
    for spec in all_cards():
        covered = set(samples_for(spec.variables))
        for name in spec.variables:
            if name not in covered and name not in ALIASES:
                gaps.append(f"{spec.key}:{name}")
    assert gaps == []


def test_hint_placeholders_match_contract_or_theme():
    """catalog hints may only name contracted vars, aliases or theme emojis."""
    theme = load_theme()
    allowed_extra = set(theme.emojis) | set(ALIASES) | set(ALIASES.values())
    bad = []
    for code in ("en-US", "pt-BR"):
        catalog = _catalog(code)
        for spec in all_cards():
            entry = _nested(catalog, spec.key)
            hint = entry.get("placeholders") or ""
            names = set(re.findall(r"\{(\w+)\}", hint))
            for name in names:
                if name not in spec.variables and name not in allowed_extra:
                    bad.append(f"{code}:{spec.key}:{{{name}}}")
    assert bad == []


def test_variables_have_labels_in_both_catalogs():
    missing = []
    for code in ("en-US", "pt-BR"):
        catalog = _catalog(code)
        variables = catalog.get("variables") or {}
        for name in VARIABLES:
            entry = variables.get(name) or {}
            if not isinstance(entry.get("label"), str):
                missing.append(f"{code}:variables.{name}.label")
            if not isinstance(entry.get("description"), str):
                missing.append(f"{code}:variables.{name}.description")
    assert missing == []


async def test_schedule_override_resolves_ping_role(tmp_path):
    """The contracted {ping_role} actually resolves through text_or."""
    from rosemary.core.cards import card_store, text_or

    class FakeBot:
        theme = load_theme()

        def __init__(self):
            from rosemary.core.storage import GuildStorage

            self.storage = GuildStorage(tmp_path)

    bot = FakeBot()
    await card_store(bot).save_document(
        1,
        "bump.schedule.open",
        {"v": 1, "blocks": [{"type": "text", "body": "OPEN {ping_role}"}]},
    )
    assert await text_or(bot, 1, "bump.schedule.open", "fallback", ping_role="<@&9>") == (
        "OPEN <@&9>"
    )


def test_lint_suggests_close_match():
    from rosemary.core.variables import lint_placeholders

    doc = {"v": 1, "blocks": [{"type": "text", "body": "hi {usre} {zzzqqq}"}]}
    issues = dict(lint_placeholders(doc, {"user", "server"}))
    assert issues["usre"] == "user"
    assert issues["zzzqqq"] is None
