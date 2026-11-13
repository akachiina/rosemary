"""The Components V2 dialect of a card: the opening phrase and the avatar.

A card says what it is; every form writes it in its own dialect. The V2 had
two gaps (the notification phrase and the subject's picture) and one policy
gap (a mention in the text of a V2 card did not notify). These tests pin the
dialect the engine draws and the policy it applies, so a card that ships as a
V2 keeps saying the same thing as the embed it replaced.
"""

from __future__ import annotations

from typing import Any

from card_forms import payload_texts, payload_thumbnail
from golden_cards import GUILD_ID, make_bot

from rosemary.core.card_service import render_card_message

#: The variables of the welcome event (the card with a phrase and an avatar).
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


async def test_the_opening_line_rides_above_the_frame():
    """The sentence stays outside the card.

    The embed carried it in the message content, above the box; the V2
    keeps it above the frame (a block of its own), so the card does not
    swallow the sentence that introduces it.
    """
    import discord

    bot = make_bot()
    payload, _allowed = await render_card_message(
        bot, GUILD_ID, "events.welcome", WELCOME, form="card"
    )
    assert payload is not None
    children = list(getattr(payload.view, "children", []) or [])
    assert children, "the card says nothing"
    first = children[0]
    assert isinstance(first, discord.ui.TextDisplay), type(first)
    assert first.content.startswith("Boas-vindas"), first.content
    # The frame (the container) carries the card itself.
    assert isinstance(children[1], discord.ui.Container), type(children[1])


async def test_the_avatar_rides_the_v2_thumbnail():
    """The V2 card has no author line: the picture is the thumbnail.

    The embed had a frame for the author; the V2 draws the subject's picture
    as the accessory of the title section (the native thumbnail), so the card
    keeps the identity without repeating the name above the title.
    """
    bot = make_bot()
    payload, _allowed = await render_card_message(
        bot, GUILD_ID, "events.welcome", WELCOME, form="card"
    )
    assert payload is not None
    texts = payload_texts(payload)
    # The name is not a line of the card: the body mentions the member.
    assert "**Ana**" not in texts, texts
    # The picture is the accessory of the title section (the thumbnail).
    assert payload_thumbnail(payload) == "https://a.b/ana.png"
    # The card draws no picture of its own: nothing rides the message.
    assert payload.files == []
    assert _pictures(payload.view) == ["https://a.b/ana.png"]
