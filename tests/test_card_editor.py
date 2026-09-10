"""Card composer flows: select-based editing, breadcrumb, compare screen."""

from __future__ import annotations

import copy

import pytest

from rosemary.core.storage import GuildStorage
from rosemary.ui.card_editor import CardEditorView
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        suffix = "".join(f";{k}={v}" for k, v in sorted(variables.items()))
        return f"{key}{suffix}"


class FakeBot:
    theme = load_theme()
    translator = FakeTranslator()

    def __init__(self, tmp_path):
        self.storage = GuildStorage(tmp_path)
        self._guild = None

    def get_guild(self, guild_id):
        return self._guild


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
        from types import SimpleNamespace

        self.user = SimpleNamespace(id=1)
        self.message = None
        self.edit_kwargs: dict = {}

    async def edit(self, **kwargs) -> None:
        self.edit_kwargs = kwargs
        self.order.append("edit")


def make_view(tmp_path, key="test.card", *, exit_factory=None):
    store: dict[int, dict] = {}

    async def load_doc(guild_id):
        doc = store.get(guild_id)
        return copy.deepcopy(doc) if doc is not None else None

    async def save_doc(guild_id, doc):
        store[guild_id] = copy.deepcopy(doc)

    async def reset_doc(guild_id):
        store.pop(guild_id, None)

    bot = FakeBot(tmp_path)
    view = CardEditorView(
        bot,
        1,
        key,
        load_doc=load_doc,
        save_doc=save_doc,
        reset_doc=reset_doc,
        owner_id=1,
        exit_factory=exit_factory,
    )
    return view, store, bot


def all_texts(view) -> str:
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


def row_ids(view) -> list[list[str]]:
    rows = []
    for row in view.children:
        if isinstance(row, discord.ui.ActionRow):
            rows.append([item.custom_id for item in row.children])
    return rows


import discord  # noqa: E402


def _no_ids(blocks):
    """Copies of blocks without editor ids, for shape assertions."""
    import copy

    blocks = copy.deepcopy(blocks)

    def walk(items):
        for block in items:
            if isinstance(block, dict):
                block.pop("id", None)
                walk(block.get("children") or [])
                for button in block.get("buttons", []) or []:
                    if isinstance(button, dict):
                        button.pop("id", None)

    walk(blocks)
    return blocks

# -- basics ---------------------------------------------------------------------


async def test_prepare_loads_default_skeleton_without_banner(tmp_path):
    view, _store, _bot = make_view(tmp_path)
    await view.prepare()
    assert _no_ids(view.blocks) == [{"type": "text", "body": ""}]
    assert "cards.editor.invalid" not in all_texts(view)


async def test_prepare_seeds_from_catalog_when_no_builder(tmp_path):
    """No builder: the editor seeds from the card's live catalog copy."""
    from rosemary.core.card_service import catalog_default_document
    from rosemary.core.cards import CardSpec, register_cards

    class CatalogTranslator(FakeTranslator):
        async def raw(self, guild_id, key):
            catalog = {
                "card.test.card": "corpo atual {user}",
                "card.test.card.title": "Cartão teste",
            }
            return catalog.get(key, key)

    class CatalogBot(FakeBot):
        translator = CatalogTranslator()

    register_cards(CardSpec(key="test.card", category="zz-seed", rich=True))
    try:
        bot = CatalogBot(tmp_path)
        seed = await catalog_default_document(bot, 1, "test.card")
        assert seed is not None
        bodies = [b["body"] for b in seed["blocks"]]
        # FakeTranslator.t() echoes keys, so no heading is seeded (never a
        # raw catalog key in the UI); the resolved body seeds as-is.
        assert "## card.test.card.title" not in bodies
        assert "corpo atual {user}" in bodies  # placeholders stay literal

        store: dict[int, dict] = {}

        async def load_doc(guild_id):
            return store.get(guild_id)

        async def save_doc(guild_id, doc):
            store[guild_id] = copy.deepcopy(doc)

        async def reset_doc(guild_id):
            store.pop(guild_id, None)

        view = CardEditorView(
            bot, 1, "test.card", load_doc=load_doc, save_doc=save_doc,
            reset_doc=reset_doc, owner_id=1,
        )
        await view.prepare()
        assert view.using_default_base is True
        assert any("corpo atual" in b.get("body", "") for b in view.blocks)

        # Reset after a saved override returns to the same catalog seed.
        store[1] = {"v": 1, "blocks": [{"type": "text", "body": "custom"}]}
        view.loaded = False
        await view.prepare()
        await view._reset(FakeInteraction())  # arms
        await view._reset(FakeInteraction())  # confirms
        assert await view._default_blocks() is not None
        assert any("corpo atual" in b.get("body", "") for b in view.blocks)
        assert 1 not in store
    finally:
        import rosemary.core.cards as cards_module

        cards_module._CARDS.pop("test.card", None)


