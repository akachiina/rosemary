"""The Card IR: one model, three forms, the same output as today.

The pipeline (:func:`rosemary.core.card_service.render_card_message`)
reads the document into the semantic model, compiles it (every placeholder
resolved **once**) and draws it with the serializer of its form.

These tests pin the two promises of the migration:

* the IR reads a document without losing anything;
* every card of the package goes through the model (the golden fixtures are
  the judge of the output).
"""

from __future__ import annotations

import json

from rosemary.core.cards import all_cards
from rosemary.core.design.compile import compile_card
from rosemary.core.design.ir import Card, Field, Text, blocks_to_document
from rosemary.core.design.serialize import card_to_embed, card_to_text, card_to_v2
from rosemary.core.documents.legacy import document_to_card


def test_a_v2_document_survives_the_ir_round_trip():
    doc = {
        "v": 1,
        "blocks": [
            {
                "type": "container",
                "color": "brand",
                "id": "b_1",
                "children": [
                    {
                        "type": "section",
                        "id": "b_2",
                        "accessory": {"type": "thumbnail", "url": "{avatar}"},
                        "children": [{"type": "text", "body": "# Title", "id": "b_3"}],
                    },
                    {"type": "divider", "spacing": "large", "visible": False, "id": "b_4"},
                    {
                        "type": "row",
                        "id": "b_5",
                        "buttons": [
                            {"label": "Go", "url": "{link}", "id": "b_6"},
                            {
                                "label": "Act",
                                "action": "dismiss",
                                "id": "b_7",
                                "style": "danger",
                            },
                        ],
                    },
                    {"type": "gallery", "urls": ["{image}"], "id": "b_8"},
                ],
            }
        ],
    }
    card = document_to_card(doc, key="demo")
    assert card.native == "blocks"
    assert blocks_to_document(card.blocks) == doc


def test_an_embed_document_survives_the_ir_round_trip():
    doc = {
        "kind": "embed",
        "embed": {
            "title": "Title",
            "description": "Body",
            "color": "#ff0000",
            "author": {"name": "Author", "icon_url": "https://example.com/i.png"},
            "footer": {"text": "Footer", "icon_url": "https://example.com/f.png"},
            "image": {"url": "https://example.com/i.png"},
            "thumbnail": {"url": "https://example.com/t.png"},
            "timestamp": 1767225600,
            "fields": [{"name": "A", "value": "B", "inline": True}],
        },
        "buttons": [{"label": "Go", "url": "https://example.com", "id": "b_1"}],
    }
    card = document_to_card(doc, key="demo")
    assert card.native == "slots"
    assert card_to_embed(card) == doc


def test_a_text_document_survives_the_ir_round_trip():
    card = document_to_card({"kind": "text", "content": "Hello"}, key="demo")
    assert card.native == "content"
    assert card_to_text(card) == "Hello"


def test_the_text_serializer_writes_one_thing_per_line():
    """The title is bold, a field is a bullet and nothing joins with " · "."""
    card = Card(
        form="text",
        title="Title",
        paragraphs=("Body",),
        fields=(Field(name="Reason", value="spam"),),
    )
    assert card_to_text(card) == "**Title**\nBody\n• **Reason:** spam"


def test_the_text_serializer_bolds_the_first_line_as_the_title():
    """A multi-line copy with no title slot: its first line is the title."""
    card = Card(form="text", paragraphs=("First line\nSecond line",))
    assert card_to_text(card) == "**First line**\nSecond line"


def test_the_legacy_text_converter_honours_fields_and_drops_the_footer():
    from rosemary.core.forms import to_text_content

    doc = {
        "blocks": [
            {
                "type": "text",
                "body": "# Title\n\nBody\n**Label:** value",
            },
            {"type": "text", "body": "-# 01/01/2026"},
        ]
    }
    assert to_text_content(doc) == "**Title**\nBody\n• **Label:** value"


def test_the_compiler_resolves_every_placeholder_once():
    card = Card(form="text", content="{message}")
    compiled = compile_card(card, {"message": "{other}", "other": "resolved"})
    # One pass: the value's own braces stay literal (the legacy pipeline
    # substituted them a second time).
    assert compiled.card.content == "{other}"


