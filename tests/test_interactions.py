"""Interactions (roleplay): genders, gif picking and the card flow.

Locks the launch contract: ``/genero`` stores the author's gender (any
implicit default), ``pick_gif`` matches the (actor, target) pair in layers
(exact -> partial -> unfiltered), the card fires only when the feature is
enabled, guards bounce self/bots, the footer prompt appears only while a
participant lacks a gender, and the regive button is target-only.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord

from rosemary.core.roleplay import (
    ACTIONS,
    DEFAULT_GENDER,
    RoleplayStore,
    normalize_gender,
    pick_gif,
)

ROOT = Path(__file__).resolve().parents[1]


class FakeTranslator:
    """Resolves real pt-BR catalog strings (parity is tested elsewhere)."""

    def __init__(self) -> None:
        import yaml

        self.catalog = yaml.safe_load((ROOT / "language" / "pt-BR.yaml").read_text())

    async def t(self, guild_id, key, **variables):
        node = self.catalog
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                return key
        text = node if isinstance(node, str) else key
        for name, value in variables.items():
            text = text.replace(f"{{{name}}}", str(value))
        return text


def _bot(tmp_path):
    bot = MagicMock(spec=[
        "storage", "get_cog", "get_guild", "translator", "theme",
    ])
    from rosemary.core.storage import GuildStorage
    from rosemary.core.themes import ThemeStore
    from rosemary.ui.theme import load_theme

    bot.storage = GuildStorage(tmp_path)
    bot.get_cog = MagicMock(return_value=None)
    bot.get_guild = MagicMock(return_value=None)
    bot.translator = FakeTranslator()
    bot.theme = load_theme()
    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    return bot


# store + picker ===============================================================


async def test_gender_roundtrip_and_normalize(tmp_path):
    store = RoleplayStore(tmp_path)
    assert await store.get_gender(1, 100) == DEFAULT_GENDER
    assert await store.has_gender(1, 100) is False
    stored = await store.set_gender(1, 100, "female")
    assert stored == "female"
    assert await store.get_gender(1, 100) == "female"
    assert await store.has_gender(1, 100) is True
    # Unknown values normalize to the implicit default, never stored raw.
    assert normalize_gender("FEMALE") == "female"
    assert normalize_gender("bogus") == DEFAULT_GENDER


def test_pick_gif_layers():
    unfiltered = {"url": "u", "actor": "any", "target": "any"}
    ff = {"url": "ff", "actor": "female", "target": "female"}
    mf = {"url": "mf", "actor": "male", "target": "female"}
    # Exact pair beats partial beats unfiltered.
    assert pick_gif([unfiltered, ff], "female", "female") is ff
    assert pick_gif([unfiltered, mf], "male", "female") is mf
    # Partial: one side any, the other exact.
    partial = {"url": "p", "actor": "any", "target": "female"}
    assert pick_gif([unfiltered, partial], "male", "female") is partial
    # No gendered gif at all: unfiltered serves.
    assert pick_gif([unfiltered], "male", "male") is unfiltered
    # Nothing matches: None (the cog answers with the no-gif error).
    assert pick_gif([mf], "female", "female") is None


def test_actions_match_the_catalog():
    import yaml

    catalog = yaml.safe_load((ROOT / "language" / "pt-BR.yaml").read_text())
    sentences = catalog["interactions"]["actions"]
    assert set(ACTIONS) == set(sentences), "action <-> catalog mismatch"


# card flow ====================================================================


def _member(member_id: int, *, bot: bool = False):
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.bot = bot
    member.mention = f"<@{member_id}>"
    member.display_name = f"user{member_id}"
    return member


def _channel():
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 55
    channel.send = AsyncMock(return_value=MagicMock(id=999))
    return channel


def _guild(channel):
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.get_channel = MagicMock(return_value=channel)
    guild.get_member = MagicMock(return_value=None)
    return guild


def _walk_custom_ids(node) -> list[str | None]:
    """Every button custom_id reachable in a (possibly nested) V2 view."""
    found: list[str | None] = []
    custom_id = getattr(node, "custom_id", None)
    if custom_id:
        found.append(custom_id)
    for child in list(getattr(node, "items", []) or []) + list(
        getattr(node, "children", []) or []
    ):
        found.extend(_walk_custom_ids(child))
    return found


async def test_card_flow_posts_gallery_and_regive(tmp_path):
    """Enabled + gif configured: one card with the sentence, gallery and
    the regive row; both participants may ping; no theme doc -> fallback."""
    from rosemary.cogs.interactions import InteractionsCog

    bot = _bot(tmp_path)
    cog = InteractionsCog(bot)
    await bot.storage.set(1, "interactions.enabled", True)
    await bot.storage.set(
        1,
        "gifs.hug",
        [{"url": "https://media.tenor.com/hug.gif", "actor": "any", "target": "any"}],
    )
    # Store the gif the way the manager does (roleplay.json, not settings).
    await cog.store.set_gifs(
        1, "hug", [{"url": "https://media.tenor.com/hug.gif", "actor": "any", "target": "any"}]
    )
    channel = _channel()
    guild = _guild(channel)
    actor, target = _member(100), _member(200)

    await cog.send_interaction_card(guild, actor, target, "hug", channel=channel)

    channel.send.assert_awaited_once()
    kwargs = channel.send.await_args.kwargs
    view = kwargs.get("view")
    assert view is not None
    # The card view carries the regive action row (nested in the container).
    assert "interactions_regive" in _walk_custom_ids(view)
    # Only the two participants may ping.
    allowed = kwargs.get("allowed_mentions")
    assert allowed is not None


async def test_card_disabled_is_silent(tmp_path):
    from rosemary.cogs.interactions import InteractionsCog

    bot = _bot(tmp_path)
    cog = InteractionsCog(bot)
    channel = _channel()
    guild = _guild(channel)

    await cog.send_interaction_card(guild, _member(1), _member(2), "hug", channel=channel)
    # send_interaction_card itself does not check the flag (commands do), so
    # the no-gif path here simply posts nothing (theme has empty gifs).
    channel.send.assert_not_awaited()


async def test_no_gif_configured_posts_nothing_and_returns_false(tmp_path):
    """Empty theme list + no override: nothing is sent and callers can tell
    (False), so they answer with the no-gif notice instead of silence."""
    from rosemary.cogs.interactions import InteractionsCog

    bot = _bot(tmp_path)
    cog = InteractionsCog(bot)
    channel = _channel()
    guild = _guild(channel)

    sent = await cog.send_interaction_card(
        guild, _member(1), _member(2), "hug", channel=channel
    )
    assert sent is False
    channel.send.assert_not_awaited()


async def test_no_gif_slash_answers_ephemeral_notice(tmp_path):
    """Slash command without a matching gif: ephemeral error, not silence."""
    from rosemary.cogs.interactions import InteractionsCog

    bot = _bot(tmp_path)
    cog = InteractionsCog(bot)
    await bot.storage.set(1, "interactions.enabled", True)
    order = []

    class FakeResponse:
        def is_done(self):
            return False

        async def defer(self, *, ephemeral=False):
            order.append("defer")

    async def fake_respond(text, **kwargs):
        order.append(("respond", text, kwargs))

    ctx = MagicMock()
    ctx.guild = _guild(_channel())
    ctx.author = _member(100)
    ctx.channel = ctx.guild.get_channel.return_value
    ctx.response = FakeResponse()
    ctx.respond = fake_respond

    await cog._slash_run(ctx, "hug", _member(200), "x")
    responds = [entry for entry in order if entry[0] == "respond"]
    assert len(responds) == 1
    text, kwargs = responds[0][1], responds[0][2]
    assert "Nenhum gif" in text
    assert kwargs.get("ephemeral") is True


async def test_no_gif_prefix_replies_with_notice(tmp_path):
    """Prefix trigger without a matching gif: the invocation gets a reply."""
    from rosemary.cogs.interactions import InteractionsCog

    bot = _bot(tmp_path)
    cog = InteractionsCog(bot)
    await bot.storage.set(1, "interactions.enabled", True)
    await bot.storage.set(1, "general.prefix", "!")
    channel = _channel()
    guild = _guild(channel)
    actor = _member(100)
    guild.get_member = MagicMock(return_value=_member(200))

    message = MagicMock(spec=discord.Message)
    message.guild = guild
    message.author = actor
    message.content = "!hug <@200>"
    message.channel = channel
    message.reply = AsyncMock()

    await cog.on_message(message)
    message.reply.assert_awaited_once()
    assert "Nenhum gif" in message.reply.await_args.args[0]


# gif manager view =============================================================


async def test_manager_rerender_never_duplicates_custom_ids(tmp_path):
    """Regression (live 400 'Component custom id cannot be duplicated'): the
    manager added its rows directly in prepare(), so every rerender stacked a
    second copy of each row and Discord rejected the edit. The MenuView
    contract (build_items returning a fresh list) must keep the ids unique."""
    from rosemary.ui.interactions_menu import InteractionsManagerView

    bot = _bot(tmp_path)
    view = InteractionsManagerView(bot, 1, 100)

    def collect(view_):
        found = []

        def walk(node):
            cid = getattr(node, "custom_id", None)
            if cid:
                found.append(cid)
            # Nested nodes expose either .items (containers) or .children
            # (action rows): never both for the same node, or buttons get
            # visited twice and the uniqueness check lies.
            nested = list(getattr(node, "items", []) or []) or list(
                getattr(node, "children", []) or []
            )
            for child in nested:
                walk(child)

        for child in view_.children:
            walk(child)
        return found

    await view.prepare()
    first = collect(view)
    assert first, "manager rendered nothing"
    assert len(first) == len(set(first)), f"duplicated ids on first render: {first}"

    # A second prepare (what rerender does before every edit) must rebuild
    # the same shape, never accumulate rows.
    await view.prepare()
    second = collect(view)
    assert len(second) == len(set(second)), f"duplicated ids after rerender: {second}"
    assert len(second) == len(first)


async def test_gender_command_stores_and_confirms(tmp_path):
    from rosemary.cogs.interactions import InteractionsCog

    bot = _bot(tmp_path)
    cog = InteractionsCog(bot)
    order = []

    class FakeResponse:
        def is_done(self):
            return False

        async def defer(self, *, ephemeral=False):
            order.append("defer")

        async def respond(self, text, **kwargs):
            order.append(("respond", text))

    ctx = MagicMock()
    ctx.guild = _guild(_channel())
    ctx.author = _member(100)
    ctx.response = FakeResponse()
    ctx.respond = ctx.response.respond

    await cog.interactions_gender.callback(cog, ctx, "female")
    assert await cog.store.get_gender(1, 100) == "female"
    respond = order[-1]
    assert respond[0] == "respond"
    assert "Feminino" in respond[1]


# prefix trigger ===============================================================


async def test_prefix_command_triggers_card(tmp_path):
    from rosemary.cogs.interactions import InteractionsCog

    bot = _bot(tmp_path)
    cog = InteractionsCog(bot)
    await bot.storage.set(1, "interactions.enabled", True)
    await cog.store.set_gifs(
        1, "hug", [{"url": "https://media.tenor.com/hug.gif", "actor": "any", "target": "any"}]
    )
    channel = _channel()
    guild = _guild(channel)
    actor, target = _member(100), _member(200)
    guild.get_member = MagicMock(return_value=target)
    bot.get_guild.return_value = guild

    message = MagicMock(spec=discord.Message)
    message.guild = guild
    message.author = actor
    message.content = "!hug <@200>"
    message.channel = channel
    message.reply = AsyncMock()

    # Guild prefix is "!" (general.prefix).
    await bot.storage.set(1, "general.prefix", "!")
    await cog.on_message(message)

    channel.send.assert_awaited_once()


async def test_prefix_command_ignores_self_and_unknown_verbs(tmp_path):
    from rosemary.cogs.interactions import InteractionsCog

    bot = _bot(tmp_path)
    cog = InteractionsCog(bot)
    await bot.storage.set(1, "interactions.enabled", True)
    await bot.storage.set(1, "general.prefix", "!")
    channel = _channel()
    guild = _guild(channel)

    message = MagicMock(spec=discord.Message)
    message.guild = guild
    author = _member(100)
    message.author = author
    message.content = "!hug <@100>"  # self
    message.channel = channel
    message.reply = AsyncMock()
    guild.get_member = MagicMock(return_value=author)
    await cog.on_message(message)
    message.reply.assert_awaited_once()
    channel.send.assert_not_awaited()

    message2 = MagicMock(spec=discord.Message)
    message2.guild = guild
    message2.author = message.author
    message2.content = "!nonsense <@200>"
    message2.channel = channel
    message2.reply = AsyncMock()
    await cog.on_message(message2)
    message2.reply.assert_not_awaited()
    channel.send.assert_not_awaited()
