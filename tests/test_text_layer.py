"""The plain-text layer: one notice line, one emoji, the theme's prefix.

Rosemary speaks three shapes: plain text (acknowledgements and notices),
classic embeds (logs and content) and Components V2 (the interactive UI).
This file pins the text layer:

* the line opens with the theme prefix and the copy follows (the title of a
  multi-line notice is bold), and the prefix comes from the theme
  (``feedback_*`` styles), so a guild restyles every notice at once;
* the prefix owns the only emoji of the line: the copy never opens with one;
* the placeholders of the card contract resolve (no raw ``{ms}``);
* an embed never notifies: an essential ping travels in the message content
  and the allowed-mentions bundle carries it.
"""

from __future__ import annotations

from pathlib import Path

import discord

import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.core.card_service import render_card_message
from rosemary.core.cards import get_card
from rosemary.core.i18n import Translator
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore, preload_themes
from rosemary.ui.theme import load_theme


def _make_bot(tmp_path, themes_yaml: str | None = None):
    """A bot with the real catalogs, the real theme store and (optionally)
    one guild theme."""

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


def _make_bot_in(language: str, tmp_path):
    """A bot whose guild speaks ``language`` (the catalogs are the real ones)."""
    bot = _make_bot(tmp_path)
    bot.translator = Translator(
        Path("language"),
        resolver=lambda guild_id: language,
        default_placeholders=bot.theme.emojis,
    )
    return bot


async def _activate(bot) -> None:
    await bot._theme_store.set_active(1, "t")
    await preload_themes(bot)


#: the text layer ----------------------------------------------------------


async def test_text_card_is_one_notice_line(tmp_path):
    """``/ping`` ships as plain text: the prefix, the bold title and the body."""
    bot = _make_bot(tmp_path)
    payload, _allowed = await render_card_message(
        bot, 1, "utility.ping", {"ms": 42}
    )
    assert payload is not None
    assert payload.view is None and payload.embed is None
    assert payload.content == "ℹ️ **Pong!**\nLatency: 42ms"


async def test_text_card_resolves_the_placeholders(tmp_path):
    """No raw contract name survives: the copy is resolved before the send."""
    bot = _make_bot(tmp_path)
    payload, _allowed = await render_card_message(
        bot, 1, "utility.ping", {"ms": 7}
    )
    assert payload is not None and payload.content
    assert "{ms}" not in payload.content
    assert "7ms" in payload.content


async def test_text_card_resolves_the_mention_fields(tmp_path):
    """A ``{@user}`` field resolves like ``{user}`` and may ping."""
    bot = _make_bot(tmp_path, "name: t\npings:\n  anti_invite.warning: true\n")
    await _activate(bot)
    payload, allowed = await render_card_message(
        bot, 1, "anti_invite.warning", {"user": "<@55>"}
    )
    assert payload is not None and payload.content
    assert "<@55>" in payload.content
    assert "{user}" not in payload.content
    # The card's mention policy decides: the theme opted this card in.
    assert [user.id for user in (allowed.users or [])] == [55]


async def test_theme_owns_the_notice_prefix(tmp_path):
    """The prefix is a theme style: a guild restyles every notice at once."""
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "emojis:\n"
        '  star: "⭐"\n'
        "styles:\n"
        '  feedback_info: { prefix: "{star} >> " }\n',
    )
    await _activate(bot)
    payload, _allowed = await render_card_message(
        bot, 1, "utility.ping", {"ms": 42}
    )
    assert payload is not None and payload.content
    assert payload.content.startswith("⭐ >> ")


async def test_the_prefix_owns_the_only_emoji(tmp_path):
    """The copy never opens with an emoji: the prefix marks the line."""
    bot = _make_bot(tmp_path)
    payload, _allowed = await render_card_message(
        bot, 1, "cleaner.result", {"count": 3, "matched": 3, "criterion": "spam"}
    )
    assert payload is not None and payload.content
    head = payload.content.splitlines()[0]
    assert head.startswith("✅ ")
    assert "**|**" not in head
    # One emoji on the line: the prefix owns it.
    assert head.count("✅") == 1


#: the embed layer ---------------------------------------------------------