def test_the_compiler_collects_the_mention_fields():
    compiled = compile_card(
        Card(form="card", blocks=(Text(body="Hi {@user}"),)), {"user": "<@123>"}
    )
    assert compiled.users == (123,)
    assert compiled.card.blocks[0].body == "Hi <@123>"

    compiled = compile_card(
        Card(form="card", blocks=(Text(body="Hi {@role}"),)), {"role": "<@&456>"}
    )
    assert compiled.roles == (456,)
    assert compiled.users == ()


def test_the_v2_serializer_owns_the_markers():
    doc = card_to_v2(Card(form="card", title="Title", paragraphs=("Body",), footer="Footer"))
    bodies = [
        block["body"] for block in doc["blocks"] if block.get("type") == "text"
    ]
    assert "# Title" in bodies
    assert "-# Footer" in bodies


def test_the_embed_serializer_writes_no_v2_marker():
    doc = card_to_embed(
        Card(form="embed", title="Title", paragraphs=("Body",), footer="Footer")
    )
    blob = json.dumps(doc, ensure_ascii=False)
    assert "-#" not in blob
    assert "# " not in blob


def test_the_embed_serializer_never_repeats_the_image_as_a_thumbnail():
    """The thumbnail is identity, the image is content: never the same art."""
    card = Card(
        form="embed",
        title="Title",
        thumbnail="https://example.com/same.png",
        image="https://example.com/same.png",
    )
    embed = card_to_embed(card)["embed"]
    assert embed["image"]["url"] == "https://example.com/same.png"
    assert "thumbnail" not in embed


def test_the_body_is_a_sequence_of_paragraphs():
    """The body keeps its paragraphs: each form joins them its own way."""
    card = Card(form="embed", title="Title", paragraphs=("First", "Second"))
    assert card_to_embed(card)["embed"]["description"] == "First\nSecond"
    bodies = [
        block["body"]
        for block in card_to_v2(Card(form="card", paragraphs=("First", "Second")))["blocks"]
        if block.get("type") == "text"
    ]
    assert bodies == ["First", "Second"]


async def test_every_card_goes_through_the_ir_pipeline():
    """Every card of the package is a document: the IR is the only path.

    The themed documents (the raw V2 JSON a guild writes) are read through
    :func:`rosemary.core.documents.legacy.document_to_card`, so the legacy
    shape never bypasses the model.
    """
    from rosemary.core.theme_package import default_package

    package = default_package()
    assert {spec.key for spec in all_cards()} == set(package.documents)


async def _themed_bot(theme_yaml: str):
    """A bot whose active theme carries one themed card."""
    import tempfile
    from pathlib import Path

    from golden_cards import GoldenBot

    from rosemary.core.themes import preload_themes

    bot = GoldenBot(Path(tempfile.mkdtemp()))
    themes = bot._theme_store.themes_dir
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "t.yaml").write_text(theme_yaml, encoding="utf-8")
    bot.guilds = [type("G", (), {"id": 1})()]
    await bot._theme_store.set_active(1, "t")
    await preload_themes(bot)
    return bot


async def test_a_themed_document_still_renders_through_the_ir():
    """A guild theme writes raw V2 JSON: the IR reads it and draws it.

    A *structural* themed document (a container) keeps the Components V2
    shape: the theme asked for a rich layout.
    """
    from rosemary.core.card_service import render_card_message

    bot = await _themed_bot(
        "name: t\n"
        "cards:\n"
        "  events.welcome:\n"
        "    - type: 17\n"
        "      accent_color: \"#ff00ff\"\n"
        "      components:\n"
        "        - type: 10\n"
        '          content: "Welcome {user}"\n'
    )
    payload, _allowed = await render_card_message(
        bot, 1, "events.welcome", {"user": "<@7>"}
    )
    assert payload is not None and payload.view is not None
    texts = [
        item.content
        for item in payload.view.walk_children()
        if hasattr(item, "content")
    ]
    assert any("Welcome <@7>" in text for text in texts), texts


async def test_a_themed_copy_only_document_keeps_the_card_surface():
    """A themed document made only of text carries no layout: the card
    keeps its own surface (the embed)."""
    from rosemary.core.card_service import render_card_message

    bot = await _themed_bot(
        "name: t\n"
        "cards:\n"
        "  events.welcome:\n"
        "    - type: 10\n"
        '      content: "Welcome {user}"\n'
    )
    payload, _allowed = await render_card_message(
        bot, 1, "events.welcome", {"user": "<@7>"}
    )
    assert payload is not None and payload.view is None
    assert payload.embed is not None
    assert payload.embed.description == "Welcome <@7>"