async def test_save_persists_and_records_history(tmp_path):
    view, store, bot = make_view(tmp_path)
    await view.prepare()
    view.blocks = [{"type": "text", "body": "conteúdo"}]
    await view._save(FakeInteraction())
    assert store[1]["blocks"][0]["body"] == "conteúdo"
    assert view.is_dirty() is False

    from rosemary.core.card_history import history_store

    versions = await history_store(bot).list(1, "test.card")
    assert len(versions) == 1
    assert versions[0]["actor"] == 1
    doc = await history_store(bot).get(1, "test.card", versions[0]["ver"])
    assert _no_ids(doc["blocks"]) == [{"type": "text", "body": "conteúdo"}]


async def test_discard_restores_saved_version_without_persisting(tmp_path):
    view, store, _bot = make_view(tmp_path)
    await view.prepare()
    view.blocks = [{"type": "text", "body": "salvo"}]
    await view._save(FakeInteraction())
    assert store[1]["blocks"][0]["body"] == "salvo"

    # Unsaved edits stay draft-only until Save is pressed.
    view.blocks[0]["body"] = "rascunho"
    assert view.is_dirty()
    interaction = FakeInteraction()
    await view._discard(interaction)  # arms the two-step discard
    assert store[1]["blocks"][0]["body"] == "salvo"
    await view._discard(FakeInteraction())  # confirms: reload last save
    assert view.blocks[0]["body"] == "salvo"
    assert view.is_dirty() is False
    assert store[1]["blocks"][0]["body"] == "salvo"


async def test_discard_without_saved_version_restores_seed(tmp_path):
    view, store, _bot = make_view(tmp_path)
    await view.prepare()
    # No default builder for test.card: the seed is the empty skeleton.
    assert view.using_default_base is False
    assert view.saved_exists is False
    view.blocks[0]["body"] = "rascunho"
    await view._discard(FakeInteraction())
    await view._discard(FakeInteraction())
    assert _no_ids(view.blocks) == [{"type": "text", "body": ""}]
    assert 1 not in store


async def test_history_restore_loads_as_draft(tmp_path):
    view, store, bot = make_view(tmp_path)
    await view.prepare()
    view.blocks = [{"type": "text", "body": "v1"}]
    await view._save(FakeInteraction())
    view.blocks = [{"type": "text", "body": "v2"}]
    await view._save(FakeInteraction())

    from rosemary.core.card_history import history_store

    versions = await history_store(bot).list(1, "test.card")
    oldest = versions[0]
    interaction = FakeInteraction(data={"values": [str(oldest["ver"])]})
    await view._pick_history(interaction)
    assert view.blocks[0]["body"] == "v1"
    assert view.is_dirty()  # draft only — Save is still required
    assert store[1]["blocks"][0]["body"] == "v2"  # nothing auto-persisted


async def test_nav_rows_never_exceed_five_buttons(tmp_path):
    """Regression: opening via /customize inside a container used to crash."""
    view, _store, _bot = make_view(
        tmp_path, exit_factory=lambda: CardEditorView.__new__(CardEditorView)
    )
    await view.prepare()
    view.path = [0]
    view.blocks = [{
        "type": "container",
        "color": None,
        "children": [{"type": "text", "body": "inner"}],
    }]
    view.selected = None
    await view.prepare()
    for row in row_ids(view):
        assert len(row) <= 5, row


async def test_breadcrumb_reflects_path(tmp_path):
    view, _store, _bot = make_view(tmp_path)
    await view.prepare()
    view.blocks = [{
        "type": "container",
        "color": None,
        "children": [{"type": "text", "body": "inner"}],
    }]
    view.path = [0]
    await view.prepare()
    rendered = all_texts(view)
    assert "cards.editor.breadcrumb" in rendered
    assert "container" in rendered  # localized type label appears in path


# -- selection ------------------------------------------------------------------


