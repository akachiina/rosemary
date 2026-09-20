"""Cleaner: purge engine criteria, live panel states and validation gates.

The engine (``CleanerCog._purge``) walks channel history and deletes matches
(bulk under 14 days, single above) honoring author/channel filters and the
``last`` cap; the panel (``CleanerPurgeView``) is a three-state MenuView
(confirm, running with Stop, summary). These tests drive the real engine
against fake channels and render the real panel with real catalogs.
"""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord

import rosemary.cogs.cleaner  # noqa: F401  (imports clean, no future import)
from rosemary.cogs.cleaner import CleanerCog, CleanerPurgeView, PurgeRequest, PurgeResult
from rosemary.core.i18n import Translator
from rosemary.core.settings import set_setting
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore
from rosemary.ui.theme import load_theme


def make_bot(tmp_path):
    bot = MagicMock()
    bot.theme = load_theme()
    # Mirror bot.py: theme emojis are default format placeholders ({error}...).
    bot.translator = Translator(_path("language"), default_placeholders=bot.theme.emojis)
    bot.storage = GuildStorage(tmp_path)
    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    bot.get_cog = lambda name: None
    return bot


def _path(rel: str):
    from pathlib import Path

    return Path(rel)


def container_texts(view) -> list[str]:
    texts = []
    for node in view.walk_children():
        if isinstance(node, discord.ui.TextDisplay):
            texts.append(node.content)
    return texts


def _view_buttons(view) -> list[discord.ui.Button]:
    return [node for node in view.walk_children() if isinstance(node, discord.ui.Button)]


def _stamp(days_old: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days_old)


def make_message(content: str, *, author_id: int = 5, days_old: float = 1.0, mid: int = 0):
    message = MagicMock()
    message.id = mid
    message.content = content
    message.author.id = author_id
    message.created_at = _stamp(days_old)
    message.delete = AsyncMock()
    return message


def make_channel(name: str, messages: list, *, guild: MagicMock | None = None):
    channel = MagicMock()
    channel.name = name
    channel.id = abs(hash(name)) % 10**17
    channel.mention = f"<#{channel.id}>"
    channel.guild = guild if guild is not None else MagicMock()
    me = MagicMock()
    me.permissions.read_messages = True
    me.permissions.manage_messages = True
    channel.guild.me = me

    async def history(limit=None):
        for message in messages:
            yield message

    channel.history = history
    deleted_batches: list[list] = []
    deleted_singles: list = []

    async def delete_messages(batch):
        deleted_batches.append(list(batch))

    async def delete_single():
        deleted_singles.append(channel)

    channel.delete_messages = delete_messages
    channel.delete = delete_single
    channel.deleted_batches = deleted_batches
    channel.deleted_singles = deleted_singles
    return channel


def make_guild(channels: list):
    guild = MagicMock()
    guild.id = 1
    guild.text_channels = channels
    return guild


def make_request(guild_id: int = 1, **kwargs) -> PurgeRequest:
    kwargs.setdefault("mode", "word")
    kwargs.setdefault("word", "spam")
    kwargs.setdefault("criterion", "criterion text")
    return PurgeRequest(**kwargs)


async def _cog_with_channel(tmp_path, channels):
    bot = make_bot(tmp_path)
    cog = CleanerCog(bot)
    guild = make_guild(channels)
    return cog, bot, guild


#: validation gates -------------------------------------------------------------


class _FakeResponse:
    def __init__(self):
        self.calls: list[tuple] = []

    def is_done(self):
        return False

    async def defer(self, **kwargs):
        self.calls.append(("defer", kwargs))

    async def send_message(self, content=None, **kwargs):
        self.calls.append(("send", content, kwargs))


def make_ctx(guild, author, channel=None):
    ctx = MagicMock()
    ctx.guild = guild
    ctx.guild_id = guild.id
    ctx.author = author
    ctx.channel = channel or (guild.text_channels[0] if guild.text_channels else guild)
    ctx.response = _FakeResponse()
    ctx.respond = AsyncMock()
    return ctx


async def test_validation_errors(tmp_path):
    cog, bot, guild = await _cog_with_channel(tmp_path, [])
    author = MagicMock()
    author.id = 9
    author.mention = "<@9>"

    # No mode at all (catalog default is en-US for a guild without language).
    ctx = make_ctx(guild, author)
    await CleanerCog.limpar.callback(
        cog, ctx, palavra=None, regex=None, last=None, canal=None, autor=None
    )
    assert "Give me a word" in ctx.respond.call_args.args[0]

    # Word AND regex conflict.
    ctx = make_ctx(guild, author)
    await CleanerCog.limpar.callback(
        cog, ctx, palavra="x", regex="y", last=None, canal=None, autor=None
    )
    assert "OR" in ctx.respond.call_args.args[0]

    # Broken regex reports the reason, never crashes.
    ctx = make_ctx(guild, author)
    await CleanerCog.limpar.callback(
        cog, ctx, palavra=None, regex="([unclosed", last=None, canal=None, autor=None
    )
    sent = ctx.respond.call_args.args[0]
    assert "Invalid regex" in sent
    # The re.error detail must reach the user, not the placeholder alone.
    assert "{reason}" not in sent
    assert len(sent) > len("{error} Invalid regex: {reason}")


