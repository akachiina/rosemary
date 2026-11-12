"""Repaint: the card follows the shape of the message it edits.

Discord's ``IS_COMPONENTS_V2`` flag is immutable: a V2 message can never
carry content or an embed, and a classic message never becomes V2. A
repaint that ignores this ends in a 400 (error 50035), so the card follows
the message:

* a V2 message gets the card in its V2 form (the feature's own layout);
* a classic message gets the card in its classic form;
* a card that says nothing in the message's form skips the repaint (the
  old content stays: an empty box is worse than no edit).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord

import rosemary.cogs.starboard  # noqa: F401  (registers the starboard builder)
import rosemary.cogs.suggestions  # noqa: F401  (registers the suggestion builder)
import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.cogs.suggestions import SuggestionsCog
from rosemary.core.card_service import (
    align_to_message,
    is_v2_message,
    is_v2_payload,
    render_card_message,
)
from rosemary.core.i18n import Translator
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore, preload_themes
from rosemary.ui.theme import load_theme

LANG_DIR = Path(__file__).resolve().parents[1] / "language"

#: Discord's IS_COMPONENTS_V2 message flag (1 << 15).
V2_FLAG = 1 << 15

STARBOARD_VARIABLES = {
    "user": "<@7>",
    "user_name": "Author",
    "user_avatar": "https://cdn.discordapp.com/embed/avatars/0.png",
    "stars": 3,
    "title": "⭐ 3 · #geral",
    "body": "miau",
    "image_url": "",
    "message_link": "https://discord.com/jump",
    "timestamp": "<t:1:R>",
}

#: The boost preview (a card that ships as a Components V2).
PREVIEW_VARIABLES = {
    "role_name": "VIP",
    "owner": "<@7>",
    "count": 3,
    "max_members": 10,
}

#: The partnerships audit (a staff card that ships as a classic embed).
AUDIT_VARIABLES = {"title": "Audit", "body": "B"}


def _resolver(language: str):
    async def resolve(guild_id):
        return language

    return resolve


class FakeBot:
    def __init__(self, tmp_path, language="en-US", themes_yaml: str | None = None):
        self.storage = GuildStorage(tmp_path)
        self.theme = load_theme()
        self.translator = Translator(
            LANG_DIR,
            resolver=_resolver(language),
            default_placeholders=self.theme.emojis,
        )
        self.guilds = []
        self.views = []
        self._cog = None
        self._guild = None
        self._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
        (tmp_path / "themes").mkdir(exist_ok=True)
        if themes_yaml is not None:
            (tmp_path / "themes" / "t.yaml").write_text(themes_yaml, encoding="utf-8")
            self.guilds = [type("G", (), {"id": 1})()]

    def add_view(self, view):
        self.views.append(view)

    def get_cog(self, name):
        return self._cog if name == "SuggestionsCog" else None

    def get_guild(self, guild_id):
        return self._guild


async def _activate(bot) -> None:
    await bot._theme_store.set_active(1, "t")
    await preload_themes(bot)


def _message(*, v2: bool, message_id: int = 111):
    """A live message fake: ``v2`` decides the immutable flag."""
    message = MagicMock()
    message.id = message_id
    message.jump_url = f"https://discord.com/channels/1/42/{message_id}"
    message.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    message.edit = AsyncMock()
    message.attachments = []
    message.flags = discord.MessageFlags(is_components_v2=v2)
    return message


def _guild():
    member = MagicMock()
    member.id = 7
    member.mention = "<@7>"
    member.display_name = "Author"
    member.display_avatar.url = "https://cdn.discordapp.com/embed/avatars/0.png"
    member.send = AsyncMock()
    channel = AsyncMock(spec=discord.TextChannel)
    channel.id = 42
    channel.mention = "<#42>"
    sent = AsyncMock()
    sent.id = 111
    sent.jump_url = "https://discord.com/channels/1/42/111"
    sent.attachments = []
    channel.send.return_value = sent
    guild = MagicMock()
    guild.id = 1
    guild.get_channel.return_value = channel
    guild.get_member.return_value = member
    return guild, channel, member, sent


#: the flag -------------------------------------------------------------------


def test_message_flag_decides_the_shape():
    """The immutable flag is what tells a V2 message from a classic one."""
    assert _message(v2=True).flags.value == V2_FLAG
    assert is_v2_message(_message(v2=True)) is True
    assert is_v2_message(_message(v2=False)) is False
    assert is_v2_message(object()) is False


#: the alignment --------------------------------------------------------------


async def test_v2_message_repaints_with_the_v2_card(tmp_path):
    """A legacy V2 message gets the card in V2: no embed, no content."""
    bot = FakeBot(tmp_path)
    payload, allowed = await render_card_message(
        bot, 1, "boost.preview", PREVIEW_VARIABLES
    )
    assert payload is not None and is_v2_payload(payload)

    aligned, _allowed = await align_to_message(
        bot,
        1,
        "boost.preview",
        PREVIEW_VARIABLES,
        payload,
        allowed,
        _message(v2=True),
    )
    assert aligned is not None
    assert aligned.embed is None
    assert aligned.content is None
    assert is_v2_payload(aligned)
    kwargs = aligned.message_kwargs()
    assert "embed" not in kwargs
    assert "content" not in kwargs


async def test_classic_message_keeps_the_embed_card(tmp_path):
    """The classic message keeps the classic card: nothing changes shape."""
    bot = FakeBot(tmp_path)
    payload, allowed = await render_card_message(
        bot, 1, "partnerships.audit", AUDIT_VARIABLES
    )
    assert payload is not None and payload.embed is not None
    aligned, _allowed = await align_to_message(
        bot,
        1,
        "partnerships.audit",
        AUDIT_VARIABLES,
        payload,
        allowed,
        _message(v2=False),
    )
    assert aligned is payload
    assert aligned.embed is not None
    assert aligned.message_kwargs()["embed"] is not None


async def test_classic_message_gets_the_v2_card_in_its_classic_form(tmp_path):
    """A card that changed shape keeps the shape of the message.

    The flag is immutable, so a classic message never becomes V2: the card
    is drawn again in the classic form and the message stays up to date.
    """
    bot = FakeBot(tmp_path)
    payload, allowed = await render_card_message(
        bot, 1, "boost.preview", PREVIEW_VARIABLES
    )
    assert payload is not None and is_v2_payload(payload)

    aligned, _allowed = await align_to_message(
        bot,
        1,
        "boost.preview",
        PREVIEW_VARIABLES,
        payload,
        allowed,
        _message(v2=False),
    )
    assert aligned is not None
    assert aligned.embed is not None
    assert not is_v2_payload(aligned)
    assert aligned.message_kwargs()["embed"] is not None


async def test_classic_message_skips_a_card_without_a_classic_form(tmp_path):
    """A card that says nothing in the message's form: the repaint is skipped.

    The old content stays: an edit that sends an empty box is worse than an
    edit that sends nothing.
    """
    bot = FakeBot(tmp_path)
    variables = {"role_name": "VIP", "owner": "<@7>"}
    payload, allowed = await render_card_message(bot, 1, "no.such.card", variables)
    assert payload is None

    aligned, _allowed = await align_to_message(
        bot,
        1,
        "no.such.card",
        variables,
        payload,
        allowed,
        _message(v2=False),
    )
    assert aligned is None


async def test_themed_embed_falls_back_to_the_default_v2_card(tmp_path):
    """A theme that writes the card as an embed cannot repaint a V2 message:
    the feature's own V2 builder answers instead."""
    bot = FakeBot(
        tmp_path,
        themes_yaml=(
            "name: t\n"
            "cards:\n"
            "  starboard.card:\n"
            "    embed:\n"
            '      title: "Mural"\n'
            '      description: "{body}"\n'
        ),
    )
    await _activate(bot)
    payload, allowed = await render_card_message(
        bot, 1, "starboard.card", STARBOARD_VARIABLES
    )
    assert payload is not None and payload.embed is not None

    aligned, _allowed = await align_to_message(
        bot,
        1,
        "starboard.card",
        STARBOARD_VARIABLES,
        payload,
        allowed,
        _message(v2=True),
    )
    assert aligned is not None
    assert aligned.embed is None
    assert is_v2_payload(aligned)