async def test_blocks_listed_in_select_and_selection_enables_actions(tmp_path):
    view, _store, _bot = make_view(tmp_path)
    await view.prepare()
    view.blocks = [
        {"type": "text", "body": "primeiro"},
        {"type": "divider", "spacing": "small"},
    ]
    await view.prepare()

    selects = [
        item
        for item in view.walk_children()
        if isinstance(item, discord.ui.Select) and item.custom_id == "card_select"
    ]
    assert [o.label for o in selects[0].options] == [
        "1. cards.editor.types.text",
        "2. cards.editor.types.divider",
    ]

    interaction = FakeInteraction(data={"values": ["0"]})
    await view._select_block(interaction)
    assert interaction.order == ["defer", "edit"]
    assert view.selected == 0

    enabled = []
    for row_obj in view.children:
        if not isinstance(row_obj, discord.ui.ActionRow):
            continue
        for item in row_obj.children:
            cid = getattr(item, "custom_id", "")
            if cid.startswith("card_act") and not item.disabled:
                enabled.append(cid)
    assert "card_act:edit" in enabled
    assert "card_act:down" in enabled
    assert "card_act:up" not in enabled  # index 0 can't move up


async def test_context_row_disabled_without_selection(tmp_path):
    view, _store, _bot = make_view(tmp_path)
    await view.prepare()
    disabled_flags = [
        item.disabled
        for row in view.children
        if isinstance(row, discord.ui.ActionRow)
        for item in row.children
        if getattr(item, "custom_id", "").startswith("card_act")
    ]
    assert disabled_flags and all(disabled_flags)


async def test_act_edit_text_opens_modal(tmp_path):
    view, _store, _bot = make_view(tmp_path)
    await view.prepare()
    view.blocks.append({"type": "text", "body": "atual"})
    view.selected = 1

    class ModalCapture:
        def __init__(self):
            self.modal_id = None

        async def send_modal(self, modal):
            self.modal_id = modal.custom_id

    interaction = FakeInteraction(custom_id="card_act:edit")
    interaction.response = ModalCapture()
    await view._act_on_selected(interaction)
    assert interaction.response.modal_id.startswith("card_text_modal")


async def test_act_up_down_delete(tmp_path):
    view, store, _bot = make_view(tmp_path)
    await view.prepare()
    view.blocks = [
        {"type": "text", "body": "a"},
        {"type": "text", "body": "b"},
        {"type": "text", "body": "c"},
    ]
    view.loaded = True
    view.selected = 2

    await view._act_on_selected(FakeInteraction(custom_id="card_act:up"))
    assert [b["body"] for b in view.blocks] == ["a", "c", "b"]
    assert view.selected == 1

    # Delete arms first, deletes on the second click; nothing autosaves.
    await view._act_on_selected(FakeInteraction(custom_id="card_act:delete"))
    assert [b["body"] for b in view.blocks] == ["a", "c", "b"]
    assert 1 not in store
    await view._act_on_selected(FakeInteraction(custom_id="card_act:delete"))
    assert [b["body"] for b in view.blocks] == ["a", "b"]
    assert view.selected is None
    assert 1 not in store
    await view._save(FakeInteraction())
    assert len(store[1]["blocks"]) == 2


async def test_act_enter_composite_then_uplevel(tmp_path):
    view, _store, _bot = make_view(tmp_path)
    view.blocks = [{
        "type": "container",
        "color": None,
        "children": [{"type": "text", "body": "inner"}],
    }]
    view.loaded = True
    view.selected = 0

    await view._act_on_selected(FakeInteraction(custom_id="card_act:enter"))
    assert view.path == [0] and view.selected is None

    await view._up_level(FakeInteraction())
    assert view.path == []


async def test_divider_style_screen(tmp_path):
    view, _store, _bot = make_view(tmp_path)
    view.blocks = [{"id": "d1", "type": "divider", "spacing": "small"}]
    view.loaded = True
    view.selected = 0
    await view._act_on_selected(FakeInteraction(custom_id="card_act:edit"))
    assert view._editing_divider == view.blocks[0].get("id")
    await view._set_divider_style(FakeInteraction(data={"values": ["large"]}))
    assert view.blocks[0]["spacing"] == "large"
    assert view.is_dirty()
    await view._set_divider_style(FakeInteraction(data={"values": ["hidden"]}))
    assert view.blocks[0].get("visible") is False


async def test_add_block_flow_touches_and_acks(tmp_path):
    view, store, _bot = make_view(tmp_path)
    await view.prepare()
    interaction = FakeInteraction(data={"values": ["text"]})
    await view._pick_add_type(interaction)
    assert len(view.blocks) == 2
    assert interaction.order == ["defer", "edit"]
    assert view.is_dirty()
    assert 1 not in store
    # Strict save refuses empty skeletons until they have content.
    await view._save(FakeInteraction())
    assert 1 not in store
    view.blocks[0]["body"] = "seed"
    view.blocks[1]["body"] = "hi"
    await view._save(FakeInteraction())
    assert store[1]["v"] == 1
    assert not view.is_dirty()