async def test_disabled_gate(tmp_path):
    bot = make_bot(tmp_path)
    cog = CleanerCog(bot)
    guild = make_guild([])
    await set_setting(bot.storage, 1, "cleaner.enabled", False)
    ctx = make_ctx(guild, MagicMock())
    await CleanerCog.limpar.callback(
        cog, ctx, palavra="x", regex=None, last=None, canal=None, autor=None
    )
    sent = ctx.respond.call_args.args[0]
    assert "desativada" in sent or "disabled" in sent


async def test_compose_criterion_covers_every_mode(tmp_path):
    """The criterion sentence names mode, author and scope explicitly: no
    implicit scope may reach the destructive confirmation without being
    written on the card."""
    cog = CleanerCog(make_bot(tmp_path))
    gid = 1
    canal = MagicMock()
    canal.mention = "<#3>"
    autor = MagicMock()
    autor.mention = "<@7>"

    word = await cog._compose_criterion(gid, "spam", None, None, None, None)
    assert word == 'with the word “spam” in every channel'

    regex = await cog._compose_criterion(
        gid, "", re.compile(r"^!\w+"), None, None, None
    )
    assert "`^!" + "\\w+`" in regex and "in every channel" in regex

    last_only = await cog._compose_criterion(gid, "", None, 50, None, None)
    assert "last 50 message(s)" in last_only

    last_word = await cog._compose_criterion(gid, "spam", None, 50, None, None)
    assert "last 50" in last_word and "spam" in last_word

    last_regex = await cog._compose_criterion(
        gid, "", None, 10, None, None
    )
    assert "last 10" in last_regex

    full = await cog._compose_criterion(gid, "spam", None, None, canal, autor)
    assert "<@7>" in full and "<#3>" in full


async def test_command_opens_confirm_panel(tmp_path):
    cog, bot, guild = await _cog_with_channel(tmp_path, [])
    author = MagicMock()
    author.id = 9
    ctx = make_ctx(guild, author)
    await CleanerCog.limpar.callback(
        cog, ctx, palavra="lixo", regex=None, last=None, canal=None, autor=None
    )
    kwargs = ctx.respond.call_args.kwargs
    assert kwargs.get("ephemeral") is True
    view = kwargs["view"]
    assert isinstance(view, CleanerPurgeView)
    texts = container_texts(view)
    assert any("lixo" in t for t in texts), texts
    assert not any("cleaner." in t for t in texts), texts  # no raw keys
    labels = [b.label for b in _view_buttons(view)]
    assert any("Apagar" in label or "Delete" in label for label in labels), labels


#: engine -----------------------------------------------------------------------


async def test_engine_word_author_and_cap(tmp_path):
    cog, bot, guild = await _cog_with_channel(
        tmp_path,
        [
            make_channel(
                "geral",
                [
                    make_message("spam um", author_id=5),
                    make_message("oi", author_id=5),
                    make_message("SPAM dois", author_id=7),
                    make_message("spam tres", author_id=5),
                ],
            )
        ],
    )
    request = make_request(mode="word", word="spam")
    progress = AsyncMock()
    cancel = asyncio.Event()
    moderator = MagicMock()
    moderator.id = 9
    result = await cog._purge(
        request, guild, moderator, progress=progress, cancel_event=cancel
    )
    assert result.deleted == 3
    assert result.matched == 3

    # Author filter: only member 5's matches.
    guild2 = make_guild(
        [
            make_channel(
                "geral",
                [
                    make_message("spam um", author_id=5),
                    make_message("SPAM dois", author_id=7),
                ],
            )
        ]
    )
    request = make_request(mode="word", word="spam", author_id=5)
    result = await cog._purge(
        request, guild2, moderator, progress=AsyncMock(), cancel_event=asyncio.Event()
    )
    assert result.deleted == 1
    assert result.matched == 1

    # last cap: stops exactly at N (first yielded message wins).
    canal_cap = make_channel(
        "geral",
        [
            make_message("spam novo", author_id=5, mid=2),
            make_message("spam antigo", author_id=5, mid=1),
        ],
    )
    guild3 = make_guild([canal_cap])
    request = make_request(mode="last", word="", last=1)
    result = await cog._purge(
        request, guild3, moderator, progress=AsyncMock(), cancel_event=asyncio.Event()
    )
    assert result.deleted == 1
    assert result.matched == 1
    assert canal_cap.deleted_batches and canal_cap.deleted_batches[0][0].content == "spam novo"


