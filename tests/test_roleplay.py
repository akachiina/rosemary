"""Roleplay (roleplay): gif sources, resolution layers and the card flow.

Locks the relaunched contract: gif resolution in layers (guild override ->
theme -> nekos.best default category), random pick inside a configured
list, the card fires only when the feature is enabled, guards bounce
self/bots, the no-source path answers the caller (False) so users get the
notice, and the regive dispatcher resolves its pair from the message id.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord

from rosemary.core.roleplay import (
    ACTIONS,
    RoleplayStore,
    category_for,
    pick_gif,
    resolve_gif,
    valid_api_category,
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
        "add_view", "add_application_command", "pending_application_commands",
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
    bot.add_view = MagicMock()
    bot.add_application_command = MagicMock()
    return bot


# store + resolution ===========================================================


async def test_store_gif_roundtrip(tmp_path):
    store = RoleplayStore(tmp_path)
    assert await store.get_gifs(1, "hug") == []
    await store.set_gifs(1, "hug", [{"url": "https://a.gif"}, {"api": "kiss"}])
    gifs = await store.get_gifs(1, "hug")
    assert gifs[0]["url"] == "https://a.gif"
    assert gifs[1]["api"] == "kiss"
    overrides = await store.all_overrides(1)
    assert "hug" in overrides


def test_pick_gif_random_member_of_list():
    gifs = [{"url": "a"}, {"url": "b"}]
    for _ in range(20):
        assert pick_gif(gifs) in gifs
    assert pick_gif([]) is None
    assert pick_gif([{"bogus": 1}]) is None


def test_default_categories_cover_actions():
    for action in ACTIONS:
        assert category_for(action), f"{action} has no default category"
    # attack has no nekos.best category; kick is the stand-in.
    assert category_for("attack") == "kick"
    assert category_for("nope") is None


def test_api_category_validation_is_shape_only():
    assert valid_api_category("hug")
    assert valid_api_category("highfive")
    assert not valid_api_category("has space")
    assert not valid_api_category("")
    assert not valid_api_category("uñ")


async def test_resolve_static_url_is_not_api_flagged(tmp_path):
    url, from_api = await resolve_gif([{"url": "https://a.gif"}], None)
    assert url == "https://a.gif"
    assert from_api is False


async def test_resolve_api_entry_fetches_category(tmp_path):
    from unittest.mock import patch

    from rosemary.core import roleplay

    with patch.object(roleplay, "fetch_nekos_gif", return_value="https://n.gif"):
        url, from_api = await resolve_gif([{"api": "hug"}], None)
    assert url == "https://n.gif"
    assert from_api is True


async def test_resolve_mixed_list_picks_one_entry(tmp_path):
    """A mixed list randomly lands on url (static) or api (fetched): the
    result is always one of the two sources, never a crash."""
    from unittest.mock import patch

    from rosemary.core import roleplay

    with patch.object(roleplay, "fetch_nekos_gif", return_value="https://n.gif"):
        url, _ = await resolve_gif([{"url": "https://a.gif"}, {"api": "hug"}], "hug")
    assert url in ("https://a.gif", "https://n.gif")


async def test_resolve_falls_through_to_default_category(tmp_path):
    """Empty configured list: the action's default nekos.best category is
    fetched (mocked here); the result is marked as api-driven."""
    from unittest.mock import patch

    from rosemary.core import roleplay

    # The module imports aiohttp at the top; resolution itself must not need
    # a session when the fetch is patched out.
    with patch.object(roleplay, "fetch_nekos_gif", return_value="https://n.gif"):
        url, from_api = await resolve_gif([], roleplay.category_for("hug"))
    assert url == "https://n.gif"
    assert from_api is True


async def test_resolve_returns_none_when_everything_fails(tmp_path):
    from unittest.mock import patch

    from rosemary.core import roleplay

    with patch.object(roleplay, "fetch_nekos_gif", return_value=None):
        url, from_api = await resolve_gif([], "hug")
    assert url is None
    assert from_api is True


async def test_resolve_api_entry_failure_falls_to_category(tmp_path):
    from unittest.mock import patch

    from rosemary.core import roleplay

    calls = []

    async def fake_fetch(category):
        calls.append(category)
        return "https://n.gif" if category == "hug" else None

    with patch.object(roleplay, "fetch_nekos_gif", side_effect=fake_fetch):
        url, from_api = await resolve_gif([{"api": "dead"}], "hug")
    assert url == "https://n.gif"
    assert from_api is True
    assert calls == ["dead", "hug"]


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


async def test_action_subcommands_attached_to_group(tmp_path):
    """One subcommand per action rides on the shared group, attached at import
    (py-cord copies the group at class-definition time, so anything attached
    later would never reach the registered copy)."""
    from discord.ext import commands

    from rosemary.cogs.roleplay import RoleplayCog, roleplay_group
    from rosemary.core.storage import GuildStorage
    from rosemary.ui.theme import load_theme

    bot = commands.Bot(command_prefix=None)
    bot.storage = GuildStorage(tmp_path)
    bot.theme = load_theme()
    bot.add_cog(RoleplayCog(bot))
    by_name = {c.name: c for c in roleplay_group.subcommands}
    assert set(ACTIONS) <= set(by_name)
    registered = {c.name for c in bot.pending_application_commands}
    assert "roleplay" in registered
    assert not (set(ACTIONS) & registered), "subcommands must not be top-level"


async def test_start_registers_dispatcher_with_real_children(tmp_path):
    """The persistent dispatcher must carry its action row when registered:
    py-cord's ViewStore indexes only walk_children() results, so an empty
    view registers nothing and every click dies as "did not respond" (the
    colors-panel lesson)."""
    from rosemary.cogs.roleplay import RoleplayCog

    bot = _bot(tmp_path)
    cog = RoleplayCog(bot)
    bot.add_view = MagicMock()
    bot.add_application_command = MagicMock()
    await cog.start()
    bot.add_view.assert_called_once()
    registered = bot.add_view.call_args.args[0]
    assert list(registered.walk_children()), "dispatcher registered with no children"


async def test_card_flow_posts_gallery_and_regive(tmp_path):
    """Enabled + static gif configured: one card with the sentence, gallery
    and the regive row; the message id lands in the regive table."""
    from rosemary.cogs.roleplay import _REGIVE_PREFIX, RoleplayCog

    bot = _bot(tmp_path)
    cog = RoleplayCog(bot)
    await bot.storage.set(1, "roleplay.enabled", True)
    await cog.store.set_gifs(1, "hug", [{"url": "https://media.tenor.com/hug.gif"}])
    channel = _channel()
    guild = _guild(channel)
    actor, target = _member(100), _member(200)

    sent = await cog.send_interaction_card(guild, actor, target, "hug", channel=channel)
    assert sent is True
    channel.send.assert_awaited_once()
    kwargs = channel.send.await_args.kwargs
    view = kwargs.get("view")
    assert view is not None
    # The card view carries the regive action row (nested in the container).
    assert _REGIVE_PREFIX in _walk_custom_ids(view)
    # The pair is remembered for the dispatcher.
    context = await cog.store.get_regive(1, 999)
    assert context == {"action": "hug", "actor": 100, "target": 200}


async def test_theme_api_seed_posts_without_configuration(tmp_path):
    """No guild override: the theme's ``api`` seed fetches a fresh gif (the
    default theme seeds every action with one)."""
    from unittest.mock import patch

    from rosemary.cogs.roleplay import RoleplayCog
    from rosemary.core import roleplay

    bot = _bot(tmp_path)
    cog = RoleplayCog(bot)
    channel = _channel()
    guild = _guild(channel)

    with patch.object(roleplay, "fetch_nekos_gif", return_value="https://n.gif"):
        sent = await cog.send_interaction_card(
            guild, _member(1), _member(2), "hug", channel=channel
        )
    assert sent is True
    channel.send.assert_awaited_once()
    # The resolved gif url IS the source: even an API gif carries the link.
    sent_view = channel.send.await_args.kwargs.get("view")
    assert [
        item for item in sent_view.walk_children() if getattr(item, "url", None)
    ]


async def test_no_source_anywhere_returns_false(tmp_path):
    """Offline API + nothing configured: nothing is sent and callers can tell
    (False), so they answer with the no-gif notice instead of silence."""
    from unittest.mock import patch

    from rosemary.cogs.roleplay import RoleplayCog
    from rosemary.core import roleplay

    bot = _bot(tmp_path)
    cog = RoleplayCog(bot)
    channel = _channel()
    guild = _guild(channel)

    with patch.object(roleplay, "fetch_nekos_gif", return_value=None):
        sent = await cog.send_interaction_card(
            guild, _member(1), _member(2), "hug", channel=channel
        )
    assert sent is False
    channel.send.assert_not_awaited()


async def test_no_gif_slash_answers_ephemeral_notice(tmp_path):
    """Slash command without a working gif source: ephemeral error, not silence."""
    from unittest.mock import patch

    from rosemary.cogs.roleplay import RoleplayCog
    from rosemary.core import roleplay

    bot = _bot(tmp_path)
    cog = RoleplayCog(bot)
    await bot.storage.set(1, "roleplay.enabled", True)
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

    with patch.object(roleplay, "fetch_nekos_gif", return_value=None):
        await cog._slash_run(ctx, "hug", _member(200))
    responds = [entry for entry in order if entry[0] == "respond"]
    assert len(responds) == 1
    text, kwargs = responds[0][1], responds[0][2]
    assert "fonte de gif" in text
    assert kwargs.get("ephemeral") is True


async def test_no_gif_prefix_replies_with_notice(tmp_path):
    """Prefix trigger without a working gif source: the invocation gets a reply."""
    from unittest.mock import patch

    from rosemary.cogs.roleplay import RoleplayCog
    from rosemary.core import roleplay

    bot = _bot(tmp_path)
    cog = RoleplayCog(bot)
    await bot.storage.set(1, "roleplay.enabled", True)
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

    with patch.object(roleplay, "fetch_nekos_gif", return_value=None):
        await cog.on_message(message)
    message.reply.assert_awaited_once()
    assert "fonte de gif" in message.reply.await_args.args[0]


async def test_static_gif_adds_source_link(tmp_path):
    """A fixed gif carries the Fonte link button; an API gif does not."""
    from rosemary.cogs.roleplay import RoleplayCog

    bot = _bot(tmp_path)
    cog = RoleplayCog(bot)
    await cog.store.set_gifs(1, "hug", [{"url": "https://media.tenor.com/hug.gif"}])
    channel = _channel()
    guild = _guild(channel)

    await cog.send_interaction_card(guild, _member(100), _member(200), "hug", channel=channel)
    sent_view = channel.send.await_args.kwargs.get("view")
    custom_ids = _walk_custom_ids(sent_view)
    assert "roleplay_regive" in custom_ids
    link_buttons = [
        item
        for item in sent_view.walk_children()
        if getattr(item, "url", None) == "https://media.tenor.com/hug.gif"
    ]
    assert link_buttons, "static gif must carry a source link button"


# gif manager view =============================================================


async def test_manager_rerender_never_duplicates_custom_ids(tmp_path):
    """Regression (live 400 'Component custom id cannot be duplicated'): the
    manager added its rows directly in prepare(), so every rerender stacked a
    second copy of each row and Discord rejected the edit. The MenuView
    contract (build_items returning a fresh list) must keep the ids unique."""
    from rosemary.ui.roleplay_menu import RoleplayManagerView

    bot = _bot(tmp_path)
    view = RoleplayManagerView(bot, 1, 100)

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


async def test_manager_add_modal_validates_and_persists(tmp_path):
    """The add modal stores url and/or api entries; garbage is rejected."""
    from rosemary.ui.roleplay_menu import RoleplayManagerView

    bot = _bot(tmp_path)
    view = RoleplayManagerView(bot, 1, 100)

    class FakeResponse:
        def is_done(self):
            return False

        async def defer(self, *, ephemeral=True):
            pass

    async def fake_respond(text, **kwargs):
        fake_respond.last = (text, kwargs)

    fake_respond.last = ("", {})
    interaction = MagicMock()
    interaction.response = FakeResponse()
    interaction.response.send_message = fake_respond
    interaction.edit = AsyncMock()

    # Garbage: neither a url nor a category.
    await view._add_submit(interaction, "nada", "tem espaço")
    assert "Preencha" in fake_respond.last[0]
    assert await view.store.get_gifs(1, "hug") == []

    # A url (no category) is stored as a static entry.
    await view._add_submit(interaction, "https://a.gif", "")
    gifs = await view.store.get_gifs(1, "hug")
    assert gifs[-1] == {"url": "https://a.gif"}

    # A category (no url) is stored as an api entry.
    await view._add_submit(interaction, "", "hug")
    gifs = await view.store.get_gifs(1, "hug")
    assert gifs[-1] == {"api": "hug"}

    # Both: one entry carrying the pair.
    await view._add_submit(interaction, "https://b.gif", "kiss")
    gifs = await view.store.get_gifs(1, "hug")
    assert gifs[-1] == {"url": "https://b.gif", "api": "kiss"}


async def test_manager_remove_and_restore(tmp_path):
    from rosemary.ui.roleplay_menu import RoleplayManagerView

    bot = _bot(tmp_path)
    view = RoleplayManagerView(bot, 1, 100)
    await view.store.set_gifs(1, "hug", [{"url": "https://a.gif"}, {"api": "kiss"}])

    class FakeResponse:
        def is_done(self):
            return False

        async def defer(self, *, ephemeral=True):
            pass

    interaction = MagicMock()
    interaction.response = FakeResponse()
    interaction.edit = AsyncMock()
    interaction.data = {"values": ["0"]}
    await view._remove(interaction)
    # Only index 0 went; the api entry stays.
    assert await view.store.get_gifs(1, "hug") == [{"api": "kiss"}]

    # Restore re-seeds from the theme (api entries in the default theme).
    interaction2 = MagicMock()
    interaction2.response = FakeResponse()
    interaction2.edit = AsyncMock()
    await view._restore(interaction2)
    gifs = await view.store.get_gifs(1, "hug")
    assert {"api": "hug"} in gifs


# prefix trigger ===============================================================


async def test_prefix_command_triggers_card(tmp_path):
    from rosemary.cogs.roleplay import RoleplayCog

    bot = _bot(tmp_path)
    cog = RoleplayCog(bot)
    await bot.storage.set(1, "roleplay.enabled", True)
    await cog.store.set_gifs(
        1, "hug", [{"url": "https://media.tenor.com/hug.gif"}]
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
    from rosemary.cogs.roleplay import RoleplayCog

    bot = _bot(tmp_path)
    cog = RoleplayCog(bot)
    await bot.storage.set(1, "roleplay.enabled", True)
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


async def test_roleplay_sentence_is_body_not_title_and_pings_both(tmp_path):
    """The titleless card opens with the action sentence, pinging both."""
    import rosemary.core.card_specs  # noqa: F401 (fills the card registry)
    from rosemary.cogs.roleplay import default_interaction_document
    from rosemary.core.card_service import render_card_message

    bot = _bot(tmp_path)
    variables = {
        "actor": "<@100>",
        "target": "<@200>",
        "actor_avatar": "https://a.b/x.png",
        "action": "abraçou",
        "gif_url": "https://media.tenor.com/hug.gif",
        "timestamp": "<t:1:R>",
    }
    doc = await default_interaction_document(bot, 1, **variables)
    bodies: list[str] = []

    def collect(node) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("body"), str):
                bodies.append(node["body"])
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(doc["blocks"])
    opening = bodies[0]
    assert "{@actor}" in opening and "{@target}" in opening
    assert not [b for b in bodies if "abraçou" in b and "{@actor}" not in b]

    _payload, allowed = await render_card_message(
        bot, 1, "roleplay.card", variables
    )
    pinged = {obj.id for obj in (allowed.users or [])}
    assert pinged == {100, 200}, allowed


def test_pick_gif_excludes_recent_urls():
    """No-repeat memory: excluded urls are skipped while anything else stays."""
    from rosemary.core.roleplay import pick_gif

    gifs = [{"url": "a"}, {"url": "b"}]
    for _ in range(20):
        assert pick_gif(gifs, ["a"]) == {"url": "b"}
    # Exhausted list repeats instead of sending nothing.
    assert pick_gif([{"url": "a"}], ["a"]) == {"url": "a"}
    # `api` entries carry no url yet: always eligible.
    assert pick_gif([{"api": "hug"}], ["a"]) == {"api": "hug"}
    assert pick_gif(gifs) in gifs


async def test_recent_memory_roundtrip_and_cap(tmp_path):
    """Recent urls persist per action, re-records rotate, oldest fall off."""
    from rosemary.core.roleplay import RECENT_HISTORY, RoleplayStore

    store = RoleplayStore(tmp_path)
    assert await store.get_recent(1, "hug") == []
    await store.remember_gif(1, "hug", "a")
    await store.remember_gif(1, "hug", "b")
    assert await store.get_recent(1, "hug") == ["a", "b"]
    await store.remember_gif(1, "hug", "a")
    assert await store.get_recent(1, "hug") == ["b", "a"]
    assert await store.get_recent(1, "kiss") == []
    for i in range(RECENT_HISTORY + 5):
        await store.remember_gif(1, "hug", f"u{i}")
    recent = await store.get_recent(1, "hug")
    assert len(recent) == RECENT_HISTORY
    assert recent[-1] == f"u{RECENT_HISTORY + 4}"


async def test_fetch_fresh_retries_then_accepts_repeat():
    """API collisions retry a few times; a stubborn repeat still sends."""
    from unittest.mock import patch

    from rosemary.core import roleplay

    with patch.object(
        roleplay, "fetch_nekos_gif", side_effect=["old", "old", "new"]
    ) as fetch:
        assert await roleplay._fetch_fresh("hug", {"old"}) == "new"
        assert fetch.await_count == 3
    with patch.object(roleplay, "fetch_nekos_gif", return_value="old"):
        assert await roleplay._fetch_fresh("hug", {"old"}) == "old"
    with patch.object(roleplay, "fetch_nekos_gif", return_value=None):
        assert await roleplay._fetch_fresh("hug", {"old"}) is None


async def test_send_with_no_repeat_avoids_last_gif(tmp_path):
    """Setting on: the just-used gif is skipped and the new one remembered."""
    from rosemary.cogs.roleplay import RoleplayCog

    bot = _bot(tmp_path)
    cog = RoleplayCog(bot)
    await bot.storage.set(1, "roleplay.enabled", True)
    await bot.storage.set(1, "roleplay.no_repeat_gifs", True)
    await cog.store.set_gifs(
        1, "hug", [{"url": "https://gifs/a.gif"}, {"url": "https://gifs/b.gif"}]
    )
    await cog.store.remember_gif(1, "hug", "https://gifs/a.gif")
    channel = _channel()
    guild = _guild(channel)

    sent = await cog.send_interaction_card(
        guild, _member(100), _member(200), "hug", channel=channel
    )
    assert sent is True
    kwargs = channel.send.await_args.kwargs
    gallery_urls = [
        gallery.items[0].media.url
        for gallery in kwargs["view"].walk_children()
        if isinstance(gallery, discord.ui.MediaGallery)
    ]
    assert gallery_urls == ["https://gifs/b.gif"], gallery_urls
    assert await cog.store.get_recent(1, "hug") == [
        "https://gifs/a.gif",
        "https://gifs/b.gif",
    ]


async def test_default_card_has_no_avatar(tmp_path):
    """The default roleplay card shows no user image (thumbnail dropped)."""
    from rosemary.cogs.roleplay import default_interaction_document

    bot = _bot(tmp_path)
    doc = await default_interaction_document(
        bot,
        1,
        actor="<@100>",
        target="<@200>",
        actor_avatar="https://a.b/x.png",
        action="abraçou",
        gif_url="https://media.tenor.com/hug.gif",
        timestamp="<t:1:R>",
    )

    def walk(node):
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from walk(item)

    blocks = list(walk(doc["blocks"]))
    assert not [b for b in blocks if b.get("type") == "thumbnail"], blocks
    assert not any("actor_avatar" in str(b) for b in blocks), blocks