async def test_add_container_does_not_navigate(tmp_path):
    view, _store, _bot = make_view(tmp_path)
    await view.prepare()
    await view._pick_add_type(FakeInteraction(data={"values": ["container"]}))
    assert view.path == []
    assert view.selected == 1


async def test_color_select_sets_token(tmp_path):
    view, store, _bot = make_view(tmp_path)
    view.blocks = [{
        "type": "container",
        "color": None,
        "children": [{"type": "text", "body": "x"}],
    }]
    view.loaded = True
    view.path = [0]
    await view._set_container_color(FakeInteraction(data={"values": ["danger"]}))
    assert view.blocks[0]["color"] == "danger"
    await view._set_container_color(FakeInteraction(data={"values": [""]}))
    assert view.blocks[0]["color"] is None


async def test_reset_restores_default_and_clears_store(tmp_path):
    view, store, _bot = make_view(tmp_path)
    store[1] = {"v": 1, "blocks": [{"type": "divider", "spacing": "large"}]}
    view.blocks = [{"type": "divider", "spacing": "large"}]
    await view.prepare()
    await view._reset(FakeInteraction())
    assert _no_ids(view.blocks) == [{"type": "divider", "spacing": "large"}]
    await view._reset(FakeInteraction())
    assert _no_ids(view.blocks) == [{"type": "text", "body": ""}]
    assert 1 not in store


async def test_import_loads_draft_without_persisting(tmp_path):
    view, store, _bot = make_view(tmp_path)
    await view.prepare()

    class Attachment:
        async def read(self):
            return b'{"v":2,"key":"test.card","blocks":[{"type":"text","body":"imported"}]}'

    await view._import_submit(FakeInteraction(), [Attachment()])
    assert 1 not in store
    assert view.blocks == [{"type": "text", "body": "imported", "id": view.blocks[0]["id"]}]
    assert view.using_default_base is False
    assert view.is_dirty()

    await view._save(FakeInteraction())
    assert store[1]["blocks"][0]["body"] == "imported"
    assert not view.is_dirty()


async def test_reset_records_result_in_history(tmp_path):
    view, store, bot = make_view(tmp_path)
    store[1] = {"v": 1, "blocks": [{"type": "divider", "spacing": "large"}]}
    await view.prepare()
    await view._reset(FakeInteraction())
    await view._reset(FakeInteraction())

    from rosemary.core.card_history import history_store

    versions = await history_store(bot).list(1, "test.card")
    assert len(versions) == 1
    assert versions[0]["actor"] == 1
    history_doc = await history_store(bot).get(1, "test.card", versions[0]["ver"])
    assert _no_ids(history_doc["blocks"]) == [{"type": "text", "body": ""}]
    assert 1 not in store



# -- compare screen ---------------------------------------------------------------


async def test_test_invalid_draft_does_not_send(tmp_path):
    view, _store, _bot = make_view(tmp_path)
    await view.prepare()
    view.blocks = [{"type": "gallery", "urls": []}]
    interaction = FakeInteraction()
    await view._test(interaction)
    assert "send" not in interaction.order
    assert "cards.editor.invalid" in all_texts(view)


async def test_compare_button_hidden_without_builder(tmp_path):
    view, _store, _bot = make_view(tmp_path)
    await view.prepare()
    ids = {item.custom_id for item in view.walk_children() if hasattr(item, "custom_id")}
    assert "card_compare" not in ids


async def test_compare_screen_renders_default_and_custom(tmp_path, monkeypatch):
    import rosemary.core.cards as cards_module

    async def fake_builder(bot, guild_id):
        return {
            "v": 1,
            "blocks": [{"type": "text", "body": "PADRÃO {user}"}],
        }

    monkeypatch.setattr(cards_module, "_DEFAULT_BUILDERS", {"test.card": fake_builder})

    view, _store, bot = make_view(tmp_path)
    guild = type("G", (), {"name": "Servidor"})
    bot.get_guild = lambda gid: guild
    await view.prepare()  # no override: seeds from the default

    assert view.using_default_base is True
    assert view.blocks[0]["body"] == "PADRÃO {user}"
    rendered = all_texts(view)
    # Stored templates stay literal, but previews render sample values.
    assert "PADRÃO <@1>" in rendered
    assert "cards.editor.using_default" in rendered

    await view._open_compare(FakeInteraction())
    assert view.comparing is True
    compare_render = all_texts(view)
    # effective (default, sample vars applied) + working copy both visible
    assert "PADRÃO <@1>" in compare_render
    assert "cards.editor.compare_default" in compare_render
    assert "cards.editor.compare_custom" in compare_render

    await view._close_compare(FakeInteraction())
    assert view.comparing is False


