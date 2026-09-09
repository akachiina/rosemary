"""Regression tests for the /personalizar + theme audit fixes.

Covers: structural overrides honored as text, echo-safe maybe_view defaults,
persist validation, stale modal paths, mention repair, picker paging and the
theme loader validation.
"""

from __future__ import annotations

from types import SimpleNamespace

import rosemary.core.cards as cards_module
from rosemary.core.cards import (
    card_store,
    maybe_flat_text,
    maybe_text,
    maybe_view,
    preview_variables,
    text_or,
)
from rosemary.core.storage import GuildStorage
from rosemary.ui.card_editor import CardEditorView
from rosemary.ui.customize_menu import CustomizeMenuView
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


class FakeBot:
    theme = load_theme()
    translator = FakeTranslator()

    def __init__(self, tmp_path):
        self.storage = GuildStorage(tmp_path)

    def get_guild(self, guild_id):
        return None


class FakeResponse:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._order.append(("defer", ephemeral))
        self._done = True


class FakeInteraction:
    def __init__(self, custom_id="", data=None):
        self.order: list[str] = []
        self.response = FakeResponse(self.order)
        self.data = data or {}
        self.custom_id = custom_id
        self.user = SimpleNamespace(id=1)
        self.message = None
        self.edit_kwargs: dict = {}

    async def edit(self, **kwargs) -> None:
        self.edit_kwargs = kwargs
        self.order.append("edit")


def make_editor(tmp_path, key="test.card"):
    async def load_doc(guild_id):
        return await card_store(bot).get_document(guild_id, key)

    async def save_doc(guild_id, doc):
        await card_store(bot).save_document(guild_id, key, doc)

    async def reset_doc(guild_id):
        await card_store(bot).reset(guild_id, key)

    bot = FakeBot(tmp_path)
    return (
        CardEditorView(
            bot,
            1,
            key,
            load_doc=load_doc,
            save_doc=save_doc,
            reset_doc=reset_doc,
            owner_id=1,
        ),
        bot,
    )


async def test_maybe_view_without_variables_keeps_user_literal(tmp_path):
    """No-vars rendering must not resolve {user} to the 👤 theme emoji."""
    bot = FakeBot(tmp_path)
    await card_store(bot).save_document(
        1, "k", {"v": 1, "blocks": [{"type": "text", "body": "hi {user}"}]}
    )
    view = await maybe_view(bot, 1, "k")
    assert view is not None
    texts = [c.content for c in view.children]
    assert texts == ["hi {user}"]


async def test_text_or_flattens_structural_override(tmp_path):
    """A rich customization is flattened, never silently ignored."""
    bot = FakeBot(tmp_path)
    await card_store(bot).save_document(
        1,
        "k",
        {
            "v": 1,
            "blocks": [
                {
                    "type": "container",
                    "color": "brand",
                    "children": [{"type": "text", "body": "CUSTOM {server}"}],
                }
            ],
        },
    )
    assert await text_or(bot, 1, "k", "DEFAULT", server="S") == "CUSTOM S"
    # Text-only behavior is unchanged (plain docs still resolve).
    await card_store(bot).save_document(
        1, "p", {"v": 1, "blocks": [{"type": "text", "body": "PLAIN"}]}
    )
    assert await maybe_text(bot, 1, "p") == "PLAIN"
    assert await maybe_flat_text(bot, 1, "p") == "PLAIN"


async def test_persist_rejects_structural_garbage(tmp_path):
    """Invalid edits are rejected with the last save intact, draft kept."""
    view, bot = make_editor(tmp_path)
    await view.prepare()
    view.blocks = [{"type": "gallery", "urls": []}]
    assert await view._persist() is False
    assert await card_store(bot).get_document(1, "test.card") is None
    assert view.blocks == [{"type": "gallery", "urls": []}]
    assert view.flash is not None


async def test_persist_accepts_empty_text_skeleton(tmp_path):
    """Draft-tolerant save: a just-added block persists for later editing."""
    view, _bot = make_editor(tmp_path)
    await view.prepare()
    view.blocks = [{"type": "text", "body": ""}]
    assert await view._persist() is True


