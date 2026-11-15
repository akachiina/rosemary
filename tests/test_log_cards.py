"""The staff log cards: the documents of the log family.

The log events are documents now (``themes/default/cards/logs.yaml``): the
document says what the log is (title, body, fields, author, footer) and the
catalog carries the copy. These tests pin the frame of the family: the
fields, the avatar, the footer and the legacy theme overrides.
"""

from __future__ import annotations

from golden_cards import make_bot

import rosemary.core.card_specs  # noqa: F401  (registers every card)
from rosemary.core.card_service import render_card_message
from rosemary.core.cards import get_card
from rosemary.core.theme_package import default_package
from rosemary.ui.theme import load_theme


async def _render(bot, key: str, variables: dict | None = None, *, form: str | None = None):
    """Render one log card and return its payload."""
    payload, _allowed = await render_card_message(bot, 1, key, variables or {}, form=form)
    return payload


def test_every_log_event_has_a_document():
    """The package carries the whole log family, with its contract."""
    package = default_package()
    # The log family: the staff log events, the settings log and the trace.
    events = [
        key
        for key in package.card_keys()
        if ".logs." in key or key in ("settings.log", "debug.trace")
    ]
    assert len(events) == 75
    for key in events:
        spec = get_card(key)
        assert spec is not None, key
        meta = package.metadata(key)
        assert spec.category == meta["category"]
        assert spec.variables == meta["variables"]
        assert spec.form == meta["form"]


async def test_a_fields_log_shows_its_fields_and_a_readable_footer():
    bot = make_bot()
    payload = await _render(
        bot,
        "moderation.logs.ban",
        {
            "member": "<@2>",
            "member_name": "Ana",
            "member_id": "123456789012345678",
            "user_avatar": "https://cdn.example/2.png",
            "moderator": "<@3>",
            "actor": "<@3>",
            "actor_name": "Bob",
            "reason": "spam",
            "timestamp": "10/11/2026 17:08",
        },
    )
    embed = payload.embed
    assert embed.title == "🚫 Banimento"
    assert [field.name for field in embed.fields] == [
        "👥 Alvo",
        "🔢 ID do membro",
        "⚙️ Moderador(a)",
        "📜 Motivo",
    ]
    assert embed.fields[0].value == "<@2>"
    # A machine value (a snowflake) renders as inline code.
    assert embed.fields[1].value == "`123456789012345678`"
    assert embed.fields[3].value == "spam"
    # The author is the subject (name and avatar), the footer names the actor
    # in readable text: neither line carries a raw mention or date token.
    assert embed.author.name == "Ana"
    assert embed.author.icon_url == "https://cdn.example/2.png"
    assert embed.footer.text == "por Bob · 10/11/2026 17:08"
    assert embed.footer.icon_url is None


async def test_the_moderation_log_shows_the_target_avatar_as_the_author_icon():
    bot = make_bot()
    payload = await _render(
        bot,
        "moderation.logs.warn",
        {
            "member": "<@2>",
            "member_name": "Ana",
            "member_id": "2",
            "moderator": "<@3>",
            "reason": "spam",
            "actor": "<@3>",
            "actor_name": "Bob",
            "user_avatar": "https://cdn.example/2.png",
            "timestamp": "10/11/2026 17:08",
        },
    )
    embed = payload.embed
    assert embed.author.name == "Ana"
    assert embed.author.icon_url == "https://cdn.example/2.png"
    # The avatar is identity, not decoration: it never repeats as a thumbnail.
    assert embed.thumbnail is None


async def test_the_leave_log_uses_the_avatar_as_the_author_icon():
    bot = make_bot()
    payload = await _render(
        bot,
        "invites.logs.leave",
        {
            "user": "<@2>",
            "user_name": "Ana",
            "inviter": "<@3>",
            "user_avatar": "https://cdn.example/left.png",
            "timestamp": "10/11/2026 17:08",
        },
    )
    embed = payload.embed
    # Nobody acted: the member is the author, with their own icon.
    assert embed.author.name == "Ana"
    assert embed.author.icon_url == "https://cdn.example/left.png"
    assert embed.thumbnail is None
    assert embed.description is None
    assert [(f.name, f.value) for f in embed.fields] == [
        ("👥 Membro", "<@2>"),
        ("👥 Convidador", "<@3>"),
    ]
    assert embed.footer.text == "10/11/2026 17:08"


