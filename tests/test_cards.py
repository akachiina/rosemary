"""Card document schema, validation and rendering (core/cards.py)."""

from __future__ import annotations

import pytest

from rosemary.core.cards import (
    MAX_COMPONENTS,
    MAX_TEXT_CHARS,
    CardsError,
    CardStore,
    build_items,
    safe_format,
    validate_document,
)
from rosemary.core.storage import GuildStorage
from rosemary.ui.containers import Container, Section, TextDisplay
from rosemary.ui.theme import load_theme


@pytest.fixture
def theme():
    return load_theme()


def _doc(*blocks):
    return {"v": 1, "blocks": list(blocks)}


# -- placeholders ------------------------------------------------------------


def test_safe_format_replaces_known_and_keeps_unknown():
    out = safe_format("Olá {user}, você é #{count}! {unknown_key} {tada}", {
        "user": "<@42>",
        "count": 7,
        "tada": "🎉",
    })
    assert out == "Olá <@42>, você é #7! {unknown_key} 🎉"


def test_safe_format_never_raises_on_braces():
    assert safe_format("{{{weird}}} {x}", {}) == "{{{weird}}} {x}"


# -- validation --------------------------------------------------------------


def test_single_text_document_is_valid(theme):
    assert validate_document(_doc({"type": "text", "body": "hi {user}"}), theme=theme) == []


def test_draft_mode_tolerates_empty_skeleton(theme):
    doc = _doc({"type": "text", "body": ""})
    assert validate_document(doc, theme=theme, draft=True) == []
    assert build_items(theme, doc, draft=True) == []
    assert validate_document(doc, theme=theme) != []


def test_cards_error_carries_structured_issues(theme):
    from rosemary.core.cards import CardIssue

    try:
        build_items(theme, _doc({"type": "row", "buttons": []}), {})
    except CardsError as exc:
        assert CardIssue("row_count", (("min", 1), ("max", 5))) in exc.issues
    else:
        raise AssertionError("CardsError not raised")


def test_full_document_with_every_block_type_is_valid(theme):
    doc = _doc(
        {
            "type": "container",
            "color": "brand",
            "children": [
                {
                    "type": "section",
                    "accessory": {"type": "thumbnail", "url": "https://a.b/x.png"},
                    "children": [
                        {"type": "text", "body": "# Title"},
                        {"type": "text", "body": "Body"},
                    ],
                },
                {"type": "divider"},
                {"type": "gallery", "urls": ["https://a.b/1.png", "https://a.b/2.png"]},
                {"type": "row", "buttons": [{"label": "Site", "url": "https://a.b"}]},
            ],
        },
        {"type": "text", "body": "footer"},
    )
    assert validate_document(doc, theme=theme) == []


def test_section_needs_one_to_three_text_children(theme):
    empty = validate_document(
        _doc({
            "type": "section",
            "accessory": {"type": "thumbnail", "url": "https://a.b/x.png"},
            "children": [],
        }),
        theme=theme,
    )
    four = validate_document(
        _doc({
            "type": "section",
            "accessory": {"type": "thumbnail", "url": "https://a.b/x.png"},
            "children": [{"type": "text", "body": str(i)} for i in range(4)],
        }),
        theme=theme,
    )
    mixed = validate_document(
        _doc({
            "type": "section",
            "accessory": {"type": "thumbnail", "url": "https://a.b/x.png"},
            "children": [{"type": "divider"}],
        }),
        theme=theme,
    )
    codes = [issue.code for issue in empty]
    assert "section_children_count" in codes
    assert "section_children_count" in [issue.code for issue in four]
    assert "section_children_text" in [issue.code for issue in mixed]


def test_section_requires_accessory(theme):
    errors = validate_document(
        _doc({"type": "section", "children": [{"type": "text", "body": "hi"}]}),
        theme=theme,
    )
    assert any(issue.code == "section_accessory_missing" for issue in errors)


def test_containers_cannot_nest(theme):
    errors = validate_document(
        _doc({
            "type": "container",
            "color": "brand",
            "children": [{"type": "container", "children": [{"type": "text", "body": "x"}]}],
        }),
        theme=theme,
    )
    assert any(issue.code == "container_nested" for issue in errors)


def test_row_buttons_require_label_and_url(theme):
    errors = validate_document(
        _doc({"type": "row", "buttons": [{"label": "", "url": "ftp://x"}, {"url": ""}]}),
        theme=theme,
    )
    assert sum(1 for i in errors if i.code == "button_label") == 2
    assert sum(1 for i in errors if i.code == "bad_url") == 2