async def test_stale_modal_path_does_not_crash(tmp_path):
    """Submitting a modal after delete/reset ACKs instead of IndexError."""
    view, _bot = make_editor(tmp_path)
    await view.prepare()
    view.blocks = [{"type": "text", "body": "a"}]
    await view._persist()
    view.blocks = []
    interaction = FakeInteraction(custom_id="card_text_modal:root:0")
    await view._text_submit(interaction, "new")
    assert ("defer", True) in interaction.order


async def test_row_submit_splits_on_last_pipe(tmp_path):
    """Labels may contain '|' — only the final segment is the URL."""
    from rosemary.ui.card_editor import _skeleton

    view, _bot = make_editor(tmp_path)
    await view.prepare()
    view.blocks = [_skeleton("row")]
    block_id = view.blocks[0]["id"]
    interaction = FakeInteraction(custom_id=f"card_row_modal:{block_id}")
    await view._row_submit(interaction, "A | B | https://x.y")
    [button] = view.blocks[0]["buttons"]
    assert (button["label"], button["url"]) == ("A | B", "https://x.y")


async def test_unknown_mention_policy_is_repaired(tmp_path):
    """Corrupted stored policies fall back to the spec default."""
    from rosemary.core import card_specs  # noqa: F401  (fills the registry)
    from rosemary.core.mentions import effective_policy, mention_store

    bot = FakeBot(tmp_path)
    await mention_store(bot)._storage.set(1, "bump.reminder", "bogus")
    assert await effective_policy(bot, 1, "bump.reminder") == "role"
    assert await mention_store(bot).get_policy(1, "bump.reminder") is None


async def test_picker_pages_overflow_category(tmp_path):
    """Categories with >25 cards page instead of truncating silently."""
    from rosemary.core.cards import CardSpec, register_cards

    specs = [CardSpec(key=f"zz.page.{i}", category="zz-page", rich=True) for i in range(27)]
    register_cards(*specs)
    try:
        bot = FakeBot(tmp_path)
        view = CustomizeMenuView(bot, 1, owner_id=1, category="zz-page")
        await view.prepare()
        assert view.page == 0
        await view._next_page(FakeInteraction())
        assert view.page == 1
        await view._next_page(FakeInteraction())
        assert view.page == 1  # clamped, never an empty page
        await view._prev_page(FakeInteraction())
        assert view.page == 0
    finally:
        for spec in specs:
            cards_module._CARDS.pop(spec.key, None)


async def test_preview_variables_cover_placeholders():
    """Preview samples leave no common placeholder unresolved."""
    mapping = preview_variables(server="S")
    for name in (
        "mention",
        "member",
        "moderator",
        "target",
        "inviter",
        "winner_mention",
        "reason",
        "duration",
        "channel",
        "role_name",
    ):
        assert name in mapping


async def test_invalid_theme_color_warns(tmp_path, caplog):
    """Boot validation warns on bad hex instead of failing later in sends."""
    import yaml

    from rosemary.ui import theme as theme_module

    doc = {
        "colors": {"brand": "not-a-hex", "info": "#123456"},
        "emojis": {},
        "markdown": {},
        "styles": {},
        "bump": {},
        "medals": {},
    }
    path = tmp_path / "theme.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with caplog.at_level("WARNING", logger="rosemary.ui.theme"):
        theme_module.load_theme(path)
    assert any("not-a-hex" in record.message for record in caplog.records)


async def test_explicit_save_and_undo_redo_flow(tmp_path):
    """Draft model: touch, undo, save, discard."""
    from rosemary.ui.card_editor import _skeleton

    view, bot = make_editor(tmp_path)
    await view.prepare()
    assert not view.is_dirty()
    view._touch()
    view.blocks.append(_skeleton("divider"))
    assert view.is_dirty()
    await view._undo_action(FakeInteraction())
    assert not view.is_dirty()
    assert len(view.blocks) == 1