async def test_engine_scope_channel_regex_and_14day_rule(tmp_path):
    me = MagicMock()
    me.permissions.read_messages = True
    me.permissions.manage_messages = True
    alvo = make_channel("alvo", [make_message("!ban all", author_id=5)], guild=None)
    alvo.guild.me = me
    outro = make_channel("outro", [make_message("!kick all", author_id=5)], guild=None)
    outro.guild.me = me

    async def history(limit=None):
        for message in (
            make_message("!ban all", author_id=5),
            make_message("!mute x", author_id=5),
        ):
            yield message

    alvo.history = history
    guild = make_guild([alvo, outro])
    cog = CleanerCog(make_bot(tmp_path))
    pattern = re.compile(r"^!\w+", re.IGNORECASE)
    request = make_request(mode="regex", word="", pattern=pattern, channel=alvo)
    result = await cog._purge(
        request, guild, MagicMock(), progress=AsyncMock(), cancel_event=asyncio.Event()
    )
    assert result.deleted == 2, "scope must be the given channel only"
    assert alvo.deleted_batches or alvo.deleted_singles
    assert not outro.deleted_batches and not outro.deleted_singles

    # A 20-day-old match goes through single delete, not bulk.
    velho = make_message("spam velho", days_old=20)
    canal_velho = make_channel("velho", [velho], guild=None)
    canal_velho.guild.me = me
    guild2 = make_guild([canal_velho])
    request = make_request(mode="word", word="spam")
    await cog._purge(
        request, guild2, MagicMock(), progress=AsyncMock(), cancel_event=asyncio.Event()
    )
    assert not canal_velho.deleted_batches
    velho.delete.assert_awaited_once()


async def test_engine_cancel_stops_between_messages(tmp_path):
    me = MagicMock()
    me.permissions.read_messages = True
    me.permissions.manage_messages = True
    messages = [make_message(f"spam {i}", mid=i) for i in range(50)]
    channel = make_channel("geral", messages, guild=None)
    channel.guild.me = me

    async def history(limit=None):
        for message in messages:
            if len(calls) == 0:
                calls.append(1)
                cancel.set()
            yield message

    calls: list = []
    cancel = asyncio.Event()
    channel.history = history
    guild = make_guild([channel])
    cog = CleanerCog(make_bot(tmp_path))
    request = make_request(mode="word", word="spam")
    result = await cog._purge(
        request, guild, MagicMock(), progress=AsyncMock(), cancel_event=cancel
    )
    assert result.deleted == 0
    assert not channel.deleted_batches


async def test_engine_renews_audit_stamp_per_channel(tmp_path):
    """The purge stamp has a 600s TTL: long runs must renew it per channel."""
    from rosemary.cogs.audit import AuditCog

    bot = make_bot(tmp_path)
    audit = AuditCog(bot)
    bot.get_cog = lambda name: audit if name == "AuditCog" else None
    cog = CleanerCog(bot)
    stamps: list = []
    audit.note_purge_context = lambda guild, moderator: stamps.append(1)

    me = MagicMock()
    me.permissions.read_messages = True
    me.permissions.manage_messages = True
    channels = []
    for i in range(3):
        channel = make_channel(f"c{i}", [make_message("spam x", mid=i)], guild=None)
        channel.guild.me = me
        channels.append(channel)
    guild = make_guild(channels)
    request = make_request(mode="word", word="spam")
    await cog._purge(
        request,
        guild,
        MagicMock(),
        progress=AsyncMock(),
        cancel_event=asyncio.Event(),
    )
    assert len(stamps) == 3, "one stamp renewal per scanned channel"


#: panel ------------------------------------------------------------------------


def make_panel_bot(tmp_path):
    return make_bot(tmp_path)


def _panel(tmp_path):
    bot = make_panel_bot(tmp_path)
    request = make_request(mode="word", word="spam", criterion="com a palavra “spam”")
    view = CleanerPurgeView(bot, 1, owner_id=9, request=request)
    return bot, view


async def test_panel_states_render_with_real_catalog(tmp_path):
    _bot, view = _panel(tmp_path)

    # Confirm state: criterion + confirm text + Yes/Cancel (catalog default
    # is en-US for a guild without a language setting).
    await view.prepare()
    texts = container_texts(view)
    # The criterion sentence is carried verbatim onto the panel.
    assert any("com a palavra" in t for t in texts), texts
    assert not any("cleaner." in t for t in texts), texts
    labels = [b.label for b in _view_buttons(view)]
    assert any("Delete" in label for label in labels)
    assert any("Cancel" in label for label in labels)

    # Running state: counters + Stop.
    view.state = "running"
    view.started_at = 0.0
    view.current_channel = "<#3>"
    view.channels_done = 2
    view.channels_total = 5
    view.scanned = 120
    view.deleted = 7
    view.matched = 9
    await view.prepare()
    texts = container_texts(view)
    joined = "\n".join(texts)
    assert "2/5" in joined
    assert "120" in joined
    assert "7 of 9" in joined
    assert not any("cleaner." in t for t in texts), texts
    labels = [b.label for b in _view_buttons(view)]
    assert any("Stop" in label for label in labels)

    # Done state: the final summary text.
    view.state = "done"
    view.final_text = "pronto"
    await view.prepare()
    assert any("pronto" in t for t in container_texts(view))


