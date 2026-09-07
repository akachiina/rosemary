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
    """Invalid edits are rejected with the last save intact."""
    view, bot = make_editor(tmp_path)
    await view.prepare()
    view.blocks = [{"type": "gallery", "urls": []}]
    assert await view._persist() is False
    assert await card_store(bot).get_document(1, "test.card") is None
    assert view.blocks != [{"type": "gallery", "urls": []}]


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
    view, _bot = make_editor(tmp_path)
    await view.prepare()
    view.blocks = [{"type": "row", "buttons": []}]
    interaction = FakeInteraction(custom_id="card_row_modal:root:0")
    await view._row_submit(interaction, "A | B | https://x.y")
    assert view.blocks[0]["buttons"] == [{"label": "A | B", "url": "https://x.y"}]


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