def test_gallery_bounds(theme):
    too_many = validate_document(
        _doc({"type": "gallery", "urls": [f"https://a.b/{i}" for i in range(11)]}),
        theme=theme,
    )
    assert any(issue.code == "gallery_count" for issue in too_many)


def test_component_count_limit(theme):
    doc = _doc(*[{"type": "text", "body": "x"} for _ in range(MAX_COMPONENTS + 1)])
    errors = validate_document(doc, theme=theme)
    assert any(issue.code == "too_many_components" for issue in errors)


def test_text_length_uses_resolved_variables(theme):
    doc = _doc({"type": "text", "body": "{big}"})
    assert validate_document(doc, theme=theme, variables={"big": "ok"}) == []
    errors = validate_document(doc, theme=theme, variables={"big": "x" * (MAX_TEXT_CHARS + 1)})
    assert any(issue.code == "text_too_long" for issue in errors)


def test_unknown_theme_color_is_flagged(theme):
    errors = validate_document(
        _doc({"type": "container", "color": "neon-pink", "children": [
            {"type": "text", "body": "x"}
        ]}),
        theme=theme,
    )
    color_issues = [i for i in errors if i.code == "color_unknown"]
    assert color_issues and dict(color_issues[0].params)["color"] == "neon-pink"


def test_malformed_documents_report_errors(theme):
    assert [i.code for i in validate_document({}, theme=theme)] == ["document_shape"]
    assert [i.code for i in validate_document({"v": 1}, theme=theme)] == ["document_shape"]
    assert [i.code for i in validate_document(_doc({"type": "explode"}), theme=theme)] == [
        "unknown_block"
    ]
    assert [i.code for i in validate_document(_doc("nope"), theme=theme)] == ["unknown_block"]


# -- rendering ---------------------------------------------------------------


def test_build_items_renders_container_tree(theme):
    doc = _doc({
        "type": "container",
        "color": "success",
        "children": [
            {
                "type": "section",
                "accessory": {"type": "button", "label": "Ir", "url": "https://a.b"},
                "children": [{"type": "text", "body": "# {title}"}],
            },
            {"type": "divider"},
        ],
    })
    items = build_items(theme, doc, {"title": "Bem-vindo"})
    assert len(items) == 1
    container = items[0]
    assert isinstance(container, Container)
    section, separator = container.items[0], container.items[1]
    assert isinstance(section, Section)
    assert isinstance(separator, discord_separator_type())
    first_text = section.items[0]
    assert isinstance(first_text, TextDisplay)
    assert "# Bem-vindo" in first_text.content


def discord_separator_type():
    from rosemary.ui.containers import Separator

    return Separator


def test_build_items_resolves_placeholders_and_emojis(theme):
    doc = _doc({"type": "text", "body": "Oi {user} {success}"})
    items = build_items(theme, doc, {"user": "<@7>"})
    content = items[0].content
    assert "<@7>" in content
    assert "✅" in content  # theme emoji token injected by name
    assert "{" not in content.replace("{unknown", "")


def test_build_items_keeps_unknown_placeholders_visible(theme):
    doc = _doc({"type": "text", "body": "Oi {typoo}"})
    (item,) = build_items(theme, doc, {})
    assert "{typoo}" in item.content


def test_build_items_raises_cards_error_on_invalid(theme):
    with pytest.raises(CardsError):
        build_items(theme, _doc({"type": "row", "buttons": []}), {})


# -- persistence -------------------------------------------------------------


async def test_card_store_roundtrip_and_reset(tmp_path):
    store = CardStore(tmp_path)
    doc = _doc({"type": "text", "body": "custom"})
    assert await store.get_document(1, "events.welcome") is None
    await store.save_document(1, "events.welcome", doc)
    assert await store.get_document(1, "events.welcome") == doc
    assert await store.get_document(2, "events.welcome") is None
    assert await store.customized_keys(1) == {"events.welcome"}
    await store.reset(1, "events.welcome")
    assert await store.get_document(1, "events.welcome") is None


async def test_card_store_lives_in_its_own_file(tmp_path):
    CardStore(tmp_path)
    settings_storage = GuildStorage(tmp_path)
    assert (tmp_path / "1" / "settings.json").exists() is False
    assert await settings_storage.get(1) == {"language": "en-US"}