async def test_the_title_uses_the_theme_title_preset():
    bot = make_bot()
    bot.theme.markdown["title"] = "## {title}"
    payload = await _render(bot, "bump.logs.channel_locked", {"channel": "#geral"}, form="card")
    container = payload.view.children[0]
    body = container.items[0].content
    assert body == "## 🔒 Canal Bloqueado"


async def test_the_actor_footer_when_the_body_does_not_name_the_actor():
    bot = make_bot()
    payload = await _render(
        bot,
        "boost.logs.register",
        {
            "actor": "<@9>",
            "actor_name": "Bob",
            "timestamp": "10/11/2026 17:08",
        },
    )
    assert payload.embed.footer.text == "por Bob · 10/11/2026 17:08"


async def test_the_system_footer_when_there_is_no_actor():
    bot = make_bot()
    payload = await _render(
        bot,
        "bump.logs.channel_locked",
        {"channel": "#geral", "timestamp": "10/11/2026 17:08"},
    )
    assert payload.embed.footer.text == "10/11/2026 17:08"


async def test_a_raw_mention_or_date_token_never_reaches_author_or_footer():
    """A guild theme may reintroduce the old tokens: the serializer drops them."""
    bot = make_bot()
    payload = await _render(
        bot,
        "moderation.logs.ban",
        {
            "member": "<@2>",
            "member_name": "<@2>",
            "member_id": "2",
            "moderator": "<@3>",
            "actor": "<@3>",
            "actor_name": "<@3>",
            "reason": "spam",
            "timestamp": "<t:1:R>",
        },
    )
    embed = payload.embed
    assert embed.author is None
    assert embed.footer is None


async def test_the_legacy_body_override_keeps_the_new_title_and_footer():
    bot = make_bot()
    bot.theme.cards = {
        "moderation.logs.ban.description": {
            "v": 1,
            "blocks": [
                {
                    "type": "container",
                    "color": "info",
                    "children": [
                        {"type": "text", "body": "legacy body for {member}"}
                    ],
                }
            ],
        }
    }
    payload = await _render(
        bot,
        "moderation.logs.ban",
        {
            "member": "<@2>",
            "member_name": "Ana",
            "member_id": "2",
            "moderator": "<@3>",
            "reason": "spam",
            "actor": "<@3>",
            "actor_name": "Bob",
            "timestamp": "10/11/2026 17:08",
        },
    )
    embed = payload.embed
    assert embed.title == "🚫 Banimento"
    assert embed.description == "legacy body for <@2>"
    # The fields gave way to the legacy body, the frame stayed.
    assert embed.fields == []
    assert embed.footer.text == "por Bob · 10/11/2026 17:08"


async def test_a_full_card_override_wins_over_the_document():
    bot = make_bot()
    override = {
        "v": 1,
        "blocks": [
            {
                "type": "container",
                "color": "danger",
                "children": [{"type": "text", "body": "full override"}],
            }
        ],
    }
    bot.theme.cards = {"moderation.logs.ban": override}
    payload = await _render(bot, "moderation.logs.ban", {"member": "<@2>"})
    assert payload.embed is None
    body = payload.view.children[0].items[0].content
    assert body == "full override"


def test_the_default_theme_still_carries_the_log_copy():
    """The copy of a log lives in the catalogs, not in the document."""
    theme = load_theme()
    assert theme.emojis  # the theme is loaded
    package = default_package()
    document = package.document("moderation.logs.ban")
    assert document["title"] == "{moderation.logs.ban.title}"
    assert document["fields"][0] == {"target": "{member}"}
