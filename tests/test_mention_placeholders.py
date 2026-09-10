"""Mentions as content: ``{@name}`` syntax, pings toggle, auto AllowedMentions.

The editor lets admins place ``{@user}``/``{user}`` wherever they want; the
renderer collects the ids the resolved text actually contains and builds
``AllowedMentions`` from that, gated by a per-card on/off toggle. These tests
lock the mechanism: syntax resolution, id parsing (users vs roles), toggle
mapping (legacy values keep working), and the three ``allowed_for_*`` entry
points.
"""

from __future__ import annotations

import rosemary.core.card_specs  # noqa: F401  (populates the card registry)
from rosemary.core.card_service import render_card_message
from rosemary.core.cards import (
    document_mention_ids,
    resolve_mention_fields,
    safe_format_mentions,
)
from rosemary.core.mentions import (
    MentionStore,
    allowed_for_document,
    allowed_for_ids,
    allowed_for_text,
    effective_pings,
    pings_default,
    set_pings_for,
)


class _Theme:
    emojis: dict[str, str] = {}
    colors: dict[str, int] = {"brand": 0x5865F2}


class _Translator:
    async def t(self, guild_id, key, **kwargs):
        return key

    async def raw(self, guild_id, key):
        return key


class _Bot:
    def __init__(self, tmp_path):
        from rosemary.core.storage import GuildStorage

        self.storage = GuildStorage(tmp_path)
        self.theme = _Theme()
        self.translator = _Translator()


# -- {@name} syntax ----------------------------------------------------------


def test_mention_placeholder_resolves_and_collects_user():
    text, mentions = safe_format_mentions("{@user} bom dia", {"user": "<@123>"})
    assert text == "<@123> bom dia"
    assert mentions == {"user": ("user", 123)}


def test_mention_placeholder_collects_role_token():
    _text, mentions = safe_format_mentions("{@ping_role}", {"ping_role": "<@&555>"})
    assert mentions == {"ping_role": ("role", 555)}


def test_plain_braces_do_not_collect():
    _text, mentions = safe_format_mentions("{user}", {"user": "<@123>"})
    assert mentions == {}


def test_unknown_mention_placeholder_stays_literal():
    text, mentions = safe_format_mentions("{@who}", {})
    assert text == "{@who}"
    assert mentions == {}


def test_mention_without_token_resolves_silently():
    text, mentions = safe_format_mentions("{@user}", {"user": "texto"})
    assert text == "texto"
    assert mentions == {}


def test_repeated_mention_collects_once():
    _text, mentions = safe_format_mentions("{@u} e {@u}", {"u": "<@9>"})
    assert list(mentions.items()) == [("u", ("user", 9))]


def test_document_mention_ids_dedupes_and_splits_kinds():
    doc = {
        "v": 1,
        "blocks": [
            {"type": "text", "body": "{@winner} {@winner} {@ping_role}"},
            {"type": "text", "body": "sem menção"},
        ],
    }
    users, roles = document_mention_ids(doc, {"winner": "<@7>", "ping_role": "<@&8>"})
    assert users == [7]
    assert roles == [8]


def test_document_mention_ids_reads_button_labels():
    doc = {
        "v": 1,
        "blocks": [
            {
                "type": "row",
                "buttons": [{"id": "b1", "label": "{@user}", "url": "https://x.y"}],
            }
        ],
    }
    users, roles = document_mention_ids(doc, {"user": "<@42>"})
    assert users == [42]
    assert roles == []


def test_document_mention_ids_ignores_plain_text_fields():
    doc = {"v": 1, "blocks": [{"type": "text", "body": "<@999> literal token"}]}
    users, roles = document_mention_ids(doc, {})
    assert users == [] and roles == []


def test_resolve_mention_fields_swaps_only_mention_fields():
    doc = {
        "v": 1,
        "blocks": [
            {"type": "text", "body": "{@user} ou {user}"},
            {"type": "divider"},
        ],
    }
    mapping = {"user": "<@5>"}
    resolved = resolve_mention_fields(doc, mapping)
    body = resolved["blocks"][0]["body"]
    assert body == "<@5> ou <@5>"
    # Non-mention fields are untouched and the input is never mutated.
    assert doc["blocks"][0]["body"] == "{@user} ou {user}"


def test_resolve_mention_fields_noop_without_mentions():
    doc = {"v": 1, "blocks": [{"type": "text", "body": "{user}"}]}
    assert resolve_mention_fields(doc, {"user": "<@5>"}) is doc


# -- pings toggle ------------------------------------------------------------


def test_pings_default_follows_spec():
    assert pings_default("bump.thank_you") is True
    assert pings_default("bump.no_bumps") is False
    assert pings_default("bump.logs.week_reset.description") is False


async def test_pings_effective_and_toggle(tmp_path):
    bot = _Bot(tmp_path)
    assert await effective_pings(bot, 1, "bump.thank_you") is True
    await set_pings_for(bot, 1, "bump.thank_you", False)
    assert await effective_pings(bot, 1, "bump.thank_you") is False
    await set_pings_for(bot, 1, "bump.thank_you", True)
    assert await effective_pings(bot, 1, "bump.thank_you") is True


