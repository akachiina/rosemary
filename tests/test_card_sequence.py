"""A card ships as one message, or as the sequence its document declares.

The topology of a message is data: a theme says how many messages a card has
and what each one carries, and the engine sends them in order. The default
package keeps one message per card; a theme may split one into two (the event
line in one, the subject in another) without touching Python.

This file pins the sequence engine:

* a document with ``messages`` renders every message, in order;
* the card's contract reaches every message (a missing name stays literal);
* a message whose copy is missing disappears instead of shipping empty;
* a theme delta customizes the first message, or replaces the topology whole;
* a message never nests another sequence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.core.card_resolution import _merge_delta
from rosemary.core.card_service import render_card_message, render_card_messages
from rosemary.core.documents.merge import DocumentError
from rosemary.core.documents.semantic import build_cards
from rosemary.core.documents.templates import Resolver
from rosemary.core.i18n import Translator
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore, preload_themes
from rosemary.ui.theme import load_theme


def _make_bot(tmp_path, themes_yaml: str | None = None):
    """A bot with the real catalogs, the real theme store and one guild theme."""

    class Bot:
        pass

    bot = Bot()
    bot.storage = GuildStorage(tmp_path)
    bot.theme = load_theme()
    bot.translator = Translator(
        Path("language"), default_placeholders=bot.theme.emojis
    )
    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    (tmp_path / "themes").mkdir(exist_ok=True)
    if themes_yaml is not None:
        (tmp_path / "themes" / "t.yaml").write_text(themes_yaml, encoding="utf-8")
        bot.guilds = [type("G", (), {"id": 1})()]
    return bot


async def _activate(bot) -> None:
    await bot._theme_store.set_active(1, "t")
    await preload_themes(bot)


def _two_text_messages(first: str, second: str, variables: str = "ms") -> str:
    """A theme that turns ``utility.ping`` into two plain-text messages."""
    return (
        "name: t\n"
        "cards:\n"
        "  utility.ping:\n"
        f"    variables: [{variables}]\n"
        "    messages:\n"
        "    - form: text\n"
        f"      content_line: '{first}'\n"
        "    - form: text\n"
        f"      content_line: '{second}'\n"
    )


async def test_a_theme_splits_a_card_into_two_messages(tmp_path):
    """The document decides the topology: two messages, sent in order."""
    bot = _make_bot(tmp_path, _two_text_messages("First", "Second"))
    await _activate(bot)
    messages = await render_card_messages(bot, 1, "utility.ping", {"ms": 42})
    assert [message.payload.content for message in messages] == ["First", "Second"]
    # The single-message call sites keep reading the first message.
    payload, _allowed = await render_card_message(bot, 1, "utility.ping", {"ms": 42})
    assert payload is not None and payload.content == "First"


async def test_every_message_keeps_the_contract_of_the_card(tmp_path):
    """The contract lives outside ``messages``: every message inherits it."""
    bot = _make_bot(
        tmp_path,
        _two_text_messages("Hello {user}", "Again {user}", variables="ms, user"),
    )
    await _activate(bot)
    messages = await render_card_messages(
        bot, 1, "utility.ping", {"ms": 42, "user": "<@5>"}
    )
    assert [message.payload.content for message in messages] == [
        "Hello <@5>",
        "Again <@5>",
    ]
    # A name the send site did not provide stays literal: it never becomes a
    # theme emoji with the same name (the theme has a ``user`` icon).
    bare = await render_card_messages(bot, 1, "utility.ping", {"ms": 42})
    assert [message.payload.content for message in bare] == [
        "Hello {user}",
        "Again {user}",
    ]


async def test_a_message_without_copy_disappears(tmp_path):
    """A message the catalog has no copy for is not sent at all."""
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "cards:\n"
        "  utility.ping:\n"
        "    messages:\n"
        "    - form: text\n"
        "      content_line: 'First'\n"
        "    - form: text\n"
        "      description: '{missing.copy.key}'\n",
    )
    await _activate(bot)
    messages = await render_card_messages(bot, 1, "utility.ping", {"ms": 1})
    assert [message.payload.content for message in messages] == ["First"]


def test_a_delta_over_a_sequence_customizes_the_first_message():
    """A simple slot of the delta reaches the main message, not the whole card."""
    base = {
        "messages": [
            {"form": "text", "description": "one"},
            {"form": "text", "description": "two"},
        ],
        "variables": ["ms"],
        "category": "utility",
    }
    merged = _merge_delta(base, {"description": "changed"})
    assert merged["messages"][0]["description"] == "changed"
    assert merged["messages"][1]["description"] == "two"
    # The card metadata stays where it belongs.
    assert merged["variables"] == ["ms"]


def test_a_delta_that_declares_messages_replaces_the_topology():
    """The theme owns the whole topology when it declares ``messages``."""
    base = {
        "messages": [{"form": "text", "description": "one"}],
        "variables": ["ms"],
    }
    merged = _merge_delta(base, {"messages": [{"form": "embed", "title": "new"}]})
    assert merged["messages"] == [{"form": "embed", "title": "new"}]
    assert merged["variables"] == ["ms"]


async def test_nested_messages_are_an_error(tmp_path):
    """A message never nests another sequence."""
    bot = _make_bot(tmp_path)
    resolver = Resolver(bot, 1, {})
    with pytest.raises(DocumentError) as error:
        await build_cards(
            {"messages": [{"messages": [{"form": "text"}]}]},
            "utility.ping",
            resolver,
        )
    assert error.value.code == "card_nested_messages"


async def test_an_empty_sequence_is_an_error(tmp_path):
    """A card with no message at all is a broken document, not an empty send."""
    bot = _make_bot(tmp_path)
    resolver = Resolver(bot, 1, {})
    with pytest.raises(DocumentError) as error:
        await build_cards({"messages": []}, "utility.ping", resolver)
    assert error.value.code == "card_bad_messages"