async def test_panel_stop_sets_event_and_disables(tmp_path):
    _bot, view = _panel(tmp_path)
    view.state = "running"
    await view.prepare()

    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.is_done = lambda: False
    interaction.response.defer = AsyncMock()
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock()
    interaction.edit = AsyncMock()

    await view._stop(interaction)
    assert view.cancel_event.is_set()
    buttons = {b.custom_id: b for b in _view_buttons(view)}
    assert buttons["cleaner_stop"].disabled is True


async def test_panel_completion_reports_and_stamps_final_summary(tmp_path):
    bot, view = _panel(tmp_path)
    view.runner = AsyncMock(return_value=PurgeResult(deleted=4, matched=5))
    view.purge_guild = MagicMock()
    view.moderator = MagicMock()
    view.moderator.mention = "<@9>"
    view.moderator.id = 9

    sent_logs: list = []

    async def spy_send_channel_log(*args, **kwargs):
        sent_logs.append((args, kwargs))
        return True

    import rosemary.cogs.cleaner as cleaner_mod

    original = cleaner_mod.send_channel_log
    cleaner_mod.send_channel_log = spy_send_channel_log
    try:
        interaction = MagicMock()
        interaction.response = MagicMock()
        interaction.response.is_done = lambda: False
        interaction.response.defer = AsyncMock()
        interaction.edit = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        await view._confirm(interaction)
        await view._task
    finally:
        cleaner_mod.send_channel_log = original

    assert view.state == "done"
    assert view.deleted == 4
    assert view.matched == 5
    texts = container_texts(view)
    joined = "\n".join(texts)
    assert "4" in joined
    assert "com a palavra" in joined
    assert len(sent_logs) == 1, "one audit card for the whole run"
    log_kwargs = sent_logs[0][1]
    assert log_kwargs["card_key"] == "cleaner.logs.purged.description"
    assert log_kwargs["mention_user_ids"] == [9]
    # The audit body reports confirmed deletes against criteria matches
    # (send_channel_log args: bot, guild_id, title, description).
    assert "4 of 5" in sent_logs[0][0][3]


async def test_panel_stopped_summary_after_cancel(tmp_path):
    bot, view = _panel(tmp_path)

    async def runner(request, guild, moderator, *, progress, cancel_event):
        cancel_event.set()
        return PurgeResult(deleted=2, matched=2)

    view.runner = runner
    view.purge_guild = MagicMock()
    view.moderator = MagicMock()
    view.moderator.mention = "<@9>"
    view.moderator.id = 9

    import rosemary.cogs.cleaner as cleaner_mod

    original = cleaner_mod.send_channel_log
    cleaner_mod.send_channel_log = AsyncMock(return_value=True)
    try:
        interaction = MagicMock()
        interaction.response = MagicMock()
        interaction.response.is_done = lambda: False
        interaction.response.defer = AsyncMock()
        interaction.edit = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        await view._confirm(interaction)
        await view._task
    finally:
        cleaner_mod.send_channel_log = original

    assert view.state == "done"
    joined = "\n".join(container_texts(view))
    assert "2" in joined
    assert "interrompida" in joined or "stopped" in joined


async def test_panel_reports_matched_vs_deleted_gap(tmp_path):
    """Matches the API could not delete surface on the panel, honestly."""
    bot, view = _panel(tmp_path)
    view.runner = AsyncMock(return_value=PurgeResult(deleted=2, matched=5))
    view.purge_guild = MagicMock()
    view.moderator = MagicMock()
    view.moderator.mention = "<@9>"
    view.moderator.id = 9

    import rosemary.cogs.cleaner as cleaner_mod

    original = cleaner_mod.send_channel_log
    cleaner_mod.send_channel_log = AsyncMock(return_value=True)
    try:
        interaction = MagicMock()
        interaction.response = MagicMock()
        interaction.response.is_done = lambda: False
        interaction.response.defer = AsyncMock()
        interaction.edit = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        await view._confirm(interaction)
        await view._task
    finally:
        cleaner_mod.send_channel_log = original

    joined = "\n".join(container_texts(view))
    assert "could not be deleted" in joined
    assert "3" in joined