async def test_legacy_store_values_map_to_toggle(tmp_path):
    bot = _Bot(tmp_path)
    store = MentionStore(tmp_path)
    await store.set_policy(1, "bump.thank_you", "single")
    assert await effective_pings(bot, 1, "bump.thank_you") is True
    await store.set_policy(1, "bump.thank_you", "none")
    assert await effective_pings(bot, 1, "bump.thank_you") is False


# -- allowed_for_* entry points ----------------------------------------------


async def test_allowed_for_ids_gated_by_toggle(tmp_path):
    bot = _Bot(tmp_path)
    allowed = await allowed_for_ids(bot, 1, "bump.thank_you", user_ids=[7])
    assert allowed.to_dict().get("users") == [7]
    await set_pings_for(bot, 1, "bump.thank_you", False)
    allowed = await allowed_for_ids(bot, 1, "bump.thank_you", user_ids=[7])
    assert allowed.to_dict() == {"parse": []}


async def test_allowed_for_ids_silent_overrides_toggle(tmp_path):
    bot = _Bot(tmp_path)
    allowed = await allowed_for_ids(bot, 1, "bump.thank_you", user_ids=[7], silent=True)
    assert allowed.to_dict() == {"parse": []}


async def test_allowed_for_ids_role_ids(tmp_path):
    bot = _Bot(tmp_path)
    allowed = await allowed_for_ids(bot, 1, "bump.reminder", role_ids=[55])
    data = allowed.to_dict()
    assert data.get("roles") == [55]


async def test_allowed_for_text_parses_present_tokens_only(tmp_path):
    bot = _Bot(tmp_path)
    text = "obrigado <@31>, meta <@&66>"
    allowed = await allowed_for_text(bot, 1, "bump.thank_you", text)
    data = allowed.to_dict()
    assert data.get("users") == [31]
    assert data.get("roles") == [66]
    allowed = await allowed_for_text(bot, 1, "bump.thank_you", "sem tokens <@3>")
    assert allowed.to_dict().get("users") == [3]


async def test_allowed_for_document_gated_by_toggle(tmp_path):
    bot = _Bot(tmp_path)
    doc = {"v": 1, "blocks": [{"type": "text", "body": "{@user}!"}]}
    mapping = {"user": "<@12>"}
    allowed = await allowed_for_document(bot, 1, "bump.thank_you", doc, mapping)
    assert allowed.to_dict().get("users") == [12]
    await set_pings_for(bot, 1, "bump.thank_you", False)
    allowed = await allowed_for_document(bot, 1, "bump.thank_you", doc, mapping)
    assert allowed.to_dict() == {"parse": []}


# -- render_card_message -----------------------------------------------------


async def test_render_card_message_none_without_document(tmp_path):
    bot = _Bot(tmp_path)
    view, allowed = await render_card_message(bot, 1, "no.such.card")
    assert view is None
    assert allowed.to_dict() == {"parse": []}


async def test_render_card_message_returns_view_and_mentions(tmp_path):
    bot = _Bot(tmp_path)
    from rosemary.core.cards import card_store

    doc = {"v": 1, "blocks": [{"type": "text", "body": "{@user} ganhou!"}]}
    await card_store(bot).save_document(1, "bump.thank_you", doc)
    view, allowed = await render_card_message(
        bot, 1, "bump.thank_you", {"user": "<@77>"}
    )
    assert view is not None
    assert allowed.to_dict().get("users") == [77]


async def test_render_card_message_silent_never_pings(tmp_path):
    bot = _Bot(tmp_path)
    from rosemary.core.cards import card_store

    doc = {"v": 1, "blocks": [{"type": "text", "body": "{@user} ganhou!"}]}
    await card_store(bot).save_document(1, "bump.thank_you", doc)
    _view, allowed = await render_card_message(
        bot, 1, "bump.thank_you", {"user": "<@77>"}, silent=True
    )
    assert allowed.to_dict() == {"parse": []}


async def test_render_card_message_invalid_document_is_none(tmp_path):
    bot = _Bot(tmp_path)
    from rosemary.core.cards import card_store

    await card_store(bot).save_document(
        1, "bump.thank_you", {"v": 1, "blocks": [{"type": "bogus"}]}
    )
    view, allowed = await render_card_message(bot, 1, "bump.thank_you", {})
    assert view is None
    assert allowed.to_dict() == {"parse": []}


def test_mention_placeholder_in_url_is_accepted_by_validation():
    from rosemary.core.cards import validate_document

    doc = {
        "v": 1,
        "blocks": [
            {
                "type": "row",
                "buttons": [{"id": "b1", "label": "x", "url": "{@link}"}],
            }
        ],
    }
    assert validate_document(doc, theme=_Theme(), draft=True) == []