# -- limits ------------------------------------------------------------------------


def _count_components(view) -> int:
    def walk(d: dict) -> int:
        n = 1
        for child in d.get("components") or []:
            n += walk(child)
        accessory = d.get("accessory")
        if isinstance(accessory, dict):
            n += walk(accessory)
        return n

    return sum(walk(item.to_component_dict()) for item in view.children)


@pytest.mark.parametrize("flash", [False, True])
async def test_editor_states_stay_under_discord_limit(tmp_path, flash):
    view, _store, _bot = make_view(
        tmp_path, exit_factory=lambda: CardEditorView.__new__(CardEditorView)
    )
    view.blocks = [
        {"type": "container", "color": "brand", "children": [{"type": "text", "body": "a"}]},
        {"type": "section",
         "accessory": {"type": "thumbnail", "url": "{user_avatar}"},
         "children": [{"type": "text", "body": "b"}]},
        {"type": "divider"},
        {"type": "gallery", "urls": ["https://a.b/x.png"]},
        {"type": "row", "buttons": [{"label": "L", "url": "https://a.b"}]},
    ]
    states = []
    for state in ("fresh", "selected", "sublevel", "adding", "compare"):
        view.loaded = True
        view.adding = view.comparing = False
        view.selected = None
        view.path.clear()
        if state == "selected":
            view.selected = 0
        elif state == "sublevel":
            view.path = [0]
        elif state == "adding":
            view.adding = True
        elif state == "compare":
            view.comparing = True
        if flash:
            view.flash = "cards.editor.saved"
        await view.prepare()
        total = _count_components(view)
        states.append((state, total))
        assert total <= 38, states


@pytest.mark.parametrize("flash", [False, True])
async def test_editor_states_with_mentions_stay_under_limit(tmp_path, flash):
    """The mention status line + select must not push states over the cap."""
    policies: dict[int, str] = {}

    async def load_doc(guild_id):
        return None

    async def save_doc(guild_id, doc):
        pass

    async def reset_doc(guild_id):
        pass

    async def load_mentions(guild_id):
        return policies.get(guild_id)

    async def save_mentions(guild_id, policy):
        policies[guild_id] = policy

    view = CardEditorView(
        FakeBot(tmp_path),
        1,
        "test.card",
        load_doc=load_doc,
        save_doc=save_doc,
        reset_doc=reset_doc,
        owner_id=1,
        load_mentions=load_mentions,
        save_mentions=save_mentions,
    )
    view.blocks = [
        {"type": "container", "color": "brand", "children": [{"type": "text", "body": "a"}]},
        {"type": "section",
         "accessory": {"type": "thumbnail", "url": "{user_avatar}"},
         "children": [{"type": "text", "body": "b"}]},
        {"type": "divider"},
        {"type": "gallery", "urls": ["https://a.b/x.png"]},
        {"type": "row", "buttons": [{"label": "L", "url": "https://a.b"}]},
    ]
    states = []
    for state in ("fresh", "selected", "sublevel", "adding", "compare"):
        view.loaded = True
        view.adding = view.comparing = False
        view.selected = None
        view.path.clear()
        if state == "selected":
            view.selected = 0
        elif state == "sublevel":
            view.path = [0]
        elif state == "adding":
            view.adding = True
        elif state == "compare":
            view.comparing = True
        if flash:
            view.flash = "cards.editor.saved"
        await view.prepare()
        total = _count_components(view)
        states.append((state, total))
        assert total <= 38, states


async def test_mentions_status_line_shows_current_policy(tmp_path):
    """The mentions row renders the effective policy, never a placeholder."""
    policies = {1: "role"}

    async def load_doc(guild_id):
        return None

    async def save_doc(guild_id, doc):
        pass

    async def reset_doc(guild_id):
        pass

    async def load_mentions(guild_id):
        return policies.get(guild_id)

    async def save_mentions(guild_id, policy):
        policies[guild_id] = policy

    view = CardEditorView(
        FakeBot(tmp_path),
        1,
        "test.card",
        load_doc=load_doc,
        save_doc=save_doc,
        reset_doc=reset_doc,
        owner_id=1,
        load_mentions=load_mentions,
        save_mentions=save_mentions,
    )
    await view.prepare()
    rendered = all_texts(view)
    assert "cards.editor.mentions.status" in rendered
    assert "cards.editor.mentions.on" in rendered  # policy name is rendered
