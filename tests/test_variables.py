"""Variable contract enforcement for customizable cards.

Every card declares the placeholder names its send site provides (the
document's ``variables``). These tests lock the triangle shut: the specs
only use registered variables, the contracts only name registered names and
the previews cover every contracted name.
"""

from __future__ import annotations

from catalog import load_catalog

import rosemary.core.card_specs  # noqa: F401  (fills the registry)
from rosemary.core.cards import all_cards
from rosemary.core.variables import ALIASES, VARIABLES, samples_for
from rosemary.ui.theme import load_theme


def _catalog(code: str) -> dict:
    return load_catalog(code)


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


def test_the_document_contracts_only_name_registered_variables():
    """The contract of a card is its document's ``variables``.

    Every name it declares must be a registered variable (or an alias), so
    the send site can provide it and the catalogs can label it. The old
    per-card ``placeholders`` hints are gone: they duplicated the contract.
    """
    from rosemary.core.theme_package import default_package

    package = default_package()
    bad = []
    for key in package.card_keys():
        contract = package.metadata(key).get("variables") or ()
        for name in contract:
            if name not in VARIABLES and name not in ALIASES:
                bad.append(f"{key}:{name}")
    assert bad == []


def test_the_catalogs_carry_no_contract_hints():
    """The hints are gone: the contract lives in the document, not the copy."""
    from catalog import load_catalog

    for code in ("en-US", "pt-BR"):
        catalog = load_catalog(code)
        for key, entry in (catalog.get("card") or {}).items():
            if isinstance(entry, dict):
                assert "placeholders" not in entry, f"{code}:card.{key}.placeholders"


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
    from rosemary.core.cards import text_or
    from rosemary.core.storage import GuildStorage
    from rosemary.core.themes import ThemeStore, preload_themes

    themes_dir = tmp_path / "themes"
    themes_dir.mkdir(exist_ok=True)
    (themes_dir / "t.yaml").write_text(
        "name: t\n"
        "cards:\n"
        "  bump.schedule.open:\n"
        "    - type: 10\n"
        "      content: \"OPEN {ping_role}\"\n",
        encoding="utf-8",
    )

    class FakeBot:
        theme = load_theme()

        def __init__(self):
            self.storage = GuildStorage(tmp_path)
            self._theme_store = ThemeStore(tmp_path, themes_dir=themes_dir)
            self.guilds = [type("G", (), {"id": 1})()]

    bot = FakeBot()
    await bot._theme_store.set_active(1, "t")
    await preload_themes(bot)
    assert await text_or(bot, 1, "bump.schedule.open", "fallback", ping_role="<@&9>") == (
        "OPEN <@&9>"
    )


def test_lint_suggests_close_match():
    from rosemary.core.variables import lint_placeholders

    doc = {"v": 1, "blocks": [{"type": "text", "body": "hi {usre} {zzzqqq}"}]}
    issues = dict(lint_placeholders(doc, {"user", "server"}))
    assert issues["usre"] == "user"
    assert issues["zzzqqq"] is None