async def test_delete_arms_before_removing(tmp_path):
    view, _bot = make_editor(tmp_path)
    await view.prepare()
    view.blocks[0]["body"] = "keep"
    interaction = FakeInteraction(custom_id="card_act:delete")
    view.selected = 0
    await view._act_on_selected(interaction)
    assert len(view.blocks) == 1
    await view._act_on_selected(FakeInteraction(custom_id="card_act:delete"))
    assert view.blocks == []


async def test_duplicate_reids_block(tmp_path):
    view, _bot = make_editor(tmp_path)
    await view.prepare()
    view.blocks[0]["body"] = "x"
    view.selected = 0
    await view._act_on_selected(FakeInteraction(custom_id="card_act:duplicate"))
    assert len(view.blocks) == 2
    assert view.blocks[0]["id"] != view.blocks[1]["id"]
    assert view.blocks[1]["body"] == "x"


async def test_modal_by_id_survives_reorder(tmp_path):
    """A modal opened before a move still edits the right block."""
    view, _bot = make_editor(tmp_path)
    await view.prepare()
    view.blocks.append({"id": "b2", "type": "text", "body": "second"})
    await view._text_submit(
        FakeInteraction(custom_id="card_text_modal:b2"), "edited"
    )
    assert view.blocks[1]["body"] == "edited"


async def test_action_button_roundtrip_validation(tmp_path):
    """Closed action registry: known actions pass, others fail."""
    from rosemary.core.cards import validate_document

    theme = load_theme()
    good = {
        "v": 1,
        "blocks": [
            {
                "type": "row",
                "buttons": [
                    {"label": "Open", "action": "open_ticket", "ticket_type": "report"}
                ],
            }
        ],
    }
    assert validate_document(good, theme=theme) == []
    bad = {
        "v": 1,
        "blocks": [
            {
                "type": "row",
                "buttons": [{"label": "X", "action": "explode", "url": "https://x.y"}],
            }
        ],
    }
    codes = {issue.code for issue in validate_document(bad, theme=theme)}
    assert {"button_unknown_action", "button_url_and_action"} <= codes


async def test_action_custom_id_parse():
    from rosemary.core.card_actions import custom_id_for, parse_custom_id

    cid = custom_id_for("bump.reminder", "b_12345678")
    assert len(cid) <= 100
    assert parse_custom_id(cid) == ("cardact", "bump.reminder", "b_12345678")
    assert parse_custom_id("tickets_open") is None


async def test_templates_apply_as_undoable_draft(tmp_path):
    from rosemary.core.card_templates import get_template, list_templates

    assert len(list_templates()) >= 3
    template = get_template("banner")
    assert template is not None
    view, _bot = make_editor(tmp_path)
    await view.prepare()
    before = len(view.blocks)
    view._touch()
    import copy

    view.blocks = copy.deepcopy(template.blocks)
    assert len(view.blocks) != before or view.is_dirty()
    await view._undo_action(FakeInteraction())
    assert not view.is_dirty()


async def test_history_append_and_cap(tmp_path):
    from rosemary.core.card_history import MAX_VERSIONS, CardHistory

    store = CardHistory(tmp_path)
    for i in range(MAX_VERSIONS + 5):
        await store.append(1, "k", {"v": 2, "blocks": [{"n": i}]}, None)
    versions = await store.list(1, "k")
    assert len(versions) == MAX_VERSIONS
    doc = await store.get(1, "k", versions[-1]["ver"])
    assert doc["blocks"] == [{"n": MAX_VERSIONS + 4}]


async def test_service_export_import_roundtrip(tmp_path):
    from rosemary.core.card_service import export_payload, import_payload

    class FakeBot:
        theme = load_theme()

        def __init__(self):
            from rosemary.core.storage import GuildStorage

            self.storage = GuildStorage(tmp_path)

    bot = FakeBot()
    doc = {"v": 2, "blocks": [{"type": "text", "body": "hi {user}"}]}
    filename, data = export_payload("bump.reminder", doc)
    assert filename == "bump-reminder.json"
    import json

    ok, _reason = await import_payload(bot, 1, "bump.reminder", json.loads(data))
    assert ok is True
    ok, _reason = await import_payload(bot, 1, "bump.reminder", {"nope": True})
    assert ok is False