#: the vote regression --------------------------------------------------------


async def test_vote_on_a_legacy_suggestion_keeps_the_v2_shape(tmp_path, monkeypatch):
    """The suggestion posted before the refactor is a V2 message: voting on it
    must not send an embed (the 400 that broke the vote buttons)."""
    import rosemary.cogs.suggestions as suggestions_module

    monkeypatch.setattr(suggestions_module, "EDIT_COOLDOWN_SECONDS", 0)
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, member, _sent = _guild()
    await cog.post_suggestion(guild, channel, member, "vote on me")

    message = _message(v2=True)
    assert await cog.apply_vote(guild, message, 9, "up") is True

    message.edit.assert_awaited_once()
    kwargs = message.edit.await_args.kwargs
    assert "embed" not in kwargs
    assert "content" not in kwargs
    assert kwargs["view"].is_components_v2() is True


#: the other repaint sites ----------------------------------------------------


async def test_starboard_repaint_of_a_legacy_v2_post(tmp_path):
    """The mural post posted before the refactor is V2: the repaint follows."""
    from rosemary.cogs.starboard import StarboardCog
    from rosemary.core.settings import set_setting

    bot = FakeBot(tmp_path)
    board = AsyncMock(spec=discord.TextChannel)
    post = _message(v2=True, message_id=999)
    board.fetch_message.return_value = post

    channel = AsyncMock(spec=discord.TextChannel)
    channel.id = 5
    channel.guild = MagicMock(spec=discord.Guild)
    channel.guild.id = 1
    channel.guild.get_channel.return_value = board

    message = MagicMock(spec=discord.Message)
    message.id = 10
    message.author.id = 7
    message.author.mention = "<@7>"
    message.author.display_name = "Author"
    message.author.display_avatar.url = "https://a.b/x.png"
    message.content = "hello"
    message.attachments = []
    message.channel = channel
    message.jump_url = "https://discord.com/jump"
    message.created_at = datetime(2026, 1, 1, tzinfo=UTC)

    cog = StarboardCog(bot)
    await set_setting(bot.storage, 1, "starboard.enabled", True)
    await set_setting(bot.storage, 1, "starboard.channel", 42)
    await set_setting(bot.storage, 1, "starboard.threshold", 3)
    await cog.store.upsert(1, 10, post_id=999, stars=3, channel_id=5, author_id=7)

    await cog._update_starboard(channel, message, 4)

    post.edit.assert_awaited_once()
    kwargs = post.edit.await_args.kwargs
    assert "embed" not in kwargs
    assert kwargs["view"].is_components_v2() is True


async def test_trap_notice_repaint_of_a_legacy_v2_message(tmp_path):
    """The notice posted before the refactor is V2: the repaint follows."""
    from rosemary.cogs.trap import TrapCog
    from rosemary.core.settings import set_setting

    bot = FakeBot(tmp_path)
    channel = AsyncMock(spec=discord.TextChannel)
    channel.id = 77
    notice = _message(v2=True, message_id=555)
    channel.fetch_message.return_value = notice

    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.name = "Server"
    guild.get_channel.return_value = channel
    bot._guild = guild

    cog = TrapCog(bot)
    await set_setting(bot.storage, 1, "trap.enabled", True)
    await set_setting(bot.storage, 1, "trap.channel", 77)
    await cog.store.set_notice(1, 555)

    await cog.repaint_notice(bot, 1)

    notice.edit.assert_awaited_once()
    kwargs = notice.edit.await_args.kwargs
    assert "embed" not in kwargs
    assert kwargs["view"].is_components_v2() is True
