"""Confessions: store, review gate, threaded replies and bans."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from card_forms import (
    payload_accent,
    payload_footer_lines,
    payload_image,
    payload_texts,
    payload_thumbnail,
    sent,
)
from catalog import load_catalog

import rosemary.cogs.confessions  # noqa: F401  (registers the commands)
import rosemary.core.card_specs  # noqa: F401  (fills the card registry)
from rosemary.bot import _COMMAND_NAME_RE
from rosemary.cogs.confessions import (
    CONFESS_MAX_LENGTH,
    ConfessionReviewView,
    ConfessionsCog,
    ConfessionView,
)
from rosemary.core.cards import get_card
from rosemary.core.i18n import Translator
from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme

LANG_DIR = Path(__file__).resolve().parents[1] / "language"


def _resolver(language: str):
    async def resolve(guild_id):
        return language

    return resolve


class FakeAssets:
    """The asset registry (the application emojis) with one known emoji."""

    def __init__(self, mention: str = "<:op_emoji:123>") -> None:
        self._mention = mention

    def mention(self, key: str, *, guild_id: int | None = None) -> str:
        return self._mention if key == "emojis/op_emoji" else ""


class FakeBot:
    def __init__(self, tmp_path, language="en-US"):
        self.storage = GuildStorage(tmp_path)
        self.theme = load_theme()
        self.translator = Translator(
            LANG_DIR,
            resolver=_resolver(language),
            default_placeholders=self.theme.emojis,
        )
        self.assets = FakeAssets()
        self.guilds = []
        self.views = []
        self._cog = None

    def add_view(self, view):
        self.views.append(view)

    def get_cog(self, name):
        return self._cog if name == "ConfessionsCog" else None

    def get_guild(self, guild_id):
        return None


def make_member(user_id=7, *, staff=True):
    member = MagicMock()
    member.id = user_id
    member.mention = f"<@{user_id}>"
    member.display_name = f"User{user_id}"
    member.display_avatar.url = "https://cdn.discordapp.com/embed/avatars/0.png"
    member.send = AsyncMock()
    member.guild_permissions.manage_messages = staff
    return member


def make_cguild(author_id=7):
    author = make_member(author_id)
    moderator = make_member(70)
    public = AsyncMock(spec=discord.TextChannel)
    public.id = 42
    public.mention = "<#42>"
    review = AsyncMock(spec=discord.TextChannel)
    review.id = 43
    review_partial = MagicMock()
    review_partial.delete = AsyncMock()
    review.get_partial_message.return_value = review_partial
    sent_public = AsyncMock()
    sent_public.id = 111
    sent_public.jump_url = "https://discord.com/channels/1/42/111"
    sent_public.attachments = []
    sent_review = AsyncMock()
    sent_review.id = 555
    sent_review.jump_url = "https://discord.com/channels/1/43/555"
    sent_review.attachments = []
    public.send.return_value = sent_public
    review.send.return_value = sent_review
    members = {author.id: author, moderator.id: moderator}
    guild = MagicMock()
    guild.id = 1
    guild.get_channel.side_effect = lambda cid: {42: public, 43: review}.get(cid)
    guild.get_member.side_effect = lambda uid: members.get(uid)
    guild.get_thread.return_value = None
    return guild, public, review, author, moderator, sent_public, sent_review


def make_thread(first_reply_id=901):
    thread = AsyncMock()
    thread.id = 777
    reply = AsyncMock()
    reply.id = first_reply_id
    reply.jump_url = f"https://discord.com/channels/1/42/{first_reply_id}"
    reply.attachments = []
    thread.send.return_value = reply
    return thread, reply


def make_click(guild, user_id=9, message=None):
    interaction = MagicMock()
    interaction.guild = guild
    interaction.user.id = user_id
    interaction.message = message
    interaction.response = AsyncMock()
    # ``is_done`` is synchronous in discord.py: an AsyncMock child would
    # return a coroutine (always truthy) and skip every guard.
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.followup = AsyncMock()
    return interaction


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


async def _enable_confessions(bot, *, review=False, reveal=False, mark_op=True, sealed=False):
    from rosemary.core.settings import set_setting

    await set_setting(bot.storage, 1, "confessions.enabled", True)
    await set_setting(bot.storage, 1, "confessions.public_channel", 42)
    await set_setting(bot.storage, 1, "confessions.review_channel", 43)
    await set_setting(bot.storage, 1, "confessions.review_required", review)
    await set_setting(bot.storage, 1, "confessions.reveal_author", reveal)
    await set_setting(bot.storage, 1, "confessions.mark_op", mark_op)
    await set_setting(bot.storage, 1, "confessions.sealed_author", sealed)


def _texts(view):
    return [
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]


def _call_texts(call) -> list[str]:
    """The texts of a sent card, whatever form it took (V2, embed, text)."""
    kwargs = call.kwargs
    texts: list[str] = []
    view = kwargs.get("view")
    if view is not None:
        texts.extend(_texts(view))
    embed = kwargs.get("embed")
    if embed is not None:
        texts.append(embed.title or "")
        texts.append(embed.description or "")
        for field in embed.fields:
            texts.append(field.name or "")
            texts.append(field.value or "")
        if embed.footer and embed.footer.text:
            texts.append(embed.footer.text)
    content = kwargs.get("content")
    if isinstance(content, str) and content:
        texts.append(content)
    return [text for text in texts if text]


def _sent_texts(send_mock) -> list[str]:
    """The texts of the last card sent through ``send_mock``."""
    return _call_texts(send_mock.await_args)


def _sent_embed(send_mock):
    """The embed of a sent card (``None`` when the card was not an embed)."""
    return send_mock.await_args.kwargs.get("embed")


#: store ----------------------------------------------------------------------


async def test_store_counter_put_get(tmp_path):
    from rosemary.core.confessions import ConfessionStore as Store

    store = Store(tmp_path)
    assert await store.next_number(1) == 1
    assert await store.next_number(1) == 2
    assert await store.next_number(2) == 1
    await store.put(1, 10, number=1, author_id=7, channel_id=5, body="hi")
    entry = await store.get(1, 10)
    assert entry is not None and entry["status"] == "open"
    assert entry["reply_count"] == 0
    assert await store.get(1, 99) is None
    assert await store.get_by_number(1, 1) == (10, entry)
    assert await store.get_by_number(1, 9) is None
    with pytest.raises(ValueError):
        await store.put(1, 11, number=2, author_id=7, channel_id=5, status="nope")


async def test_store_status_thread_replies(tmp_path):
    from rosemary.core.confessions import ConfessionStore as Store

    store = Store(tmp_path)
    await store.put(1, 10, number=1, author_id=7, channel_id=5, status="pending")
    assert (await store.set_status(1, 10, "denied"))["status"] == "denied"
    assert await store.set_status(1, 99, "open") is None
    assert (await store.set_thread(1, 10, 777))["thread_id"] == 777
    assert await store.set_thread(1, 99, 1) is None
    first = await store.add_reply(
        1, 10, 901, parent_id=None, parent_index=0, author_id=9, body="r1"
    )
    assert first is not None and first[0] == 1
    second = await store.add_reply(
        1, 10, 902, parent_id=901, parent_index=1, author_id=10, body="r2"
    )
    assert second is not None and second[0] == 2
    reply = await store.get_reply(1, 902)
    assert reply is not None and reply["parent_index"] == 1
    assert reply["author_id"] == 10
    assert await store.add_reply(
        1, 99, 903, parent_id=None, parent_index=0, author_id=9, body="x"
    ) is None


async def test_store_remove_cascades_and_discard_is_single(tmp_path):
    from rosemary.core.confessions import ConfessionStore as Store

    store = Store(tmp_path)
    await store.put(1, 10, number=1, author_id=7, channel_id=5, status="open")
    await store.put(1, 11, number=1, author_id=7, channel_id=5, status="pending")
    await store.add_reply(
        1, 10, 901, parent_id=None, parent_index=0, author_id=9, body="r"
    )
    await store.remove(1, 10)
    assert await store.get(1, 10) is None
    assert await store.get_reply(1, 901) is None
    # The pending twin shares the number: a single discard keeps it.
    await store.put(1, 20, number=2, author_id=7, channel_id=5, status="pending")
    await store.put(1, 21, number=2, author_id=7, channel_id=5, status="open")
    await store.discard(1, 20)
    assert await store.get(1, 20) is None
    assert await store.get(1, 21) is not None


async def test_store_bans(tmp_path):
    from rosemary.core.confessions import ConfessionStore as Store

    store = Store(tmp_path)
    assert await store.is_banned(1, 7) is False
    assert await store.ban(1, 7) is True
    assert await store.ban(1, 7) is False
    assert await store.is_banned(1, 7) is True
    assert await store.banned_list(1) == [7]
    assert await store.unban(1, 7) is True
    assert await store.unban(1, 7) is False
    assert await store.ban(1, 0) is False


async def test_store_rekey(tmp_path):
    from rosemary.core.confessions import ConfessionStore as Store

    store = Store(tmp_path)
    await store.put(1, 0, number=1, author_id=7, channel_id=5, status="pending")
    assert await store.rekey(1, 0, 555) is True
    assert await store.get(1, 555) is not None
    assert await store.rekey(1, 0, 556) is False


#: command shape --------------------------------------------------------------


def test_command_names_are_valid():
    assert _COMMAND_NAME_RE.match("confess")
    for code in ("en-US", "pt-BR"):
        data = load_catalog(code)
        name = data["confess"]["command"]["name"]
        assert _COMMAND_NAME_RE.match(name) and name == name.lower(), f"{code}:{name!r}"
        for key in ("confess_send", "confess_ban", "confess_unban", "confess_banned"):
            sub = data[key]["command"]
            assert _COMMAND_NAME_RE.match(sub["name"]), f"{code}:{key}"
            assert sub["name"] == sub["name"].lower(), f"{code}:{key}"


def test_group_registers_six_subcommands():
    subs = {
        sub.name
        for attr in vars(ConfessionsCog).values()
        if isinstance(attr, discord.SlashCommandGroup)
        for sub in (getattr(attr, "subcommands", None) or [])
    }
    assert subs == {"send", "panel", "ban", "unban", "banned", "reply"}


def test_confess_modal_strings_present():
    for code in ("en-US", "pt-BR"):
        data = load_catalog(code)
        modal = data["confess"]["modal"]
        assert len(modal["title"]) <= 45, f"{code}: modal title over Discord cap"
        assert modal["label"] and modal["placeholder"]
        assert modal["image_label"] and modal["image_description"]
        assert CONFESS_MAX_LENGTH == 2000


#: card contracts -------------------------------------------------------------


def test_card_specs_registered_with_exact_contracts():
    """The family lives in the default package: the document declares the
    contract (the copy the send site delivers)."""
    from rosemary.core.theme_package import default_package

    package = default_package()
    for key in (
        "confessions.card",
        "confessions.response",
        "confessions.dm.approved",
        "confessions.dm.rejected",
        "confessions.dm.response",
    ):
        assert key in package.documents, key
        assert get_card(key) is not None, key

    # The review card lives in the default package: its document declares
    # the contract.
    assert get_card("confessions.review").variables == (
        "confession_number", "title", "body", "image_url", "author_line",
        "timestamp",
    )
    assert get_card("confessions.card").variables == (
        "confession_number", "title", "body", "image_url", "color_hex",
        "alias_name", "op_emoji", "title_emoji",
    )
    assert get_card("confessions.response").variables == (
        "confession_number", "reply_index", "title", "body", "color_hex",
        "image_url", "alias_name", "op_emoji", "title_emoji",
    )
    assert get_card("confessions.dm.approved").variables == (
        "confession_number", "body", "message_link",
    )
    assert get_card("confessions.dm.rejected").variables == (
        "confession_number", "body", "reason", "reason_line",
    )
    assert get_card("confessions.dm.response").variables == (
        "confession_number", "reply_index", "body", "message_link", "parent_line",
    )


#: flows ----------------------------------------------------------------------


async def test_post_direct_stores_random_color(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, author, _mod, sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    interaction = make_click(guild, user_id=7)
    await cog._confess_submit(interaction, "my secret", [])
    public.send.assert_awaited_once()
    entry = await cog.store.get(1, 111)
    assert entry is not None and entry["status"] == "open"
    assert entry["color_hex"].startswith("#") and len(entry["color_hex"]) == 7
    assert entry["title_emoji"]
    texts = _sent_texts(public.send)
    assert any("Confession #1" in text for text in texts), texts


async def test_post_goes_to_review_when_required(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, review, _author, _mod, _sent, sent_review = make_cguild()
    await _enable_confessions(bot, review=True)
    interaction = make_click(guild, user_id=7)
    await cog._confess_submit(interaction, "needs review", [])
    review.send.assert_awaited_once()
    public.send.assert_not_awaited()
    entry = await cog.store.get(1, 555)
    assert entry is not None and entry["status"] == "pending"


async def test_banned_author_cannot_submit(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog.store.ban(1, 7)
    interaction = make_click(guild, user_id=7)
    await cog._confess_submit(interaction, "blocked", [])
    public.send.assert_not_awaited()
    text = interaction.followup.send.await_args.args[0]
    assert "ban" in text.lower(), text


async def test_approve_posts_public_dm_and_cleans_review(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, review, author, moderator, _sent, sent_review = make_cguild()
    await _enable_confessions(bot, review=True)
    await cog._confess_submit(make_click(guild, user_id=7), "approve me", [])
    outcome, public_msg = await cog.approve_confession(guild, 555, moderator)
    assert outcome == "ok" and public_msg is not None
    public.send.assert_awaited_once()
    assert await cog.store.get(1, 555) is None
    entry = await cog.store.get(1, 111)
    assert entry is not None and entry["status"] == "open"
    assert entry["color_hex"].startswith("#")
    review.get_partial_message(555).delete.assert_awaited_once()
    author.send.assert_awaited_once()
    texts = _sent_texts(author.send)
    assert any("approved" in text for text in texts), texts


async def test_deny_keeps_record_and_dm_reason(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, author, moderator, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot, review=True)
    await cog._confess_submit(make_click(guild, user_id=7), "deny me", [])
    outcome, _msg = await cog.deny_confession(guild, 555, moderator, "spammy")
    assert outcome == "ok"
    assert (await cog.store.get(1, 555))["status"] == "denied"
    public.send.assert_not_awaited()
    texts = _sent_texts(author.send)
    assert any("spammy" in text for text in texts), texts


async def test_nested_replies_number_and_dm_both_authors(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog._confess_submit(make_click(guild, user_id=7), "discuss me", [])
    thread, first_reply = make_thread(901)
    guild.get_thread.return_value = thread
    await cog.store.set_thread(1, 111, 777)
    second = AsyncMock()
    second.id = 902
    second.jump_url = "https://discord.com/channels/1/42/902"
    second.attachments = []
    thread.send.side_effect = [first_reply, second]
    replier = make_member(9)
    guild.get_member.side_effect = lambda uid: {7: author, 9: replier}.get(uid)
    outcome, _msg = await cog.post_reply(guild, 111, 0, replier, "first!")
    assert outcome == "ok"
    outcome, _msg = await cog.post_reply(guild, 111, 901, author, "second!")
    assert outcome == "ok"
    reply = await cog.store.get_reply(1, 902)
    assert reply is not None and reply["index"] == 2
    assert reply["parent_index"] == 1
    texts = [
        text
        for call in thread.send.await_args_list
        for text in _call_texts(call)
    ]
    assert any("#1-2" in text for text in texts), texts
    assert any("#1-1" in text for text in texts), texts
    # The confession's author hears about the other reply but not about
    # their own one (a DM about your own comment is noise).
    assert author.send.await_count == 1
    assert replier.send.await_count == 1
    # The DM names the answer and the answer it answers.
    dm_texts = _sent_texts(replier.send)
    assert any("in reply to" in text for text in dm_texts), dm_texts
    assert any("#1-1" in text for text in dm_texts), dm_texts
    assert any("second!" in text for text in dm_texts), dm_texts


async def test_thread_created_lazily_and_reused(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog._confess_submit(make_click(guild, user_id=7), "thread me", [])
    confession = AsyncMock()
    confession.create_thread = AsyncMock()
    thread, _reply = make_thread()
    confession.create_thread.return_value = thread
    public.fetch_message = AsyncMock(return_value=confession)
    replier = make_member(9)
    guild.get_member.side_effect = lambda uid: {7: _author, 9: replier}.get(uid)
    assert (await cog.post_reply(guild, 111, 0, replier, "hi"))[0] == "ok"
    confession.create_thread.assert_awaited_once()
    assert (await cog.store.get(1, 111))["thread_id"] == 777
    guild.get_thread.return_value = thread
    second = AsyncMock()
    second.id = 903
    second.jump_url = "https://discord.com/channels/1/42/903"
    second.attachments = []
    thread.send.side_effect = [thread.send.return_value, second]
    assert (await cog.post_reply(guild, 111, 0, replier, "again"))[0] == "ok"
    confession.create_thread.assert_awaited_once()


async def test_reveal_author_line_in_review(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot, review=True, reveal=True)
    await cog._confess_submit(make_click(guild, user_id=7), "shown", [])
    view = review.send.await_args.kwargs["view"]
    assert any("<@7>" in text for text in _texts(view)), _texts(view)


async def test_card_has_no_identicon(tmp_path):
    """The public card shows the alias, the body and the image; no identicon."""
    from rosemary.core.card_service import render_card_message

    bot = FakeBot(tmp_path)
    payload, _allowed = await render_card_message(
        bot, 1, "confessions.card",
        {
            "confession_number": 1,
            "title": "Confession #1",
            "body": "segredo",
            "image_url": "",
            "color_hex": "#FFB7C5",
            "alias_name": "Maçã",
            "op_emoji": "",
            "title_emoji": "✨",
        },
    )
    assert payload is not None
    assert payload_thumbnail(payload) == ""
    texts = payload_texts(payload)
    assert "**Maçã ✨ (Confession #1)**" in texts, texts
    assert "segredo" in texts, texts
    assert payload_footer_lines(payload) == []


async def test_start_registers_two_dispatchers(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    await cog.start()
    await cog.start()
    assert len(bot.views) == 2


async def test_boot_reconciles_reveal_and_seal(tmp_path):
    """The reveal and the seal are exclusive: a guild that has both on at
    boot gets the seal off (the reveal needs the readable id)."""
    from rosemary.core.settings import get_setting, set_setting

    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    guild, *_ = make_cguild()
    bot.guilds = [guild]
    await set_setting(bot.storage, 1, "confessions.reveal_author", True)
    await set_setting(bot.storage, 1, "confessions.sealed_author", True)
    await cog.start()
    assert await get_setting(bot.storage, 1, "confessions.sealed_author") is False
    assert await get_setting(bot.storage, 1, "confessions.reveal_author") is True


async def test_deleted_public_post_cascades(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog._confess_submit(make_click(guild, user_id=7), "doomed", [])
    thread, _reply = make_thread()
    guild.get_thread.return_value = thread
    await cog.store.set_thread(1, 111, 777)
    replier = make_member(9)
    guild.get_member.side_effect = lambda uid: {7: _author, 9: replier}.get(uid)
    await cog.post_reply(guild, 111, 0, replier, "hi")
    payload = MagicMock()
    payload.guild_id = 1
    payload.message_id = 111
    await cog.on_raw_message_delete(payload)
    assert await cog.store.get(1, 111) is None
    assert await cog.store.get_reply(1, 901) is None


#: button and modal handlers --------------------------------------------------


async def test_new_button_opens_modal(tmp_path):

    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    view = ConfessionView(bot, 1)
    interaction = make_click(guild)
    await view._new(interaction)
    interaction.response.send_modal.assert_awaited_once()


async def test_new_button_refuses_banned(tmp_path):

    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog.store.ban(1, 9)
    view = ConfessionView(bot, 1)
    interaction = make_click(guild)
    await view._new(interaction)
    interaction.response.send_modal.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()


async def test_reply_button_sets_context_and_opens_modal(tmp_path):

    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog._confess_submit(make_click(guild, user_id=7), "reply me", [])
    message = MagicMock()
    message.id = 111
    view = ConfessionView(bot, 1)
    interaction = make_click(guild, user_id=9, message=message)
    await view._reply(interaction)
    interaction.response.send_modal.assert_awaited_once()
    assert cog._modal_context[9] == ("reply", 111, 0)


async def test_reply_submit_posts_numbered_card_and_dm(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, _review, author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog._confess_submit(make_click(guild, user_id=7), "reply me", [])
    thread, _reply = make_thread()
    guild.get_thread.return_value = thread
    await cog.store.set_thread(1, 111, 777)
    replier = make_member(9)
    guild.get_member.side_effect = lambda uid: {7: author, 9: replier}.get(uid)
    cog._modal_context[9] = ("reply", 111, 0)
    interaction = make_click(guild, user_id=9)
    await cog._reply_submit(interaction, "my take", [])
    thread.send.assert_awaited_once()
    texts = _sent_texts(thread.send)
    assert any("#1-1" in text for text in texts), texts
    assert any("my take" in text for text in texts), texts
    author.send.assert_awaited_once()
    text = interaction.followup.send.await_args.args[0]
    assert "postada" in text or "posted" in text, text


async def test_approve_button_posts_and_confirms(tmp_path):

    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, review, _author, moderator, _sent, sent_review = make_cguild()
    await _enable_confessions(bot, review=True)
    await cog._confess_submit(make_click(guild, user_id=7), "approve me", [])
    message = MagicMock()
    message.id = 555
    view = ConfessionReviewView(bot, 1)
    interaction = make_click(guild, user_id=70, message=message)
    await view._approve(interaction)
    interaction.followup.send.assert_awaited_once()
    assert (await cog.store.get(1, 111))["status"] == "open"


async def test_deny_button_opens_reason_modal(tmp_path):

    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot, review=True)
    await cog._confess_submit(make_click(guild, user_id=7), "deny me", [])
    message = MagicMock()
    message.id = 555
    view = ConfessionReviewView(bot, 1)
    interaction = make_click(guild, user_id=70, message=message)
    await view._deny(interaction)
    interaction.response.send_modal.assert_awaited_once()
    assert cog._modal_context[70] == ("deny", 555, 0)
    await cog._reason_submit(make_click(guild, user_id=70), "too spicy")
    assert (await cog.store.get(1, 555))["status"] == "denied"


async def test_ban_button_bans_and_dm(tmp_path):

    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, _review, author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot, review=True)
    await cog._confess_submit(make_click(guild, user_id=7), "ban me", [])
    message = MagicMock()
    message.id = 555
    view = ConfessionReviewView(bot, 1)
    interaction = make_click(guild, user_id=70, message=message)
    await view._ban(interaction)
    interaction.response.send_modal.assert_awaited_once()
    await cog._reason_submit(make_click(guild, user_id=70), "spam")
    assert await cog.store.is_banned(1, 7) is True
    assert await cog.store.get(1, 555) is None
    author.send.assert_awaited_once()
    # The ban notice is a themed text card: the copy rides the content.
    kwargs = author.send.await_args.kwargs
    text = kwargs.get("content") or ""
    assert "spam" in text, kwargs
    assert kwargs["allowed_mentions"].none()
    # The plain-text grammar: the icon opens the title, the motive is a
    # bullet.
    assert text.startswith("🚫 **"), text
    assert "• **" in text, text


async def test_modal_submit_with_image_attaches(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, _author, _mod, sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    cdn = MagicMock()
    cdn.url = "https://cdn.discordapp.com/attachments/1/42/pic.png"
    cdn.filename = "cat.png"
    sent.attachments = [cdn]
    interaction = make_click(guild, user_id=7)
    await cog._confess_submit(interaction, "with pic", [make_upload()])
    files = public.send.await_args.kwargs.get("files") or []
    # Only the user's picture rides with the message (no identicon).
    assert [f.filename for f in files] == ["cat.png"], [f.filename for f in files]
    entry = await cog.store.get(1, 111)
    assert entry is not None
    assert entry["image_url"] == "https://cdn.discordapp.com/attachments/1/42/pic.png"


async def test_non_staff_cannot_use_review_buttons(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot, review=True)
    await cog._confess_submit(make_click(guild, user_id=7), "deny me", [])
    plain = make_member(9, staff=False)
    guild.get_member.side_effect = lambda uid: {7: plain}.get(uid, plain)

    class FakeResponse:
        def is_done(self):
            return False

        async def send_message(self, content, **kwargs):
            self.sent = content

    message = MagicMock()
    message.id = 555
    view = ConfessionReviewView(bot, 1)
    interaction = make_click(guild, user_id=9, message=message)
    interaction.response = FakeResponse()
    await view._approve(interaction)
    assert "staff" in interaction.response.sent.lower(), interaction.response.sent


#: anonymity guard ------------------------------------------------------------


def _all_strings(view) -> list[str]:
    found = []
    for item in view.walk_children():
        for attr in ("content", "url", "label"):
            value = getattr(item, attr, None)
            if isinstance(value, str) and value:
                found.append(value)
    return found


async def test_public_card_shows_no_identicon_nor_the_real_avatar(tmp_path):
    """Regression (live 400 + identity leak): the anonymous card shows no
    generated identicon and never the author's own avatar."""
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog._confess_submit(make_click(guild, user_id=7), "secret", [])
    thumbnail = payload_thumbnail(sent(public.send.await_args))
    assert thumbnail == "", "no identicon rides the card"
    files = public.send.await_args.kwargs.get("files") or []
    assert not any(f.filename.startswith("anonimo_") for f in files), "no identicon file"
    avatar = "https://cdn.discordapp.com/embed/avatars/0.png"
    assert avatar not in "\n".join(_sent_texts(public.send))


