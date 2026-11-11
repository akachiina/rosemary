"""A message carries one embed, or a stack of them.

The ``embeds`` slot is the plural of the classic embed: a theme writes the
boxes a message carries (each one with its own title, body, fields and
accent), and the engine sends them stacked, in one message. The top slots of
a document are the short form of a list of one box, so every card that never
declared the slot keeps shipping exactly what it shipped before.

This file pins the stack engine:

* the list decides: the top slots are ignored when the list is declared;
* a delta that declares ``embeds`` replaces the packaged list whole;
* the buttons belong to the message, not to a box;
* Discord's limit (ten boxes) is a document error;
* a slot of the message may not live inside a box;
* the stack only lives in the classic embed form;
* the stack survives the repaint (a classic message keeps its boxes).
"""

from __future__ import annotations

from pathlib import Path

import discord
import pytest

import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.core.card_payload import CardPayload
from rosemary.core.card_resolution import _merge_delta
from rosemary.core.card_service import align_to_message, render_card_message
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


def _two_boxes(first: str = "Caixa 1", second: str = "Caixa 2") -> str:
    """A theme that stacks two boxes in the ping card."""
    return (
        "name: t\n"
        "cards:\n"
        "  utility.ping:\n"
        "    form: embed\n"
        "    embeds:\n"
        f"    - title: '{first}'\n"
        "      description: '{ping.latency}'\n"
        f"    - title: '{second}'\n"
        "      description: 'segunda caixa'\n"
    )


async def test_a_theme_stacks_two_boxes_in_one_message(tmp_path):
    """The list decides: one message, two embeds."""
    bot = _make_bot(tmp_path, _two_boxes())
    await _activate(bot)
    payload, _allowed = await render_card_message(bot, 1, "utility.ping", {"ms": 42})
    assert payload is not None
    assert len(payload.embeds) == 2
    assert payload.embed is payload.embeds[0]
    assert [embed.title for embed in payload.embeds] == ["Caixa 1", "Caixa 2"]
    # The stack rides the ``embeds`` keyword: Discord carries the boxes in
    # one message.
    kwargs = payload.message_kwargs()
    assert "embed" not in kwargs
    assert len(kwargs["embeds"]) == 2


async def test_a_single_box_keeps_the_embed_keyword(tmp_path):
    """A card that never declared the slot keeps shipping one embed."""
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "cards:\n"
        "  utility.ping:\n"
        "    form: embed\n",
    )
    await _activate(bot)
    payload, _allowed = await render_card_message(bot, 1, "utility.ping", {"ms": 42})
    assert payload is not None
    assert len(payload.embeds) == 1
    kwargs = payload.message_kwargs()
    assert "embeds" not in kwargs
    assert kwargs["embed"] is payload.embeds[0]


async def test_the_top_slots_are_ignored_when_the_list_is_declared(tmp_path):
    """The list replaces the short form: the top slots do not ship."""
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "cards:\n"
        "  utility.ping:\n"
        "    form: embed\n"
        "    title: 'Título do topo'\n"
        "    description: 'corpo do topo'\n"
        "    embeds:\n"
        "    - title: 'Caixa 1'\n"
        "      description: 'primeira caixa'\n",
    )
    await _activate(bot)
    payload, _allowed = await render_card_message(bot, 1, "utility.ping", {"ms": 42})
    assert payload is not None
    assert len(payload.embeds) == 1
    assert payload.embeds[0].title == "Caixa 1"
    assert payload.embeds[0].description == "primeira caixa"


def test_a_delta_replaces_the_packaged_list():
    """A list is replaced whole (the ``$append`` aside)."""
    base = {
        "embeds": [{"title": "one"}, {"title": "two"}],
        "variables": ["ms"],
    }
    merged = _merge_delta(base, {"embeds": [{"title": "new"}]})
    assert merged["embeds"] == [{"title": "new"}]
    assert merged["variables"] == ["ms"]


async def test_more_than_ten_boxes_is_an_error(tmp_path):
    """Discord carries ten embeds at most: the document says so."""
    bot = _make_bot(tmp_path)
    resolver = Resolver(bot, 1, {})
    with pytest.raises(DocumentError) as error:
        await build_cards(
            {"form": "embed", "embeds": [{"title": f"box {index}"} for index in range(11)]},
            "utility.ping",
            resolver,
        )
    assert error.value.code == "card_too_many_embeds"