async def test_embed_ping_travels_in_the_content(tmp_path):
    """An embed never notifies: the essential phrase rides the content.

    The document declares the phrase it wants in the message content
    (``ping_line``); the embed keeps the mention chip and never pings.
    """
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "colors:\n"
        '  brand: "#57F287"\n'
        "cards:\n"
        "  about.card:\n"
        "    form: embed\n"
        '    title: "Hi"\n'
        '    description: "Hello {user}"\n'
        '    ping_line: "{user}, come see"\n'
        "    color: brand\n"
        "pings:\n"
        "  about.card: true\n",
    )
    await _activate(bot)
    payload, allowed = await render_card_message(
        bot, 1, "about.card", {"user": "<@77>"}
    )
    assert payload is not None and payload.embed is not None
    # The phrase travels in the message content: that is the only part of a
    # message that notifies. The embed keeps the chip (it never pings).
    assert payload.content == "<@77>, come see"
    assert [user.id for user in (allowed.users or [])] == [77]


async def test_embed_ping_stays_silent_when_the_toggle_is_off(tmp_path):
    """The theme's ``pings:`` toggle wins: no content, no mention."""
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "colors:\n"
        '  brand: "#57F287"\n'
        "pings:\n"
        "  about.card: false\n"
        "cards:\n"
        "  about.card:\n"
        "    embed:\n"
        '      title: "Hi"\n'
        '      description: "Hello {@user}"\n'
        "      color: brand\n",
    )
    await _activate(bot)
    payload, allowed = await render_card_message(
        bot, 1, "about.card", {"user": "<@77>"}
    )
    assert payload is not None and payload.embed is not None
    assert not payload.content
    assert allowed.everyone is False
    assert not allowed.users


async def test_embed_card_ships_without_a_view(tmp_path):
    """A pure embed card has no Components V2 items: the classic shape."""
    import rosemary.cogs.utility  # noqa: F401  (registers the builder)

    bot = _make_bot(tmp_path)
    payload, _allowed = await render_card_message(
        bot, 1, "utility.serverinfo", {"server": "Girassol"}
    )
    assert payload is not None
    assert payload.embed is not None
    assert payload.view is None
    assert payload.content is None


#: the form axis -----------------------------------------------------------


async def test_the_theme_picks_the_form_of_a_card(tmp_path):
    """``cards.<key>.form`` swaps the shape without rewriting the copy."""
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "colors:\n"
        '  info: "#3498DB"\n'
        "cards:\n"
        "  utility.ping:\n"
        "    form: embed\n",
    )
    await _activate(bot)
    payload, _allowed = await render_card_message(
        bot, 1, "utility.ping", {"ms": 42}
    )
    assert payload is not None
    assert payload.embed is not None
    assert payload.content is None
    assert "42ms" in (payload.embed.description or "") + (payload.embed.title or "")


async def test_a_themed_copy_keeps_the_text_surface(tmp_path):
    """A text card customized with a plain copy stays a plain message."""
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "cards:\n"
        "  utility.ping:\n"
        "    - type: 10\n"
        '      content: "Pong {ms}"\n',
    )
    await _activate(bot)
    payload, _allowed = await render_card_message(
        bot, 1, "utility.ping", {"ms": 42}
    )
    assert payload is not None
    assert payload.view is None
    assert payload.content == "ℹ️ Pong 42"


async def test_a_themed_copy_keeps_the_embed_surface(tmp_path):
    """A copy-only override carries no layout: an embed card stays an embed."""
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "colors:\n"
        '  brand: "#57F287"\n'
        "cards:\n"
        "  about.card:\n"
        "    - type: 10\n"
        '      content: "Version {version}"\n',
    )
    await _activate(bot)
    payload, _allowed = await render_card_message(
        bot, 1, "about.card", {"version": "1.2.3"}
    )
    assert payload is not None
    assert payload.embed is not None
    assert payload.view is None
    assert "1.2.3" in (payload.embed.description or "")


async def test_a_themed_layout_ships_as_components_v2(tmp_path):
    """A themed *structural* document is the opposite: the theme asked for
    a rich layout, so the card ships as Components V2."""
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "cards:\n"
        "  utility.ping:\n"
        "    - type: 17\n"
        "      accent_color: 5865F2\n"
        "      components:\n"
        "        - type: 10\n"
        '          content: "Pong {ms}"\n',
    )
    await _activate(bot)
    payload, _allowed = await render_card_message(
        bot, 1, "utility.ping", {"ms": 42}
    )
    assert payload is not None
    assert payload.view is not None
    assert payload.content is None


async def test_a_themed_embed_can_ship_as_text(tmp_path):
    """Forced to the text form, a themed embed loses the layout and keeps
    the copy: the line carries the title and the body."""
    bot = _make_bot(
        tmp_path,
        "name: t\n"
        "cards:\n"
        "  about.card:\n"
        "    embed:\n"
        '      title: "About"\n'
        '      description: "Version {version}"\n',
    )
    await _activate(bot)
    payload, _allowed = await render_card_message(
        bot, 1, "about.card", {"version": "1.2.3"}, form="text"
    )
    assert payload is not None
    assert payload.view is None
    assert payload.embed is None
    assert payload.content is not None
    assert "1.2.3" in payload.content
    assert "About" in payload.content


