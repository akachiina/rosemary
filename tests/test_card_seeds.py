"""Every customizable card seeds its editor from real catalog content.

The /personalizar editor must never open on an empty "1. Texto (vazio)"
skeleton: :func:`catalog_default_document` resolves what members receive
today — builder, seed map or ``card.<key>``/``<key>`` scalars. These tests
sweep the whole card registry against the real catalogs so a card whose copy
moves to a new catalog key fails here instead of showing an empty editor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rosemary.cogs.birthdays  # noqa: F401  (registers announce builder)
import rosemary.cogs.welcome  # noqa: F401  (registers event builders)
import rosemary.core.card_specs  # noqa: F401  (fills the registry)
from rosemary.core.card_service import catalog_default_document
from rosemary.core.cards import all_cards, card_store
from rosemary.core.i18n import Translator
from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme


class CatalogBackedTranslator:
    """Real catalogs: ``t``/``raw`` resolve exactly like production."""

    def __init__(self) -> None:
        self._translator = Translator(Path("language"))

    async def t(self, guild_id, key, **variables):
        return await self._translator.t(guild_id, key, **variables)

    async def raw(self, guild_id, key):
        return await self._translator.raw(guild_id, key)


class CatalogBot:
    theme = load_theme()

    def __init__(self, tmp_path) -> None:
        self.storage = GuildStorage(tmp_path)
        self.translator = CatalogBackedTranslator()

    def get_guild(self, guild_id):
        return None


def _iter_texts(blocks):
    """Yield every text block, descending into containers/sections."""
    for block in blocks or []:
        if block.get("type") == "text":
            yield block
        yield from _iter_texts(block.get("children") or [])


def _text_bodies(doc: dict) -> list[str]:
    """Every text body in the document, in render order."""
    return [block.get("body", "") for block in _iter_texts(doc.get("blocks") or [])]


@pytest.mark.parametrize("language", ["en-US", "pt-BR"])
async def test_every_card_seeds_nonempty_from_catalog(tmp_path, language):
    """No card may resolve an empty document in either catalog language."""

    class LanguageBot(CatalogBot):
        def __init__(self, tmp_path):
            super().__init__(tmp_path)
            self._translator = self.translator._translator
            self.translator._translator.resolver = lambda _gid: language

    bot = LanguageBot(tmp_path)
    empty = []
    for spec in all_cards():
        doc = await catalog_default_document(bot, 1, spec.key)
        if doc is None or not any(body.strip() for body in _text_bodies(doc)):
            empty.append(spec.key)
    assert empty == []


async def test_seed_matches_real_feature_copy(tmp_path):
    """Spot-checks: the seeded text is the copy the feature itself renders."""
    bot = CatalogBot(tmp_path)

    about = await catalog_default_document(bot, 1, "about.card")
    bodies = "\n".join(_text_bodies(about))
    assert "# About Rosemary" in bodies  # about.title as the heading
    assert "minimal, multilingual" in bodies  # about.text

    warn = await catalog_default_document(bot, 1, "warn.dm")
    assert "{guild}" in _text_bodies(warn)[0]  # placeholders stay literal

    log = await catalog_default_document(bot, 1, "moderation.logs.ban.description")
    assert "{member_label}" in _text_bodies(log)[0]


async def test_editor_opens_seeded_for_about_card(tmp_path):
    """The reported bug: /personalizar on about.card shows real copy."""
    from rosemary.ui.card_editor import CardEditorView

    bot = CatalogBot(tmp_path)

    async def load_doc(guild_id):
        return await card_store(bot).get_document(guild_id, "about.card")

    async def save_doc(guild_id, doc):
        await card_store(bot).save_document(guild_id, "about.card", doc)

    async def reset_doc(guild_id):
        await card_store(bot).reset(guild_id, "about.card")

    view = CardEditorView(
        bot, 1, "about.card",
        load_doc=load_doc, save_doc=save_doc, reset_doc=reset_doc, owner_id=1,
    )
    await view.prepare()
    assert view.using_default_base is True
    rendered = "\n".join(_text_bodies(view.document()))
    assert "About Rosemary" in rendered
    assert all(block.get("body", "").strip() for block in _iter_texts(view.blocks))


async def test_reset_restores_catalog_seed_not_skeleton(tmp_path):
    """After Reset, the editor returns to the catalog copy (never empty)."""
    from rosemary.core.card_actions import sync_guild  # noqa: F401
    from rosemary.ui.card_editor import CardEditorView

    bot = CatalogBot(tmp_path)
    store = card_store(bot)
    await store.save_document(
        1, "about.card", {"v": 1, "blocks": [{"type": "text", "body": "custom"}]}
    )

    async def load_doc(guild_id):
        return await store.get_document(guild_id, "about.card")

    async def save_doc(guild_id, doc):
        await store.save_document(guild_id, "about.card", doc)

    async def reset_doc(guild_id):
        await store.reset(guild_id, "about.card")

    view = CardEditorView(
        bot, 1, "about.card",
        load_doc=load_doc, save_doc=save_doc, reset_doc=reset_doc, owner_id=1,
    )
    await view.prepare()

    class FakeResponse:
        def __init__(self):
            self._done = False

        def is_done(self):
            return self._done

        async def defer(self, *, ephemeral=False):
            self._done = True

    class FakeInteraction:
        def __init__(self):
            self.response = FakeResponse()

        async def edit(self, **kwargs):
            pass

    await view._reset(FakeInteraction())  # arms
    await view._reset(FakeInteraction())  # confirms
    assert view.using_default_base is True
    bodies = _text_bodies({"blocks": view.blocks})
    assert any("About Rosemary" in body for body in bodies)
    assert await load_doc(1) is None  # override removed


def _count_components(view) -> int:
    """Discord-style component count, nesting included (same as cap test)."""

    def walk(node: dict) -> int:
        return 1 + sum(walk(child) for child in node.get("components", []) or [])

    return sum(walk(item.to_component_dict()) for item in view.children)


@pytest.mark.parametrize("flash", [False, True])
async def test_seeded_editor_states_with_mentions_stay_under_limit(tmp_path, flash):
    """A seeded catalog document keeps every editor state under the cap."""
    from rosemary.ui.card_editor import CardEditorView

    bot = CatalogBot(tmp_path)
    store = card_store(bot)
    policies: dict[int, str] = {1: "role"}

    async def load_doc(guild_id):
        return await store.get_document(guild_id, "about.card")

    async def save_doc(guild_id, doc):
        await store.save_document(guild_id, "about.card", doc)

    async def reset_doc(guild_id):
        await store.reset(guild_id, "about.card")

    async def load_mentions(guild_id):
        return policies.get(guild_id)

    async def save_mentions(guild_id, policy):
        policies[guild_id] = policy

    view = CardEditorView(
        bot, 1, "about.card",
        load_doc=load_doc, save_doc=save_doc, reset_doc=reset_doc, owner_id=1,
        load_mentions=load_mentions, save_mentions=save_mentions,
    )
    states = []
    for state in ("fresh", "selected", "sublevel", "adding", "more", "compare"):
        view.loaded = True
        view.adding = view.comparing = False
        view._show_more = False
        view.selected = None
        view.path.clear()
        if state == "selected":
            view.selected = 0
        elif state == "sublevel":
            view.path = [0]
        elif state == "adding":
            view.adding = True
        elif state == "more":
            view._show_more = True
        elif state == "compare":
            view.comparing = True
        if flash:
            view.flash = "cards.editor.saved"
        await view.prepare()
        total = _count_components(view)
        states.append((state, total))
        assert total <= 38, states