async def test_a_message_slot_inside_a_box_is_an_error(tmp_path):
    """The buttons, the content line and the contract belong to the message."""
    bot = _make_bot(tmp_path)
    resolver = Resolver(bot, 1, {})
    with pytest.raises(DocumentError) as error:
        await build_cards(
            {
                "form": "embed",
                "embeds": [{"title": "box", "actions": [{"label": "Ir", "url": "x"}]}],
            },
            "utility.ping",
            resolver,
        )
    assert error.value.code == "card_bad_embed"


async def test_the_stack_only_lives_in_the_embed_form(tmp_path):
    """The other two shapes have no place for embeds."""
    bot = _make_bot(tmp_path)
    resolver = Resolver(bot, 1, {})
    with pytest.raises(DocumentError) as error:
        await build_cards(
            {"form": "text", "embeds": [{"title": "box"}]},
            "utility.ping",
            resolver,
        )
    assert error.value.code == "card_bad_embeds"


async def test_the_buttons_belong_to_the_message(tmp_path):
    """The actions of the card ride the message, under the last box."""
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "cards:\n"
        "  utility.ping:\n"
        "    form: embed\n"
        "    actions:\n"
        "    - label: 'Abrir'\n"
        "      url: 'https://example.com'\n"
        "    embeds:\n"
        "    - title: 'Caixa 1'\n"
        "      description: 'primeira'\n"
        "    - title: 'Caixa 2'\n"
        "      description: 'segunda'\n",
    )
    await _activate(bot)
    payload, _allowed = await render_card_message(bot, 1, "utility.ping", {"ms": 42})
    assert payload is not None
    assert len(payload.embeds) == 2
    assert payload.view is not None
    assert [button.label for button in payload.view.children] == ["Abrir"]


async def test_every_box_resolves_its_own_copy(tmp_path):
    """Each box is a card of its own: it resolves the variables alone."""
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "cards:\n"
        "  utility.ping:\n"
        "    form: embed\n"
        "    variables: [ms]\n"
        "    embeds:\n"
        "    - title: 'Primeira'\n"
        "      description: '{ms} ms'\n"
        "    - title: 'Segunda'\n"
        "      description: 'fim'\n",
    )
    await _activate(bot)
    payload, _allowed = await render_card_message(bot, 1, "utility.ping", {"ms": 42})
    assert payload is not None
    assert payload.embeds[0].description == "42 ms"
    assert payload.embeds[1].description == "fim"


async def test_a_box_without_copy_does_not_eat_the_message(tmp_path):
    """One empty box does not ship, and the other one still does."""
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "cards:\n"
        "  utility.ping:\n"
        "    form: embed\n"
        "    embeds:\n"
        "    - title: '{missing.copy.key}'\n"
        "    - title: 'Caixa 2'\n"
        "      description: 'segunda'\n",
    )
    await _activate(bot)
    payload, _allowed = await render_card_message(bot, 1, "utility.ping", {"ms": 42})
    assert payload is not None
    assert len(payload.embeds) == 1
    assert payload.embeds[0].title == "Caixa 2"


async def test_a_repaint_of_a_classic_message_keeps_the_stack(tmp_path):
    """A classic message edits the classic card: the boxes stay."""
    bot = _make_bot(tmp_path, _two_boxes())
    await _activate(bot)
    payload, allowed = await render_card_message(bot, 1, "utility.ping", {"ms": 42})
    message = type("Message", (), {"flags": None, "id": 7})()
    aligned, _allowed = await align_to_message(
        bot, 1, "utility.ping", {"ms": 42}, payload, allowed, message
    )
    assert aligned is not None
    assert len(aligned.embeds) == 2


async def test_a_repaint_of_a_v2_message_skips_the_stack(tmp_path):
    """A V2 message cannot carry embeds: the repaint is skipped."""
    bot = _make_bot(tmp_path, _two_boxes())
    await _activate(bot)
    payload, allowed = await render_card_message(bot, 1, "utility.ping", {"ms": 42})
    flags = type("Flags", (), {"value": 1 << 15})()
    message = type("Message", (), {"flags": flags, "id": 7})()
    aligned, _allowed = await align_to_message(
        bot, 1, "utility.ping", {"ms": 42}, payload, allowed, message
    )
    assert aligned is None


def test_the_payload_keeps_both_views_of_the_stack():
    """``embed`` is the first box and ``embeds`` is the whole stack."""
    embed = discord.Embed(title="Uma caixa")
    payload = CardPayload(embed=embed)
    assert payload.embeds == (embed,)
    assert payload.message_kwargs() == {"embed": embed}
    stack = CardPayload(embeds=(embed, discord.Embed(title="Outra")))
    assert stack.embed is embed
    kwargs = stack.message_kwargs()
    assert "embed" not in kwargs
    assert len(kwargs["embeds"]) == 2