async def test_the_allowed_mentions_never_open_the_door(tmp_path):
    """A card without mentions sends a closed bundle."""
    bot = _make_bot(tmp_path)
    _payload, allowed = await render_card_message(
        bot, 1, "utility.ping", {"ms": 42}
    )
    assert isinstance(allowed, discord.AllowedMentions)
    assert allowed.everyone is False
    assert not allowed.users
    assert not allowed.roles


#: the code fallback -------------------------------------------------------


async def test_the_fallback_follows_the_card_form(tmp_path):
    """``simple_payload`` never changes the surface under the member."""
    from rosemary.core.card_service import simple_payload

    bot = _make_bot(tmp_path)
    text_card = simple_payload(bot, 1, "utility.ping", title="Pong", body="42ms")
    assert text_card.content == "ℹ️ **Pong**\n42ms"
    assert text_card.embed is None
    assert text_card.view is None

    embed_card = simple_payload(bot, 1, "about.card", title="About", body="Hi")
    assert embed_card.embed is not None
    assert embed_card.embed.title == "About"
    assert embed_card.embed.description == "Hi"
    assert embed_card.view is None


#: the notice never notifies ------------------------------------------------


async def test_a_notice_never_notifies(tmp_path):
    """A notice is plain content now, so it must carry a closed policy.

    The Components V2 container it replaced never notified: the copy may
    name the member the bot is about to ban, and that member must not be
    pinged by the acknowledgement.
    """
    from rosemary.core.feedback import respond

    class Response:
        def is_done(self) -> bool:
            return False

    class Ctx:
        def __init__(self) -> None:
            self.response = Response()
            self.calls: list[dict] = []

        async def respond(self, *args, **kwargs):
            self.calls.append({"args": args, "kwargs": kwargs})

    bot = _make_bot(tmp_path)
    ctx = Ctx()
    await respond(ctx, bot, 1, "Banned <@7> for spam.", style="success")
    (content,) = ctx.calls[0]["args"]
    assert "<@7>" in content
    mentions = ctx.calls[0]["kwargs"]["allowed_mentions"]
    assert isinstance(mentions, discord.AllowedMentions)
    assert mentions.everyone is False
    assert not mentions.users
    assert not mentions.roles


async def test_the_confirmation_never_notifies(tmp_path):
    """The moderation confirmation names the target: it must not ping them."""
    from rosemary.ui.confirm import ConfirmView

    class Translator:
        async def t(self, guild_id, key, **variables) -> str:
            return key

    class Bot:
        pass

    bot = Bot()
    bot.theme = load_theme()
    bot.translator = Translator()

    class Question(ConfirmView):
        async def question_items(self):
            return []

        async def question_text(self) -> str:
            return "Ban <@7>?"

    view = Question(bot, guild_id=1, owner_id=None, on_confirm=lambda _i: "done")
    kwargs = await view.message_kwargs()
    assert "<@7>" in kwargs["content"]
    mentions = kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert not mentions.users
    assert not mentions.roles

    payload = await view.rerender_payload()
    assert payload["allowed_mentions"].users is False


#: the copy never opens with an emoji ---------------------------------------

async def test_the_text_copies_never_open_with_an_emoji(tmp_path):
    """The prefix owns the only emoji of the line: the copy never opens with one.

    Swept over every text card in both catalogs: a copy that brings its own
    mark would show two emojis on the line.
    """
    import re

    from rosemary.core.theme_package import default_package
    from rosemary.core.variables import samples_for

    emoji = re.compile(
        "[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF\u2190-\u21FF]"
    )
    package = default_package()
    keys = [
        key
        for key in package.card_keys()
        if package.metadata(key)["form"] == "text"
    ]
    assert keys, "the package carries no text card"
    for language in ("en-US", "pt-BR"):
        bot = _make_bot_in(language, tmp_path)
        for key in keys:
            spec = get_card(key)
            variables = samples_for(spec.variables) if spec else {}
            payload, _allowed = await render_card_message(bot, 1, key, variables)
            assert payload is not None, key
            content = payload.content or ""
            assert content, key
            assert "**|**" not in content, key
            # The prefix owns the mark: what follows it is plain prose (the
            # serializer may bold the first line as the title).
            head_line = content.splitlines()[0]
            body = head_line.split(" ", 1)[1] if " " in head_line else head_line
            head = body.lstrip("*")
            assert not emoji.match(head), f"{language} {key}: {head!r}"
