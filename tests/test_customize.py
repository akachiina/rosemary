"""Card registry, picker menu and settings deep-link (F0c)."""

from __future__ import annotations

import pytest
import yaml

import rosemary.core.cards as cards_module
from rosemary.core.cards import (
    CardSpec,
    card_store,
    cards_for_category,
    get_card,
    register_cards,
)
from rosemary.core.storage import GuildStorage
from rosemary.ui.card_editor import CardEditorView
from rosemary.ui.customize_menu import CustomizeMenuView
from rosemary.ui.settings_menu import SettingsMenuView
from rosemary.ui.theme import load_theme

SPEC = CardSpec(key="test.hello", category="general", rich=False)


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


class FakeUser:
    id = 1


class FakeResponse:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._order.append("defer")
        self._done = True


class FakeFollowup:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def send(self, *args, **kwargs) -> None:
        self._order.append("send")


class FakeInteraction:
    def __init__(self, custom_id="", data=None):
        self.order: list[str] = []
        self.response = FakeResponse(self.order)
        self.followup = FakeFollowup(self.order)
        self.data = data or {}
        self.custom_id = custom_id
        self.user = FakeUser()
        self.message = None
        self.edit_kwargs: dict = {}

    async def edit(self, **kwargs) -> None:
        self.edit_kwargs = kwargs
        self.order.append("edit")


def make_bot(tmp_path):
    class Bot:
        theme = load_theme()
        translator = FakeTranslator()

        def __init__(self):
            self.storage = GuildStorage(tmp_path)

    return Bot()


@pytest.fixture
def clean_registry():
    """Empty the global card registry for isolation, restoring afterwards."""
    snapshot = dict(cards_module._CARDS)
    cards_module._CARDS.clear()
    yield
    cards_module._CARDS.clear()
    cards_module._CARDS.update(snapshot)


@pytest.fixture
def with_spec(clean_registry):
    register_cards(SPEC)
    yield SPEC


# -- registry ----------------------------------------------------------------


def test_register_and_lookup(with_spec):
    assert get_card("test.hello") is SPEC
    assert cards_for_category("general") == [SPEC]
    assert cards_for_category("boost") == []
    assert SPEC.title_key == "card.test.hello.title"
    assert SPEC.placeholders_key == "card.test.hello.placeholders"


async def test_default_store_roundtrip(tmp_path):
    bot = make_bot(tmp_path)
    await card_store(bot).save_document(1, "x", {"v": 1, "blocks": []})
    assert (tmp_path / "1" / "cards.json").exists()


# -- catalogs parity for every registered spec --------------------------------


def _catalog(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _nested_entry(catalog: dict, key: str) -> dict:
    """Walk the dotted ``key`` through the nested ``card:`` YAML section."""
    node: object = catalog.get("card") or {}
    for part in key.split("."):
        if not isinstance(node, dict):
            return {}
        node = node.get(part) or {}
    return node if isinstance(node, dict) else {}


def test_every_registered_card_has_labels_in_both_catalogs():
    en = _catalog("language/en-US.yaml")
    pt = _catalog("language/pt-BR.yaml")
    for spec in cards_module.all_cards():
        for catalog in (en, pt):
            entry = _nested_entry(catalog, spec.key)
            assert isinstance(entry.get("title"), str), f"{lang_tag(catalog)}:{spec.key}"
            assert isinstance(entry.get("placeholders"), str), f"{lang_tag(catalog)}:{spec.key}"


def lang_tag(catalog: dict) -> str:
    return "en-US" if catalog.get("language", {}).get("name") == "English" else "pt-BR"


# -- picker menu ---------------------------------------------------------------


def _all_texts(view) -> str:
    found: list[str] = []

    def walk(node):
        content = getattr(node, "content", None)
        if isinstance(content, str):
            found.append(content)
        for child in getattr(node, "items", []) or []:
            walk(child)

    for child in view.children:
        walk(child)
    return "\n".join(found)


async def test_picker_empty_state(tmp_path, clean_registry):
    view = CustomizeMenuView(make_bot(tmp_path), 1, owner_id=1)
    await view.prepare()
    assert "cards.customize.empty" in _all_texts(view)


async def test_picker_lists_spec_and_opens_editor(tmp_path, with_spec):
    view = CustomizeMenuView(make_bot(tmp_path), 1, owner_id=1, category="general")
    await view.prepare()
    interaction = FakeInteraction(data={"values": ["test.hello"]})
    await view._pick_card(interaction)
    assert interaction.order == ["defer", "edit"]
    assert isinstance(interaction.edit_kwargs["view"], CardEditorView)
    editor = interaction.edit_kwargs["view"]
    assert editor.key == "test.hello"
    assert editor.placeholders_hint == "card.test.hello.placeholders"


async def test_editor_exit_returns_to_picker(tmp_path, with_spec):
    view = CustomizeMenuView(make_bot(tmp_path), 1, owner_id=1, category="general")
    await view.prepare()
    opened = FakeInteraction(data={"values": ["test.hello"]})
    await view._pick_card(opened)
    editor = opened.edit_kwargs["view"]

    back = FakeInteraction()
    await editor._exit_to_origin(back)
    assert back.order == ["defer", "edit"]
    returned = back.edit_kwargs["view"]
    assert isinstance(returned, CustomizeMenuView)
    assert returned.category == "general"


async def test_picker_back_returns_to_categories(tmp_path, with_spec):
    view = CustomizeMenuView(make_bot(tmp_path), 1, owner_id=1, category="general")
    await view.prepare()
    interaction = FakeInteraction(custom_id="custom_back")
    await view._back_to_categories(interaction)
    assert interaction.order == ["defer", "edit"]
    assert view.category is None


# -- /settings deep-link -------------------------------------------------------


async def test_settings_button_appears_for_category_with_cards(tmp_path, with_spec):
    view = SettingsMenuView(make_bot(tmp_path), 1, owner_id=1)
    await view.prepare()
    ids = [
        item.custom_id for item in view.walk_children() if getattr(item, "custom_id", None)
    ]
    assert "settings_customize" in ids


async def test_settings_no_button_without_cards(tmp_path):
    from rosemary.core.settings import SettingCategory as _Category

    view = SettingsMenuView(make_bot(tmp_path), 1, owner_id=1)
    view.category = _Category.LOGGING  # no customizable cards in this category
    await view.prepare()
    ids = [
        item.custom_id for item in view.walk_children() if getattr(item, "custom_id", None)
    ]
    assert "settings_customize" not in ids


async def test_settings_customize_opens_picker(tmp_path, with_spec):
    view = SettingsMenuView(make_bot(tmp_path), 1, owner_id=1)
    await view.prepare()
    interaction = FakeInteraction(custom_id="settings_customize")
    await view._open_customize(interaction)
    assert interaction.order == ["defer", "edit"]
    picker = interaction.edit_kwargs["view"]
    assert isinstance(picker, CustomizeMenuView)
    assert picker.category == "general"
