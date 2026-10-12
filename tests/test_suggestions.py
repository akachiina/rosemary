"""Suggestions: per-guild counter, toggle voting, card and command shape."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import yaml

import rosemary.cogs.suggestions  # noqa: F401  (registers the builder)
import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.bot import _COMMAND_NAME_RE
from rosemary.cogs.suggestions import (
    SUGGEST_MAX_LENGTH,
    SuggestionsCog,
    SuggestionVoteView,
    _safe_filename,
    _score_color,
    default_suggestion_document,
    default_suggestion_vote_dm_document,
)
from rosemary.core.cards import get_card
from rosemary.core.i18n import Translator
from rosemary.core.storage import GuildStorage
from rosemary.core.suggestions import SuggestionStore
from rosemary.core.variables import VARIABLES, samples_for
from rosemary.ui.theme import load_theme

LANG_DIR = Path(__file__).resolve().parents[1] / "language"


def _resolver(language: str):
    async def resolve(guild_id):
        return language

    return resolve


class FakeBot:
    def __init__(self, tmp_path, language="en-US"):
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

    def add_view(self, view):
        self.views.append(view)

    def get_cog(self, name):
        return self._cog if name == "SuggestionsCog" else None


def make_guild(member_id=7):
    member = MagicMock()
    member.id = member_id
    member.mention = f"<@{member_id}>"
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


def make_message(message_id=111):
    message = MagicMock()
    message.id = message_id
    message.jump_url = f"https://discord.com/channels/1/42/{message_id}"
    message.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    message.edit = AsyncMock()
    return message


#: store ----------------------------------------------------------------------


async def test_counter_increments_per_guild(tmp_path):
    store = SuggestionStore(tmp_path)
    assert await store.next_number(1) == 1
    assert await store.next_number(1) == 2
    assert await store.next_number(2) == 1


async def test_put_get_remove_roundtrip(tmp_path):
    store = SuggestionStore(tmp_path)
    assert await store.get(1, 10) is None
    await store.put(1, 10, number=3, author_id=7, channel_id=5, body="hi")
    entry = await store.get(1, 10)
    assert entry["number"] == 3
    assert entry["author_id"] == 7
    assert entry["body"] == "hi"
    assert SuggestionStore.counts(entry) == (0, 0)
    await store.remove(1, 10)
    assert await store.get(1, 10) is None


async def test_toggle_vote_add_remove_switch(tmp_path):
    store = SuggestionStore(tmp_path)
    await store.put(1, 10, number=1, author_id=7, channel_id=5)
    entry = await store.toggle_vote(1, 10, 9, "up")
    assert SuggestionStore.counts(entry) == (1, 0)
    entry = await store.toggle_vote(1, 10, 9, "up")
    assert SuggestionStore.counts(entry) == (0, 0)
    entry = await store.toggle_vote(1, 10, 9, "down")
    assert SuggestionStore.counts(entry) == (0, 1)
    entry = await store.toggle_vote(1, 10, 9, "up")
    assert SuggestionStore.counts(entry) == (1, 0)
    assert await store.toggle_vote(1, 99, 9, "up") is None


async def test_toggle_vote_rejects_unknown_side(tmp_path):
    store = SuggestionStore(tmp_path)
    await store.put(1, 10, number=1, author_id=7, channel_id=5)
    with pytest.raises(ValueError):
        await store.toggle_vote(1, 10, 9, "sideways")


#: command shape --------------------------------------------------------------


def test_command_names_are_valid():
    assert _COMMAND_NAME_RE.match("suggest") and "suggest".lower() == "suggest"
    for code in ("en-US", "pt-BR"):
        with (LANG_DIR / f"{code}.yaml").open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        name = data["suggest"]["command"]["name"]
        assert _COMMAND_NAME_RE.match(name) and name == name.lower(), f"{code}:{name!r}"
        assert isinstance(data["suggest"]["command"]["description"], str)


def test_suggest_modal_strings_present():
    for code in ("en-US", "pt-BR"):
        with (LANG_DIR / f"{code}.yaml").open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        modal = data["suggest"]["modal"]
        assert len(modal["title"]) <= 45, f"{code}: modal title over Discord cap"
        assert modal["label"] and modal["placeholder"]
        assert SUGGEST_MAX_LENGTH == 1000


#: card contract --------------------------------------------------------------


def test_card_spec_registered_with_exact_contract():
    spec = get_card("suggestions.card")
    assert spec is not None
    assert spec.variables == (
        "user", "user_name", "user_avatar", "suggestion_number", "title", "body",
        "image_url", "upvotes", "downvotes", "score", "message_link", "timestamp",
    )
    for name in spec.variables:
        assert name in VARIABLES, f"unregistered variable {name}"


def test_new_variables_have_labels_in_both_catalogs():
    for code in ("en-US", "pt-BR"):
        with (LANG_DIR / f"{code}.yaml").open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        for name in ("suggestion_number", "upvotes", "downvotes", "score"):
            entry = (data.get("variables") or {}).get(name) or {}
            assert isinstance(entry.get("label"), str), f"{code}:{name}.label"
            assert isinstance(entry.get("description"), str), f"{code}:{name}.description"


def test_builder_signature_accepts_variables():
    params = inspect.signature(default_suggestion_document).parameters
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    dm_params = inspect.signature(default_suggestion_vote_dm_document).parameters
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in dm_params.values())


def test_score_color_branches():
    assert _score_color(3) == "gold"
    assert _score_color(-2) == "danger"
    assert _score_color(0) == "info"


async def test_default_document_colors_and_placeholders(tmp_path):
    from rosemary.core.card_service import default_document

    bot = FakeBot(tmp_path)
    variables = samples_for(get_card("suggestions.card").variables)
    for score, color in ((5, "gold"), (-1, "danger"), (0, "info")):
        doc = await default_document(
            bot, 1, "suggestions.card", {**variables, "score": score}
        )
        assert doc is not None
        assert doc["blocks"][0]["color"] == color


async def test_default_document_renders_title_body_counts(tmp_path):
    from rosemary.core.card_service import render_document

    bot = FakeBot(tmp_path)
    variables = {
        "user": "<@7>",
        "user_name": "Author",
        "user_avatar": "https://cdn.discordapp.com/embed/avatars/0.png",
        "suggestion_number": 12,
        "title": "Suggestion #12",
        "body": "add more emojis",
        "upvotes": 4,
        "downvotes": 1,
        "score": 3,
        "message_link": "https://discord.com/channels/1/42/111",
        "timestamp": "<t:1111111111:R>",
    }
    doc = await default_suggestion_document(bot, 1, **variables)
    view = await render_document(bot, doc, variables, guild_id=1, card_key="suggestions.card")
    texts = [
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]
    assert any("Suggestion #12" in text for text in texts), texts
    assert any("add more emojis" in text for text in texts), texts
    assert any("4" in text and "1" in text for text in texts), texts


#: vote view ------------------------------------------------------------------


async def test_vote_row_has_send_and_vote_buttons(tmp_path):
    bot = FakeBot(tmp_path)
    view = SuggestionVoteView(bot, 1, upvotes=2, downvotes=3)
    view.add_item(await view.build_row())
    buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
    assert [(b.custom_id, b.label) for b in buttons] == [
        ("suggest_up", "2"),
        ("suggest_down", "3"),
        ("suggest_new", "Send suggestion"),
    ]
    assert all(b.callback is not None for b in buttons)
    assert {str(b.emoji) for b in buttons} == {"⬆️", "⬇️", "✏️"}


async def test_vote_row_new_button_is_translated(tmp_path):
    bot = FakeBot(tmp_path, "pt-BR")
    view = SuggestionVoteView(bot, 1)
    view.add_item(await view.build_row())
    labels = [
        b.label for b in view.walk_children() if isinstance(b, discord.ui.Button)
    ]
    assert "Enviar sugestão" in labels


async def test_start_registers_single_dispatcher(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    await cog.start()
    await cog.start()
    assert len(bot.views) == 1


#: flows ----------------------------------------------------------------------


async def test_post_records_entry_and_sends(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, member, sent = make_guild()
    await cog.post_suggestion(guild, channel, member, "more emojis please")
    channel.send.assert_awaited_once()
    entry = await cog.store.get(1, 111)
    assert entry["number"] == 1
    assert entry["author_id"] == 7
    assert entry["body"] == "more emojis please"


async def test_post_second_message_increments_counter(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, member, _sent = make_guild()
    second = AsyncMock()
    second.id = 222
    second.jump_url = "https://discord.com/channels/1/42/222"
    second.attachments = []
    channel.send.return_value = second
    await cog.post_suggestion(guild, channel, member, "first")
    await cog.post_suggestion(guild, channel, member, "second")
    assert (await cog.store.get(1, 222))["number"] == 2


async def test_apply_vote_toggles_and_repaints(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, member, _sent = make_guild()
    await cog.post_suggestion(guild, channel, member, "vote on me")
    message = make_message()
    assert await cog.apply_vote(guild, message, 9, "up") is True
    assert SuggestionStore.counts(await cog.store.get(1, 111)) == (1, 0)
    message.edit.assert_awaited()
    assert await cog.apply_vote(guild, message, 9, "up") is True
    assert SuggestionStore.counts(await cog.store.get(1, 111)) == (0, 0)
    assert await cog.apply_vote(guild, message, 9, "down") is True
    assert SuggestionStore.counts(await cog.store.get(1, 111)) == (0, 1)


async def test_apply_vote_unknown_message_returns_false(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, _channel, _member, _sent = make_guild()
    assert await cog.apply_vote(guild, make_message(999), 9, "up") is False


async def test_deleted_message_clears_entry(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, member, _sent = make_guild()
    await cog.post_suggestion(guild, channel, member, "temporary")
    payload = MagicMock()
    payload.guild_id = 1
    payload.message_id = 111
    await cog.on_raw_message_delete(payload)
    assert await cog.store.get(1, 111) is None


#: footer hint --------------------------------------------------------------


async def test_default_document_includes_hint(tmp_path):
    from rosemary.core.card_service import render_document

    for language, expected in (
        ("en-US", "Use `/suggest` or the button below to send your suggestion!"),
        ("pt-BR", "Use `/sugestao` ou o botão abaixo para enviar a sua sugestão!"),
    ):
        bot = FakeBot(tmp_path, language)
        variables = {
            "user": "<@7>",
            "user_name": "Author",
            "user_avatar": "https://cdn.discordapp.com/embed/avatars/0.png",
            "suggestion_number": 1,
            "title": "Suggestion #1",
            "body": "more emojis please",
            "upvotes": 0,
            "downvotes": 0,
            "score": 0,
            "message_link": "https://discord.com/channels/1/42/111",
            "timestamp": "<t:1111111111:R>",
        }
        doc = await default_suggestion_document(bot, 1, **variables)
        view = await render_document(
            bot, doc, variables, guild_id=1, card_key="suggestions.card"
        )
        texts = [
            item.content
            for item in view.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        ]
        assert any(expected in text for text in texts), (language, texts)


#: vote DM ------------------------------------------------------------------


def test_dm_vote_spec_registered_with_exact_contract():
    spec = get_card("suggestions.dm.vote")
    assert spec is not None
    assert spec.variables == (
        "voter", "suggestion_number", "body", "message_link", "vote",
    )
    for name in spec.variables:
        assert name in VARIABLES, f"unregistered variable {name}"


def test_dm_vote_variables_have_labels_in_both_catalogs():
    for code in ("en-US", "pt-BR"):
        with (LANG_DIR / f"{code}.yaml").open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        for name in ("voter", "vote"):
            entry = (data.get("variables") or {}).get(name) or {}
            assert isinstance(entry.get("label"), str), f"{code}:{name}.label"
            assert isinstance(entry.get("description"), str), f"{code}:{name}.description"


def _dm_texts_and_urls(view):
    texts = [
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]
    urls = [
        item.url
        for item in view.walk_children()
        if isinstance(item, discord.ui.Button)
        and item.style is discord.ButtonStyle.link
    ]
    return texts, urls


async def test_vote_sends_dm_to_owner(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, member, _sent = make_guild()
    await cog.post_suggestion(guild, channel, member, "vote on me")
    message = make_message()
    assert await cog.apply_vote(guild, message, 9, "up") is True
    member.send.assert_awaited_once()
    view = member.send.await_args.kwargs["view"]
    texts, urls = _dm_texts_and_urls(view)
    assert any("upvote" in text for text in texts), texts
    assert any("vote on me" in text for text in texts), texts
    assert any("#1" in text for text in texts), texts
    assert message.jump_url in urls, urls


async def test_vote_down_dm_uses_downvote_word(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, member, _sent = make_guild()
    await cog.post_suggestion(guild, channel, member, "vote on me")
    assert await cog.apply_vote(guild, make_message(), 9, "down") is True
    view = member.send.await_args.kwargs["view"]
    texts, _urls = _dm_texts_and_urls(view)
    assert any("downvote" in text for text in texts), texts


async def test_no_dm_on_self_vote(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, member, _sent = make_guild()
    await cog.post_suggestion(guild, channel, member, "mine")
    assert await cog.apply_vote(guild, make_message(), 7, "up") is True
    member.send.assert_not_awaited()


async def test_no_dm_when_owner_left(tmp_path):
    bot = FakeBot(tmp_path)
    bot.fetch_user = AsyncMock(return_value=None)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, member, _sent = make_guild()
    guild.get_member.side_effect = lambda uid: None if uid == 7 else member
    await cog.post_suggestion(guild, channel, member, "orphan")
    assert await cog.apply_vote(guild, make_message(), 9, "up") is True
    member.send.assert_not_awaited()


#: send button --------------------------------------------------------------


def make_click(guild, user_id=9, message=None):
    interaction = MagicMock()
    interaction.guild = guild
    interaction.user.id = user_id
    interaction.message = message
    interaction.response = AsyncMock()
    interaction.response.is_done.return_value = False
    interaction.followup = AsyncMock()
    return interaction


async def _enable_suggestions(bot, guild_id=1, channel_id=42):
    from rosemary.core.settings import set_setting

    await set_setting(bot.storage, guild_id, "suggestions.enabled", True)
    await set_setting(bot.storage, guild_id, "suggestions.channel", channel_id)


async def test_send_button_opens_modal(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, _channel, _member, _sent = make_guild()
    await _enable_suggestions(bot)
    view = SuggestionVoteView(bot, 1)
    interaction = make_click(guild)
    await view._new(interaction)
    interaction.response.send_modal.assert_awaited_once()
    modal = interaction.response.send_modal.await_args.args[0]
    assert modal is not None


async def test_send_button_refuses_when_disabled(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, _channel, _member, _sent = make_guild()
    view = SuggestionVoteView(bot, 1)
    interaction = make_click(guild)
    await view._new(interaction)
    interaction.response.send_modal.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()


async def test_modal_submit_posts_suggestion(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, member, _sent = make_guild()
    await _enable_suggestions(bot)
    interaction = make_click(guild, user_id=7)
    await cog._modal_submit(interaction, "  fresh idea  ", [])
    channel.send.assert_awaited_once()
    entry = await cog.store.get(1, 111)
    assert entry["body"] == "fresh idea"
    assert entry["image_url"] == ""
    interaction.followup.send.assert_awaited_once()


async def test_modal_submit_rejects_empty(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, _member, _sent = make_guild()
    await _enable_suggestions(bot)
    interaction = make_click(guild, user_id=7)
    await cog._modal_submit(interaction, "   ", [])
    channel.send.assert_not_awaited()


#: images -------------------------------------------------------------------


def make_upload(
    *,
    content_type="image/png",
    size=100,
    filename="cat.png",
    data=b"img-bytes",
):
    upload = MagicMock()
    upload.content_type = content_type
    upload.size = size
    upload.filename = filename
    upload.read = AsyncMock(return_value=data)
    return upload


def _card_variables(**overrides):
    variables = {
        "user": "<@7>",
        "user_name": "Author",
        "user_avatar": "https://cdn.discordapp.com/embed/avatars/0.png",
        "suggestion_number": 1,
        "title": "Suggestion #1",
        "body": "more emojis please",
        "image_url": "",
        "upvotes": 0,
        "downvotes": 0,
        "score": 0,
        "message_link": "https://discord.com/channels/1/42/111",
        "timestamp": "<t:1111111111:R>",
    }
    variables.update(overrides)
    return variables


def _has_gallery(view) -> bool:
    return any(
        isinstance(item, discord.ui.MediaGallery) for item in view.walk_children()
    )


def test_safe_filename_strips_spaces():
    assert _safe_filename("my cat (1).png") == "my_cat_1_.png"
    assert _safe_filename("") == "suggestion.png"


async def test_gallery_renders_only_with_image(tmp_path):
    from rosemary.core.card_service import render_document

    bot = FakeBot(tmp_path)
    doc = await default_suggestion_document(bot, 1, **_card_variables())
    view = await render_document(
        bot, doc, _card_variables(), guild_id=1, card_key="suggestions.card"
    )
    assert not _has_gallery(view)
    with_image = _card_variables(image_url="https://cdn.example.com/cat.png")
    doc = await default_suggestion_document(bot, 1, **with_image)
    view = await render_document(
        bot, doc, with_image, guild_id=1, card_key="suggestions.card"
    )
    assert _has_gallery(view)


async def test_modal_submit_with_image_attaches_and_stores_url(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, _member, sent = make_guild()
    await _enable_suggestions(bot)
    cdn = MagicMock()
    cdn.url = "https://cdn.discordapp.com/attachments/1/42/cat.png"
    sent.attachments = [cdn]
    interaction = make_click(guild, user_id=7)
    await cog._modal_submit(interaction, "look at this", [make_upload()])
    channel.send.assert_awaited_once()
    files = channel.send.await_args.kwargs.get("files") or []
    assert len(files) == 1
    assert files[0].filename == "cat.png"
    entry = await cog.store.get(1, 111)
    assert entry["image_url"] == "https://cdn.discordapp.com/attachments/1/42/cat.png"
    sent.edit.assert_awaited_once()


async def test_modal_submit_rejects_non_image(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, _member, _sent = make_guild()
    await _enable_suggestions(bot)
    interaction = make_click(guild, user_id=7)
    upload = make_upload(content_type="application/pdf", filename="doc.pdf")
    await cog._modal_submit(interaction, "read this", [upload])
    channel.send.assert_not_awaited()
    message = interaction.followup.send.await_args.args[0]
    assert "image file" in message


async def test_modal_submit_rejects_huge_image(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, _member, _sent = make_guild()
    await _enable_suggestions(bot)
    interaction = make_click(guild, user_id=7)
    await cog._modal_submit(interaction, "big one", [make_upload(size=9 * 1024 * 1024)])
    channel.send.assert_not_awaited()
    message = interaction.followup.send.await_args.args[0]
    assert "8 MB" in message


async def test_vote_repaint_keeps_image(tmp_path):
    bot = FakeBot(tmp_path)
    cog = SuggestionsCog(bot)
    bot._cog = cog
    guild, channel, _member, sent = make_guild()
    await _enable_suggestions(bot)
    cdn = MagicMock()
    cdn.url = "https://cdn.discordapp.com/attachments/1/42/cat.png"
    sent.attachments = [cdn]
    poster = make_click(guild, user_id=7)
    await cog._modal_submit(poster, "with pic", [make_upload()])
    message = make_message()
    assert await cog.apply_vote(guild, message, 9, "up") is True
    view = message.edit.await_args.kwargs["view"]
    assert _has_gallery(view)