async def test_review_card_has_no_avatar_thumbnail(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot, review=True, reveal=True)
    await cog._confess_submit(make_click(guild, user_id=7), "secret", [])
    view = review.send.await_args.kwargs["view"]
    assert not any(
        isinstance(item, discord.ui.Thumbnail) for item in view.walk_children()
    )
    avatar = "https://cdn.discordapp.com/embed/avatars/0.png"
    assert not any(avatar in text for text in _all_strings(view))
    assert any("<@7>" in text for text in _all_strings(view))


async def test_reply_with_image_attaches_and_persists(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, _review, author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog._confess_submit(make_click(guild, user_id=7), "pic thread", [])
    thread, first_reply = make_thread(901)
    guild.get_thread.return_value = thread
    await cog.store.set_thread(1, 111, 777)
    cdn = MagicMock()
    cdn.url = "https://cdn.discordapp.com/attachments/1/42/reply.png"
    cdn.filename = "cat.png"
    first_reply.attachments = [cdn]
    replier = make_member(9)
    guild.get_member.side_effect = lambda uid: {7: author, 9: replier}.get(uid)
    cog._modal_context[9] = ("reply", 111, 0)
    interaction = make_click(guild, user_id=9)
    await cog._reply_submit(interaction, "look", [make_upload()])
    files = thread.send.await_args.kwargs.get("files") or []
    # Only the user's picture rides with the message (no identicon).
    assert [f.filename for f in files] == ["cat.png"], [f.filename for f in files]
    reply = await cog.store.get_reply(1, 901)
    assert reply is not None
    assert reply["image_url"] == "https://cdn.discordapp.com/attachments/1/42/reply.png"
    assert thread.send.await_args.kwargs.get("view") is not None
    assert payload_image(sent(thread.send.await_args)), "the picture rides the card"


def _has_gallery(view) -> bool:
    return any(
        isinstance(item, discord.ui.MediaGallery) for item in view.walk_children()
    )


#: card layout ----------------------------------------------------------------


def _png_bytes(files) -> bytes:
    """The bytes of the first attached file."""
    assert files, "no file attached"
    data = files[0].fp.read()
    return data if isinstance(data, bytes) else data.read()


def _identicon_file(files):
    """The attached file whose name starts with ``anonimo_``."""
    for file in files:
        if file.filename.startswith("anonimo_"):
            return file
    return None


async def test_card_shows_the_alias_as_title_and_the_number_in_the_title(tmp_path):
    """The alias and the fruit emoji open the title; the number closes it.

    The card is a Components V2: the alias rides the heading with the random
    emoji and the numbered title, and the public confession carries no footer.
    """
    from rosemary.core.card_service import render_card_message

    bot = FakeBot(tmp_path)
    payload, _allowed = await render_card_message(
        bot,
        1,
        "confessions.card",
        {
            "confession_number": 55,
            "title": "Confession #55",
            "body": "o texto",
            "image_url": "",
            "color_hex": "#FFB7C5",
            "alias_name": "Anonymous K7Q",
            "op_emoji": "",
            "title_emoji": "✨",
        },
    )
    assert payload is not None
    texts = payload_texts(payload)
    assert "**Anonymous K7Q ✨ (Confession #55)**" in texts
    assert "o texto" in texts
    assert payload_thumbnail(payload) == ""
    assert payload_footer_lines(payload) == []
    # The alias' own color is the accent.
    assert payload_accent(payload) == 0xFFB7C5
    # No identicon: the card shows the sentence, the picture and no footer.
    assert payload_thumbnail(payload) == ""


async def test_reply_card_moves_the_reference_to_the_footer(tmp_path):
    """The reply keeps the alias as its title; the footer carries the number.

    The parent reference rides Discord's native reply line, never the card.
    """
    from rosemary.core.card_service import render_card_message

    bot = FakeBot(tmp_path)
    payload, _allowed = await render_card_message(
        bot,
        1,
        "confessions.response",
        {
            "confession_number": 55,
            "reply_index": 1,
            "title": "Confession #55-1",
            "body": "o texto",
            "color_hex": "#FFB7C5",
            "image_url": "",
            "alias_name": "Anonymous K7Q",
            "op_emoji": "<:op_emoji:123>",
            "title_emoji": "✨",
        },
    )
    assert payload is not None
    texts = payload_texts(payload)
    assert "**Anonymous K7Q<:op_emoji:123> ✨ (Confession #55-1)**" in texts
    assert "o texto" in texts
    assert payload_thumbnail(payload) == ""
    # Only the number: the parent's own text never rides the card (the native
    # reply points at it) and the OP emoji alone marks the author.
    assert payload_footer_lines(payload) == ["Confession #55-1"]


async def test_review_card_closes_with_the_standard_footer(tmp_path):
    """The staff card ends with the bot's standard small timestamp line."""
    from rosemary.core.card_service import render_card_message

    bot = FakeBot(tmp_path)
    payload, _allowed = await render_card_message(
        bot,
        1,
        "confessions.review",
        {
            "confession_number": 60,
            "title": "Confession (#60) · Review",
            "body": "o texto",
            "image_url": "",
            "author_line": "",
            "timestamp": "2 minutes ago",
        },
    )
    assert payload is not None and payload.view is not None
    texts = [
        item.content
        for item in payload.view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]
    assert texts[-1] == "-# 2 minutes ago"
    # The reveal line is a paragraph of its own: it disappears when the
    # guild keeps the author anonymous.
    assert not any("<@" in text for text in texts)


async def test_public_card_keeps_the_text_without_an_identicon(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog._confess_submit(make_click(guild, user_id=7), "my secret", [])
    files = public.send.await_args.kwargs.get("files") or []
    assert _identicon_file(files) is None, [f.filename for f in files]
    texts = _sent_texts(public.send)
    # The body is plain text in the card, not a rendered image.
    assert any("my secret" in text for text in texts), texts
    # The alias is the heading of the card and the number closes the title.
    assert any(text.startswith("**") for text in texts), texts
    assert any("Confession #1" in text for text in texts), texts
    entry = await cog.store.get(1, 111)
    assert entry is not None
    assert entry["alias"].strip(), entry
    assert entry["color_hex"].startswith("#")
    assert entry["title_emoji"]


async def test_public_card_never_shows_the_real_identity(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog._confess_submit(make_click(guild, user_id=7), "secret", [])
    strings = _all_strings(public.send.await_args.kwargs["view"])
    assert not any("User7" in text for text in strings), strings
    assert not any("embed/avatars" in text for text in strings), strings


async def test_reply_by_the_confession_author_is_flagged_op(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog._confess_submit(make_click(guild, user_id=7), "mine", [])
    thread, _reply = make_thread(901)
    guild.get_thread.return_value = thread
    await cog.store.set_thread(1, 111, 777)
    outcome, _msg = await cog.post_reply(guild, 111, 0, author, "my own reply")
    assert outcome == "ok"
    stored = await cog.store.get_reply(1, 901)
    assert stored is not None and stored["is_op"] is True
    texts = _sent_texts(thread.send)
    # The OP emoji rides the alias name: it is the only mark left.
    assert any("<:op_emoji:123>" in text for text in texts), texts
    assert not any("Confession author" in text for text in texts), texts
    # No DM about your own comment.
    assert author.send.await_count == 0


async def test_op_emoji_can_be_turned_off(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot, mark_op=False)
    await cog._confess_submit(make_click(guild, user_id=7), "mine", [])
    thread, _reply = make_thread(901)
    guild.get_thread.return_value = thread
    await cog.store.set_thread(1, 111, 777)
    outcome, _msg = await cog.post_reply(guild, 111, 0, author, "my own reply")
    assert outcome == "ok"
    texts = _sent_texts(thread.send)
    assert not any("<:op_emoji" in text for text in texts), texts
    assert not any("Confession author" in text for text in texts), texts


async def test_alias_is_stable_inside_one_thread(tmp_path):
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog._confess_submit(make_click(guild, user_id=7), "mine", [])
    thread, first = make_thread(901)
    second = AsyncMock()
    second.id = 902
    second.jump_url = "https://discord.com/channels/1/777/902"
    second.attachments = []
    thread.send.side_effect = [first, second]
    guild.get_thread.return_value = thread
    await cog.store.set_thread(1, 111, 777)
    replier = make_member(9)
    guild.get_member.side_effect = lambda uid: {7: author, 9: replier}.get(uid)
    assert (await cog.post_reply(guild, 111, 0, replier, "one"))[0] == "ok"
    assert (await cog.post_reply(guild, 111, 901, replier, "two"))[0] == "ok"
    first_reply = await cog.store.get_reply(1, 901)
    second_reply = await cog.store.get_reply(1, 902)
    # The same participant keeps the same alias for the thread (the reply
    # carries it, so it survives the sealed mode and the restart).
    assert first_reply["alias"] == second_reply["alias"]
    confession = await cog.store.get(1, 111)
    assert first_reply["alias"] != confession["alias"]


async def test_nested_reply_links_to_the_parent_without_quoting_it(tmp_path):
    """The reply points at the parent comment through Discord's native reply.

    The card carries only the number of the answer, never a copy of what the
    parent said nor a hand-written "in reply to" line.
    """
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog._confess_submit(make_click(guild, user_id=7), "mine", [])
    thread, first = make_thread(901)
    second = AsyncMock()
    second.id = 902
    second.jump_url = "https://discord.com/channels/1/777/902"
    second.attachments = []
    thread.send.side_effect = [first, second]
    guild.get_thread.return_value = thread
    await cog.store.set_thread(1, 111, 777)
    # The fetches resolve to real messages, so the native reference is built.
    parent = MagicMock()
    parent.id = 901
    thread.get_partial_message.return_value.fetch.return_value = parent
    confession_message = MagicMock()
    confession_message.id = 111
    public.get_partial_message.return_value.fetch.return_value = confession_message
    replier = make_member(9)
    other = make_member(10)
    guild.get_member.side_effect = (
        lambda uid: {7: author, 9: replier, 10: other}.get(uid)
    )
    assert (await cog.post_reply(guild, 111, 0, replier, "parent comment"))[0] == "ok"
    assert (await cog.post_reply(guild, 111, 901, other, "child comment"))[0] == "ok"
    texts = _sent_texts(thread.send)
    assert any("Confession #1-2" in text for text in texts), texts
    # The native reply carries the parent message; the copy does not.
    assert thread.send.await_args.kwargs.get("reference") is parent
    assert not any(
        "discord.com/channels/1/777/901" in text for text in texts
    ), texts
    # The parent's own text is not copied into the reply card.
    assert not any("parent comment" in text for text in texts), texts


#: stale interactions (dead token) ---------------------------------------------


def _dead_token() -> discord.NotFound:
    """The error Discord answers when the interaction token is already gone."""
    return discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"),
        {"code": 10062, "message": "Unknown interaction"},
    )


async def test_confess_submit_with_a_dead_token_posts_nothing(tmp_path):
    """A submit replayed after a RESUME must not post behind a dead token.

    Discord answers 404/10062 to the defer, the handler stops there and no
    traceback reaches the console.
    """
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    interaction = make_click(guild, user_id=7)
    interaction.response.defer = AsyncMock(side_effect=_dead_token())
    await cog._confess_submit(interaction, "my secret", [])
    public.send.assert_not_awaited()
    assert await cog.store.get(1, 111) is None
    interaction.followup.send.assert_not_awaited()


async def test_reply_submit_with_a_dead_token_posts_nothing(tmp_path):
    """The same for the reply modal: nothing is posted behind a dead token."""
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, _review, author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    await cog._confess_submit(make_click(guild, user_id=7), "reply me", [])
    thread, _reply = make_thread()
    guild.get_thread.return_value = thread
    await cog.store.set_thread(1, 111, 777)
    replier = make_member(9)
    guild.get_member.side_effect = lambda uid: {7: author, 9: replier}.get(uid)
    cog._modal_context[9] = ("reply", 111, 0)
    interaction = make_click(guild, user_id=9)
    interaction.response.defer = AsyncMock(side_effect=_dead_token())
    await cog._reply_submit(interaction, "my take", [])
    thread.send.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()


async def test_reason_submit_with_a_dead_token_moderates_nothing(tmp_path):
    """A replayed deny/ban modal never reaches the moderation action."""
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, review, _author, moderator, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot, review=True)
    await cog._confess_submit(make_click(guild, user_id=7), "deny me", [])
    cog._modal_context[70] = ("deny", 555, 0)
    interaction = make_click(guild, user_id=70)
    interaction.response.defer = AsyncMock(side_effect=_dead_token())
    await cog._reason_submit(interaction, "because")
    review.get_partial_message.return_value.delete.assert_not_awaited()
    entry = await cog.store.get(1, 555)
    assert entry["status"] == "pending"
    interaction.followup.send.assert_not_awaited()


async def test_sealed_store_never_writes_the_author_id(tmp_path):
    """The sealed entry carries the sealed blob and the claim hash, never
    the readable id (the store is what a leak would expose)."""
    from rosemary.core.alias_identity import claim_hash, claim_token, generate_keypair, seal
    from rosemary.core.confessions import ConfessionStore as Store

    store = Store(tmp_path)
    pub, _priv = generate_keypair()
    token = claim_token()
    await store.put(
        1, 10, number=1, author_id=7, channel_id=5, body="s",
        author_seal=seal(pub, "k1", "7"), claim_hash=claim_hash(token),
    )
    entry = await store.get(1, 10)
    assert entry is not None
    assert "author_id" not in entry
    assert entry["author_seal"].startswith("k1:")
    assert entry["claim_hash"] == claim_hash(token)
    assert await store.claim(1, token) is not None
    assert await store.claim(1, "wrong") is None


async def test_sealed_ban_uses_the_hmac(tmp_path):
    """The sealed ban list stores the HMAC (no id), and the check reads it."""
    from rosemary.core.alias_identity import ban_hash
    from rosemary.core.confessions import ConfessionStore as Store

    store = Store(tmp_path)
    await store.ban(1, 7, sealed=True)
    data = await store._doc(1)
    assert data["banned"] == [ban_hash(tmp_path, 1, 7)]
    assert await store.is_banned(1, 7, sealed=True) is True
    assert await store.is_banned(1, 8, sealed=True) is False


async def test_confession_confirmation_carries_the_seal_proof(tmp_path):
    """The "confession sent" confirmation is the member's proof: the
    algorithm, the key fingerprint, the claim token and the repo."""
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    from rosemary.core.alias_identity import KeyRing, generate_keypair
    from rosemary.core.settings import set_setting

    pub, _priv = generate_keypair()
    await set_setting(bot.storage, 1, "confessions.sealed_author", True)
    await set_setting(bot.storage, 1, "confessions.reveal_author", False)
    await KeyRing(bot.storage.data_dir).add(1, pub, moderation_seconds=60)

    interaction = make_click(guild, user_id=7)
    await cog._confess_submit(interaction, "segredo", [])

    confirmation = interaction.followup.send.await_args.args[0]
    assert "X25519 + XSalsa20-Poly1305" in confirmation, confirmation
    assert "claim key" in confirmation.lower(), confirmation
    assert "code:" in confirmation.lower(), confirmation
    # The member can check the exact key period and public fingerprint.
    from rosemary.core.alias_identity import fingerprint

    active = await cog._keys.active(1)
    assert active is not None
    assert f"`{active.key_id}`" in confirmation, confirmation
    assert f"`{fingerprint(active.public)}`" in confirmation, confirmation
    # The token is shown once and matches the in-memory claim.
    tokens = [
        token
        for token in cog._claim_tokens
        if f"`{token}`" in confirmation
    ]
    assert len(tokens) == 1, confirmation
    entry = await cog.store.get(1, 111)
    assert entry is not None
    assert "author_id" not in entry, entry
    assert ":" in entry["author_seal"], entry["author_seal"]


async def test_sealed_send_splits_the_key_among_the_staff(tmp_path):
    """The first sealed send generates the pair and DMs the Shamir shares.

    The bot keeps the public half only; three of the five DMs rebuild the
    private half and open the stored seal back to the author id.
    """
    import re

    from rosemary.core.alias_identity import open_seal, shamir_open

    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, _public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot, sealed=True)

    owner = make_member(1, staff=True)
    mods = [make_member(uid, staff=True) for uid in (2, 3, 4, 5)]
    for position, member in enumerate(mods, start=1):
        member.top_role.position = position
    for member in [owner, *mods]:
        member.bot = False
        member.guild_permissions.moderate_members = True
    guild.owner_id = owner.id
    guild.members = [owner, *mods]
    everyone = {member.id: member for member in [owner, *mods]}
    everyone[_author.id] = _author
    guild.get_member.side_effect = lambda uid: everyone.get(uid)

    await cog._confess_submit(make_click(guild, user_id=7), "segredo", [])

    keys = await cog._keys.keys(1)
    assert len(keys) == 1 and keys[0].public
    # One share per holder, the owner included: 3 of 5 opens the seal.
    shares = []
    for member in [owner, *mods]:
        member.send.assert_awaited_once()
        content = member.send.await_args.kwargs["content"]
        match = re.search(r"`([^`]+)`", content)
        assert match is not None, content
        shares.append(match.group(1))
    private = shamir_open(shares[:3])
    entry = await cog.store.get(1, 111)
    assert entry is not None
    assert open_seal(private, entry["author_seal"]) == "7"
    assert "author_id" not in entry


async def test_panel_is_posted_and_the_write_button_opens_the_modal(tmp_path):
    """The panel is the no-typing path: the write button opens the modal."""
    bot = FakeBot(tmp_path)
    cog = ConfessionsCog(bot)
    bot._cog = cog
    guild, public, _review, _author, _mod, _sent, _sent_review = make_cguild()
    await _enable_confessions(bot)
    bot.get_guild = MagicMock(return_value=guild)

    await cog.repaint_panel(bot, 1)

    public.send.assert_awaited_once()
    view = public.send.await_args.kwargs.get("view")
    assert view is not None
    buttons = [
        item for item in view.walk_children()
        if getattr(item, "custom_id", None) == "confess_new"
    ]
    assert [button.label for button in buttons] == ["Write confession"]
    panel_id = await cog.store.get_panel(1)
    assert panel_id == 111

    interaction = make_click(guild, user_id=7)
    view = ConfessionView(bot, 1)
    await view._new(interaction)
    interaction.response.send_modal.assert_awaited_once()
