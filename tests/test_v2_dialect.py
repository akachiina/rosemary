"""The Components V2 dialect of a card: the opening phrase and the author.

A card says what it is; every form writes it in its own dialect. The V2 had
two gaps (the notification phrase and the author line) and one policy gap (a
mention in the text of a V2 card did not notify). These tests pin the dialect
the engine draws and the policy it applies, so a card that ships as a V2 keeps
saying the same thing as the embed it replaced.
"""

from __future__ import annotations

from typing import Any

from card_forms import payload_strip, payload_texts
from golden_cards import GUILD_ID, make_bot

from rosemary.core.card_service import render_card_message

#: The variables of the welcome event (the card with a phrase and an author).
WELCOME = {
    "user": "<@42>",
    "user_name": "Ana",
    "user_id": "42",
    "server": "Serv",
    "count": "3",
    "user_avatar": "https://a.b/ana.png",
    "inviter": "<@7>",
}

#: The variables of the bump leaderboard (the card whose phrase names the
#: winner and whose body names nobody).
LEADERBOARD = {
    "winner_mention": "<@42>",
    "winner_name": "Ana",
    "winner_count": "3",
    "winner_avatar": "https://a.b/ana.png",
    "role_name": "Membro",
    "leaderboard_list": "Ana: 3",
    "start_timestamp": "01/01",
    "end_timestamp": "08/01",
    "next_reset_timestamp": "08/01",
    "user": "<@9>",
    "timestamp": "11/11/2026 10:00",
}


def _pictures(view: Any) -> list[str]:
    """The URL of every picture of a rendered view (the accessories)."""
    found: list[str] = []

    def walk(node: Any) -> None:
        accessory = getattr(node, "accessory", None)
        url = getattr(accessory, "url", "")
        if isinstance(url, str) and url:
            found.append(url)
        for child in list(getattr(node, "children", []) or []) + list(
            getattr(node, "items", []) or []
        ):
            walk(child)

    for item in getattr(view, "children", []) or []:
        walk(item)
    return found


def _pings(allowed: Any) -> list[int]:
    """The ids a rendered card may notify."""
    return sorted(int(user.id) for user in (allowed.users or []))


async def test_the_notification_phrase_opens_the_v2_card():
    """The phrase the embed carries in the message content opens the V2 card.

    The embed ships it above the box; a V2 message cannot carry a top-level
    content, so the phrase rides as the first block of the card.
    """
    bot = make_bot()
    payload, allowed = await render_card_message(
        bot, GUILD_ID, "bump.leaderboard", LEADERBOARD, form="card"
    )
    assert payload is not None
    texts = payload_texts(payload)
    assert texts, "the card says nothing"
    assert texts[0].startswith("Parabéns"), texts
    assert "<@42>" in texts[0], texts
    # The phrase is the notification: it may ping the member it names.
    assert _pings(allowed) == [42]


async def test_the_body_of_a_v2_card_never_notifies():
    """The body of a V2 card is message content, but it never notifies.

    In the embed the body lives inside the box (an embed never notifies), so
    the V2 keeps that intent: only the mention fields (the ``{@name}`` ones)
    and the opening phrase may ping.
    """
    from rosemary.core.card_service import _allowed_for_card
    from rosemary.core.design.compile import compile_card
    from rosemary.core.design.ir import Card

    bot = make_bot()
    card = Card(
        key="events.welcome",
        form="card",
        title="ola",
        paragraphs=("oi <@99>",),
        ping_line="ola <@42>",
    )
    compiled = compile_card(card, {})
    allowed = await _allowed_for_card(
        bot, GUILD_ID, "events.welcome", compiled, "ola <@42>", False
    )
    assert _pings(allowed) == [42]


async def test_a_mention_field_notifies():
    """A ``{@name}`` field is the document's own choice: it may ping."""
    bot = make_bot()
    payload, allowed = await render_card_message(
        bot,
        GUILD_ID,
        "roleplay.card",
        {
            "actor": "<@42>",
            "actor_name": "Ana",
            "target": "<@43>",
            "actor_avatar": "https://a.b/ana.png",
            "action": "abraçou",
            "love_letter": "uma carta",
            "gif_url": "",
            "timestamp": "11/11/2026 10:00",
        },
        form="card",
    )
    assert payload is not None
    assert _pings(allowed) == [42, 43]


async def test_the_pings_toggle_silences_the_v2_card():
    """The theme's pings toggle governs the phrase: off means no phrase."""
    import tempfile
    from pathlib import Path

    from rosemary.core.storage import GuildStorage
    from rosemary.core.themes import ThemeStore, preload_themes

    root = Path(tempfile.mkdtemp())
    themes = root / "themes"
    themes.mkdir()
    (themes / "quiet.yaml").write_text(
        "name: quiet\npings:\n  bump.leaderboard: false\n", encoding="utf-8"
    )
    bot = make_bot()
    bot.storage = GuildStorage(root)
    bot._theme_store = ThemeStore(root, themes_dir=themes)
    bot.guilds = [type("G", (), {"id": GUILD_ID})()]
    await bot._theme_store.set_active(GUILD_ID, "quiet")
    await preload_themes(bot)

    payload, allowed = await render_card_message(
        bot, GUILD_ID, "bump.leaderboard", LEADERBOARD, form="card"
    )
    assert payload is not None
    texts = payload_texts(payload)
    assert not any("Parabéns" in text for text in texts), texts
    assert _pings(allowed) == []


async def test_the_author_line_rides_the_v2_card():
    """The author line of the V2 card is the drawn strip.

    The embed has a frame for it (the author line); the V2 draws the name
    and the picture as a picture of its own (the media gallery), so the card
    keeps the attribution without a section of its own.
    """
    bot = make_bot()
    payload, _allowed = await render_card_message(
        bot, GUILD_ID, "events.welcome", WELCOME, form="card"
    )
    assert payload is not None
    texts = payload_texts(payload)
    # The name lives in the drawing, not in a line of text.
    assert "**Ana**" not in texts, texts
    strip = payload_strip(payload)
    assert strip.startswith("attachment://author_"), strip
    # The picture rides the message and the gallery points at it.
    assert len(payload.files) == 1
    assert payload.files[0].filename == strip.removeprefix("attachment://")
    # The picture of the card is not an accessory anymore.
    assert _pictures(payload.view) == []


async def test_without_the_drawing_the_author_line_stays_native(monkeypatch):
    """A host that cannot draw keeps the native line: the name in bold and
    the picture as the accessory of the section."""
    from rosemary.core.render import elements

    monkeypatch.setattr(elements, "render_element", lambda *args, **kwargs: None)
    bot = make_bot()
    payload, _allowed = await render_card_message(
        bot, GUILD_ID, "events.welcome", WELCOME, form="card"
    )
    assert payload is not None
    texts = payload_texts(payload)
    assert "**Ana**" in texts, texts
    assert payload_strip(payload) == ""
    assert _pictures(payload.view) == ["https://a.b/ana.png"]
