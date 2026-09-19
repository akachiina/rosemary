"""Info commands: default builders, variable contracts and emoji dispatch.

The five info cards (``utility.userinfo``/``emojiinfo``/``stickerinfo``/
``roleinfo``/``channelinfo``) are rich-builder cards like
``utility.serverinfo``: these tests pin the render path with real catalogs,
the optional-block behavior (banner, nickname, lottie sticker...), the emoji
command's dispatch (custom / unicode / sticker) and the no-seed-map rule.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

import rosemary.cogs.info  # noqa: F401  (registers the builders)
from rosemary.core.card_service import SEED_PARTS_BY_KEY, default_document, render_document
from rosemary.core.i18n import Translator
from rosemary.ui.theme import load_theme


def make_bot(tmp_path=None):
    from pathlib import Path

    bot = MagicMock()
    bot.theme = load_theme()
    bot.translator = Translator(Path("language"))
    if tmp_path is not None:
        from rosemary.core.storage import GuildStorage
        from rosemary.core.themes import ThemeStore

        bot.storage = GuildStorage(tmp_path)
        bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    else:
        bot._theme_store = MagicMock()
    return bot


def container_texts(view) -> list[str]:
    texts = []

    def walk(node):
        if isinstance(node, discord.ui.TextDisplay):
            texts.append(node.content)
        for child in (
            list(getattr(node, "children", []) or [])
            + list(getattr(node, "items", []) or [])
            + list(getattr(node, "walk_children", lambda: [])())
        ):
            walk(child)

    for item in view.children:
        walk(item)
    return texts


def _sections_and_galleries(view):
    sections: list = []
    galleries: list = []

    def walk(node):
        if type(node).__name__ == "Section":
            sections.append(node)
        if type(node).__name__ == "MediaGallery":
            galleries.append(node)
        for child in (
            list(getattr(node, "items", []) or [])
            + list(getattr(node, "children", []) or [])
            + [getattr(node, "accessory", None)]
        ):
            if child is not None:
                walk(child)

    for item in view.children:
        walk(item)
    return sections, galleries


async def _build_and_render(bot, key, variables):
    doc = await default_document(bot, 1, key, variables)
    assert doc is not None, f"{key}: no default document"
    return await render_document(bot, doc, variables, guild_id=1, card_key=key)


#: rich builders render with real catalogs ------------------------------------


async def test_userinfo_card_renders_full_and_degraded():
    bot = make_bot()
    full = {
        "user": "<@5>",
        "user_name": "Ana",
        "user_avatar": "https://a.b/ana.png",
        "banner_url": "https://a.b/banner.png",
        "nickname": "aninha",
        "created_at": "<t:1111111111:D>",
        "created_rel": "<t:1111111111:R>",
        "joined_at": "<t:2222222222:D>",
        "joined_rel": "<t:2222222222:R>",
        "roles": 4,
        "top_role": "<@&7>",
        "boosting_since": "<t:3333333333:R>",
        "timeout": "nenhuma",
        "is_bot": "Não",
        "user_id": "123",
    }
    view = await _build_and_render(bot, "utility.userinfo", full)
    texts = container_texts(view)
    assert any("Ana" in t for t in texts)
    assert any("aninha" in t for t in texts), texts
    assert any("<@&7>" in t for t in texts)
    sections, galleries = _sections_and_galleries(view)
    assert len(sections) == 1
    assert "ana.png" in sections[0].accessory.media.url
    assert len(galleries) == 1  # banner gallery present

    # Banner sits between the header section and the divider: visible near
    # the top, not after the stats (document order, no late-append).
    container = view.children[0]
    blocks = list(container.items)
    banner_index = next(
        i for i, b in enumerate(blocks) if type(b).__name__ == "MediaGallery"
    )
    header_index = next(i for i, b in enumerate(blocks) if type(b).__name__ == "Section")
    assert banner_index == header_index + 1
    # No raw keys leak into the card (the created_at_label regression).
    assert not any("card.utility." in t for t in texts), texts

    # No banner, no nickname, no boost, no top-role pill: blocks drop out,
    # still renders.
    bare = {
        **full,
        "banner_url": "",
        "nickname": "",
        "boosting_since": "",
        "top_role": "",
    }
    view = await _build_and_render(bot, "utility.userinfo", bare)
    texts = container_texts(view)
    assert not any("aninha" in t for t in texts)
    assert not any("<@&7>" in t for t in texts)
    sections, galleries = _sections_and_galleries(view)
    assert galleries == []
    # No avatar -> no section accessory at all (never an empty thumbnail).
    plain = {**bare, "user_avatar": ""}
    view = await _build_and_render(bot, "utility.userinfo", plain)
    sections, _galleries = _sections_and_galleries(view)
    assert sections == []


async def test_emojiinfo_card_custom_and_unicode():
    bot = make_bot()
    custom = {
        "emoji": "<:rosa:123>",
        "emoji_name": "rosa",
        "emoji_id": "123",
        "animated": "Não",
        "emoji_url": "https://cdn.discordapp.com/emojis/123.png",
        "source": "Emoji do servidor",
        "created_at": "<t:1111111111:D>",
        "created_rel": "<t:1111111111:R>",
    }
    view = await _build_and_render(bot, "utility.emojiinfo", custom)
    sections, _galleries = _sections_and_galleries(view)
    assert len(sections) == 1
    assert "123.png" in sections[0].accessory.media.url
    assert any("Emoji do servidor" in t for t in container_texts(view))
    # No raw keys leak into the card (the created_at_label regression).
    assert not any("card.utility." in t for t in container_texts(view))

    unicode_vars = {
        "emoji": "🌻",
        "emoji_name": "🌻",
        "emoji_id": "",
        "animated": "",
        "emoji_url": "",
        "source": "",
        "created_at": "",
        "created_rel": "",
    }
    view = await _build_and_render(bot, "utility.emojiinfo", unicode_vars)
    texts = container_texts(view)
    assert any("🌻" in t for t in texts)
    sections, _galleries = _sections_and_galleries(view)
    assert sections == []  # nothing to show as an image
    assert not any("card.utility." in t for t in texts)


async def test_stickerinfo_gallery_drops_for_lottie():
    bot = make_bot()
    png = {
        "sticker_name": "Sela",
        "sticker_id": "42",
        "description": "um selo",
        "format": "PNG",
        "tags": "selo, verde",
        "uploader": "<@9>",
        "sticker_url": "https://cdn.discordapp.com/stickers/42.png",
        "created_at": "<t:1111111111:D>",
        "created_rel": "<t:1111111111:R>",
    }
    view = await _build_and_render(bot, "utility.stickerinfo", png)
    _sections, galleries = _sections_and_galleries(view)
    assert len(galleries) == 1
    assert "42.png" in galleries[0].items[0].url

    lottie = {**png, "sticker_url": "", "description": "", "format": "LOTTIE"}
    view = await _build_and_render(bot, "utility.stickerinfo", lottie)
    _sections, galleries = _sections_and_galleries(view)
    assert galleries == []
    assert any("LOTTIE" in t for t in container_texts(view))


async def test_roleinfo_card_color_and_icon_optional():
    bot = make_bot()
    rich = {
        "role": "<@&7>",
        "role_name": "Moderador",
        "role_color": "#3498db",
        "members": 12,
        "position": 5,
        "mentionable": "Sim",
        "hoist": "Não",
        "role_icon": "https://a.b/icon.png",
        "role_id": "7",
        "created_at": "<t:1111111111:D>",
        "created_rel": "<t:1111111111:R>",
    }
    view = await _build_and_render(bot, "utility.roleinfo", rich)
    sections, _galleries = _sections_and_galleries(view)
    assert len(sections) == 1
    texts = container_texts(view)
    assert any("#3498db" in t for t in texts)
    assert any("Moderador" in t for t in texts)

    plain = {**rich, "role_color": "", "role_icon": ""}
    view = await _build_and_render(bot, "utility.roleinfo", plain)
    texts = container_texts(view)
    assert not any("#3498db" in t for t in texts)
    sections, _galleries = _sections_and_galleries(view)
    assert sections == []


async def test_channelinfo_card_voice_and_text():
    bot = make_bot()
    voice = {
        "channel": "<#3>",
        "channel_name": "Geral",
        "channel_type": "Voz",
        "topic": "",
        "category": "Salas",
        "nsfw": "Não",
        "slowmode": "",
        "position": 2,
        "bitrate": "96kbps",
        "user_limit": "10",
        "channel_id": "3",
        "created_at": "<t:1111111111:D>",
        "created_rel": "<t:1111111111:R>",
    }
    view = await _build_and_render(bot, "utility.channelinfo", voice)
    texts = container_texts(view)
    assert any("96kbps" in t for t in texts)
    assert any("10" in t for t in texts)

    text = {**voice, "channel_type": "Texto", "bitrate": "", "user_limit": ""}
    view = await _build_and_render(bot, "utility.channelinfo", text)
    texts = container_texts(view)
    assert not any("kbps" in t for t in texts)
    assert any("Texto" in t for t in texts)


#: seed-map rule ---------------------------------------------------------------


def test_info_cards_resolve_via_builder_not_seed():
    """A seed entry would shadow the registered rich builders (the starboard/
    serverinfo shadowing regression)."""
    for key in (
        "utility.userinfo",
        "utility.emojiinfo",
        "utility.stickerinfo",
        "utility.roleinfo",
        "utility.channelinfo",
    ):
        assert key not in SEED_PARTS_BY_KEY, f"seed would shadow the builder: {key}"


#: emoji dispatch ---------------------------------------------------------------


class _FakeResponse:
    def __init__(self):
        self.calls: list[tuple] = []

    def is_done(self):
        return False

    async def defer(self, **kwargs):
        self.calls.append(("defer", kwargs))

    async def send_message(self, content=None, **kwargs):
        self.calls.append(("send", content, kwargs))


class _FakeFollowup:
    def __init__(self):
        self.calls: list[tuple] = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)


def _make_ctx(guild, author):
    from unittest.mock import AsyncMock

    ctx = MagicMock()
    ctx.guild = guild
    ctx.guild_id = 1
    ctx.author = author
    ctx.channel = guild.text_channels[0] if guild.text_channels else guild
    ctx.response = _FakeResponse()
    ctx.respond = AsyncMock()
    ctx.defer = AsyncMock()
    ctx.followup = _FakeFollowup()
    return ctx


def make_pipeline_bot(tmp_path):
    """Mock bot whose card-pipeline attributes are real objects (theme,
    translator, storage, theme store) so render_card_message works."""
    return make_bot(tmp_path)


def _guild_with_emoji(emoji):
    guild = MagicMock()
    guild.id = 1
    guild.name = "Serv"
    guild.text_channels = []
    guild.fetch_emoji = AsyncMock(side_effect=discord.NotFound(MagicMock(), "nope"))
    guild.fetch_sticker = AsyncMock(side_effect=discord.NotFound(MagicMock(), "nope"))
    return guild


def _member(**overrides):
    member = MagicMock()
    member.id = 5
    member.mention = "<@5>"
    member.display_name = "Ana"
    member.bot = False
    member.created_at = discord.utils.snowflake_time(100000000000000000)
    member.joined_at = discord.utils.snowflake_time(200000000000000000)
    member.premium_since = None
    member.nick = None
    member.display_avatar.url = "https://a.b/ana.png"
    member.guild_banner = None
    member.roles = [MagicMock(id=1)]
    member.top_role.mention = "<@&2>"
    for name, value in overrides.items():
        setattr(member, name, value)
    return member


async def _run_command(cog, coro):
    """Invoke the underlying callback with a fresh capture context."""
    ctx = coro.args[0] if coro.args else None
    return ctx


async def test_emojiinfo_dispatch_custom_unicode_and_sticker(tmp_path):
    from rosemary.cogs.info import InfoCog

    bot = make_pipeline_bot(tmp_path)
    cog = InfoCog(bot)

    guild = _guild_with_emoji(None)
    author = _member()
    ctx = _make_ctx(guild, author)

    # Custom emoji resolves from the bot cache (real 18-digit snowflake:
    # py-cord's from_str only parses ids with 13-20 digits).
    cached = MagicMock(name="GuildEmoji")
    cached.name = "rosa"
    cached.id = 123456789012345678
    cached.animated = False
    cached.guild = guild
    cached.url = "https://cdn.discordapp.com/emojis/123456789012345678.png"
    cached.created_at = discord.utils.snowflake_time(123456789012345678)
    get_emoji = MagicMock(name="get_emoji")
    get_emoji.return_value = cached
    bot.get_emoji = get_emoji

    await InfoCog.emojiinfo.callback(cog, ctx, emoji="<:rosa:123456789012345678>")
    assert ctx.respond.call_args is not None, "card must be sent"
    sent = ctx.respond.call_args.kwargs
    allowed = sent.get("allowed_mentions")
    assert (allowed.everyone, allowed.users, allowed.roles, allowed.replied_user) == (
        False, False, False, False
    )
    assert sent.get("view") is not None

    # Unicode glyph path: no fetches at all.
    ctx2 = _make_ctx(guild, author)
    bot.get_emoji = MagicMock()
    await InfoCog.emojiinfo.callback(cog, ctx2, emoji="🌻")
    bot.get_emoji.assert_not_called()
    assert ctx2.respond.call_args is not None

    # Numeric input goes to the sticker fetch, which misses -> not_found.
    ctx3 = _make_ctx(guild, author)
    guild.fetch_sticker = AsyncMock(side_effect=discord.NotFound(MagicMock(), "x"))
    bot.fetch_emoji = AsyncMock(side_effect=discord.NotFound(MagicMock(), "x"))
    await InfoCog.emojiinfo.callback(cog, ctx3, emoji="999")
    assert ctx3.respond.call_args.args[0].endswith("Could not find this emoji or sticker.")
    assert ctx3.respond.call_args.kwargs == {"ephemeral": True}

    # Garbage input -> error.
    ctx4 = _make_ctx(guild, author)
    await InfoCog.emojiinfo.callback(cog, ctx4, emoji="<:broken")
    assert "Invalid emoji" in ctx4.respond.call_args.args[0]
    assert ctx4.respond.call_args.kwargs == {"ephemeral": True}


async def test_userinfo_defers_and_sends_card(tmp_path):
    from rosemary.cogs.info import InfoCog

    bot = make_pipeline_bot(tmp_path)
    cog = InfoCog(bot)
    guild = _guild_with_emoji(None)
    guild.default_role = MagicMock(id=1)
    author = _member()
    ctx = _make_ctx(guild, author)
    bot.fetch_user = AsyncMock(
        return_value=MagicMock(banner=None)
    )

    await InfoCog.userinfo.callback(cog, ctx, member=None)
    assert ctx.defer.await_count == 1
    assert ctx.followup.calls, "card must be sent"
    sent = ctx.followup.calls[0]
    allowed = sent.get("allowed_mentions")
    assert (allowed.everyone, allowed.users, allowed.roles, allowed.replied_user) == (
        False, False, False, False
    )
    assert sent["view"] is not None


def _texts_of_view(view):
    texts = []

    def walk(node):
        if isinstance(node, discord.ui.TextDisplay):
            texts.append(node.content)
        for child in (
            list(getattr(node, "children", []) or [])
            + list(getattr(node, "items", []) or [])
            + list(getattr(node, "walk_children", lambda: [])())
        ):
            walk(child)

    for item in view.children:
        walk(item)
    return texts


async def test_userinfo_everyone_top_role_renders_plain_text(tmp_path):
    """@everyone has no mention pill (<@&guild_id> renders oddly and its token
    duplicates the subtitle): the card carries plain "@everyone" instead and
    the allowed_mentions still never allow an everyone ping."""
    from rosemary.cogs.info import InfoCog

    bot = make_pipeline_bot(tmp_path)
    cog = InfoCog(bot)
    guild = _guild_with_emoji(None)
    guild.default_role = MagicMock()
    guild.default_role.id = guild.id
    everyone = MagicMock()
    everyone.id = guild.id  # default role: is_default() True
    everyone.mention = "<@&1>"
    everyone.is_default = lambda: True
    member = _member()
    member.roles = [everyone]
    member.top_role = everyone
    ctx = _make_ctx(guild, member)
    bot.fetch_user = AsyncMock(return_value=MagicMock(banner=None))

    await InfoCog.userinfo.callback(cog, ctx, member=None)
    assert ctx.followup.calls, "card must be sent"
    view = ctx.followup.calls[0]["view"]
    texts = "\n".join(_texts_of_view(view))
    assert "@everyone" in texts
    assert "<@&1>" not in texts, "raw guild-id role token must not leak"
    allowed = ctx.followup.calls[0].get("allowed_mentions")
    assert (allowed.everyone, allowed.users, allowed.roles, allowed.replied_user) == (
        False, False, False, False
    )


async def test_roleinfo_and_channelinfo_default_targets(tmp_path):
    from rosemary.cogs.info import InfoCog

    bot = make_pipeline_bot(tmp_path)
    cog = InfoCog(bot)
    guild = _guild_with_emoji(None)
    role = MagicMock()
    role.id = 7
    role.name = "Moderador"
    role.mention = "<@&7>"
    role.colour = discord.Colour.default()
    role.icon = None
    role.members = [1, 2, 3]
    role.position = 5
    role.mentionable = False
    role.hoist = False
    role.created_at = discord.utils.snowflake_time(7)
    author = _member(top_role=role)
    ctx = _make_ctx(guild, author)

    await InfoCog.roleinfo.callback(cog, ctx, role=None)
    assert ctx.respond.call_args is not None, "roleinfo must send a card"

    channel = MagicMock()
    channel.id = 3
    channel.name = "geral"
    channel.type = discord.ChannelType.text
    channel.topic = "assunto"
    channel.category = None
    channel.nsfw = False
    channel.slowmode_delay = 5
    channel.bitrate = None
    channel.user_limit = None
    channel.position = 1
    channel.mention = "<#3>"
    ctx2 = _make_ctx(guild, author)
    ctx2.channel = channel
    await InfoCog.channelinfo.callback(cog, ctx2, channel=None)
    assert ctx2.respond.call_args is not None
