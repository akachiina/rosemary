"""Broadcast session persistence, live relay and DM attachment delivery."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from rosemary.cogs.broadcast import BroadcastCog, _Session
from rosemary.core.broadcast import BroadcastSession, BroadcastStore
from rosemary.core.mass_dm import DmAttachment, fan_out
from rosemary.core.storage import GuildStorage
from rosemary.ui.theme import load_theme


def _bot(tmp_path):
    bot = MagicMock()
    bot.storage = GuildStorage(tmp_path)
    bot.theme = load_theme()
    bot.guilds = []
    bot.get_guild = MagicMock(return_value=None)
    return bot


def _member(member_id: int, *, bot: bool = False):
    member = MagicMock()
    member.id = member_id
    member.bot = bot
    member.roles = []
    member.send = AsyncMock()
    return member


async def test_store_roundtrip_ignores_runtime_fields(tmp_path):
    store = BroadcastStore(tmp_path)
    session = _Session(
        channel_id=10,
        author_id=20,
        title="Maintenance",
        target_role_id=30,
        delay_seconds=1,
        sent=4,
        failed=1,
        relayed=2,
        last_message_id=99,
        started_at="2026-10-21T12:00:00+00:00",
    )

    await store.save(1, session)
    path = tmp_path / "1" / "broadcast.json"
    raw = json.loads(path.read_text(encoding="utf-8"))["session"]
    assert "lock" not in raw
    assert "tasks" not in raw
    assert "cancelled" not in raw

    restored = await store.load(1)
    assert restored == BroadcastSession(
        channel_id=10,
        author_id=20,
        title="Maintenance",
        target_role_id=30,
        delay_seconds=1,
        sent=4,
        failed=1,
        relayed=2,
        last_message_id=99,
        started_at="2026-10-21T12:00:00+00:00",
    )

    await store.clear(1)
    assert await store.load(1) is None


class _DmMember:
    def __init__(self, *, bot: bool = False, fail_files: bool = False) -> None:
        self.bot = bot
        self.fail_files = fail_files
        self.calls: list[tuple[str | None, list[discord.File] | None]] = []

    async def send(self, *, content=None, files=None):
        if files and self.fail_files:
            raise discord.HTTPException(MagicMock(), "upload too large")
        self.calls.append((content, files))


async def test_fan_out_sends_real_files_and_falls_back_to_links():
    attachment = DmAttachment(
        filename="photo.png",
        data=b"image-bytes",
        url="https://cdn.example/photo.png",
    )
    ok = _DmMember()
    fallback = _DmMember(fail_files=True)
    skipped_bot = _DmMember(bot=True)
    progress: list[tuple[int, int]] = []

    sent, failed = await fan_out(
        [ok, fallback, skipped_bot],
        "Update",
        0,
        attachments=(attachment,),
        on_progress=lambda count_ok, count_failed: progress.append(
            (count_ok, count_failed)
        ),
    )

    assert (sent, failed) == (2, 0)
    assert progress == [(1, 0), (2, 0)]
    assert skipped_bot.calls == []
    content, files = ok.calls[0]
    assert content == "Update"
    assert files is not None and len(files) == 1
    assert files[0].filename == "photo.png"
    assert files[0].fp.getvalue() == b"image-bytes"
    assert fallback.calls == [
        ("Update\nhttps://cdn.example/photo.png", None)
    ]


async def test_start_restores_a_persisted_session(tmp_path):
    store = BroadcastStore(tmp_path)
    await store.save(
        1,
        BroadcastSession(
            channel_id=55,
            author_id=77,
            title="Maintenance",
            target_role_id=88,
            delay_seconds=3,
            sent=12,
            failed=2,
            relayed=3,
            last_message_id=900,
        ),
    )
    bot = _bot(tmp_path)
    bot.guilds = [SimpleNamespace(id=1)]
    cog = BroadcastCog(bot)

    await cog.start()

    session = cog._sessions[1]
    assert session.channel_id == 55
    assert session.author_id == 77
    assert session.target_role_id == 88
    assert session.delay_seconds == 3
    assert session.sent == 12
    assert session.relayed == 3


async def test_live_message_relays_downloaded_attachment(tmp_path):
    bot = _bot(tmp_path)
    target_a = _member(1001)
    target_b = _member(1002)
    guild = MagicMock()
    guild.id = 1
    guild.members = [target_a, target_b]
    bot.get_guild.return_value = guild

    cog = BroadcastCog(bot)
    cog._loaded.add(1)
    session = _Session(
        channel_id=55,
        author_id=77,
        title="Maintenance",
        delay_seconds=0,
    )
    cog._sessions[1] = session

    attachment = MagicMock()
    attachment.filename = "photo.png"
    attachment.url = "https://cdn.example/photo.png"
    attachment.size = 4
    attachment.read = AsyncMock(return_value=b"data")
    attachment.is_spoiler = MagicMock(return_value=False)
    message = MagicMock()
    message.id = 123
    message.guild = guild
    message.author = SimpleNamespace(id=77, bot=False)
    message.channel = SimpleNamespace(id=55)
    message.content = "Update"
    message.attachments = [attachment]
    message.add_reaction = AsyncMock()

    await cog.on_message(message)
    await asyncio.gather(*list(session.tasks))

    for member in (target_a, target_b):
        member.send.assert_awaited_once()
        kwargs = member.send.await_args.kwargs
        assert kwargs["content"] == "Update"
        files = kwargs["files"]
        assert files[0].filename == "photo.png"
        assert files[0].fp.getvalue() == b"data"
    assert session.relayed == 1
    assert session.sent == 2
    assert session.last_message_id == 123
    assert message.add_reaction.await_count == 2

    stored = await BroadcastStore(tmp_path).load(1)
    assert stored is not None
    assert stored.relayed == 1
    assert stored.sent == 2
    assert stored.last_message_id == 123


async def test_duplicate_cursor_message_is_not_relayed_again(tmp_path):
    bot = _bot(tmp_path)
    guild = MagicMock()
    guild.id = 1
    guild.members = [_member(1001)]
    bot.get_guild.return_value = guild

    cog = BroadcastCog(bot)
    cog._loaded.add(1)
    session = _Session(
        channel_id=55,
        author_id=77,
        title="Maintenance",
        delay_seconds=0,
        last_message_id=123,
    )
    cog._sessions[1] = session
    message = MagicMock()
    message.id = 123
    message.guild = guild
    message.author = SimpleNamespace(id=77, bot=False)
    message.channel = SimpleNamespace(id=55)
    message.content = "Update"
    message.attachments = []
    message.add_reaction = AsyncMock()

    await cog.on_message(message)

    assert session.tasks == set()
    message.add_reaction.assert_not_awaited()


@pytest.mark.parametrize("delay", [0, 30])
async def test_store_clamps_restored_delay(tmp_path, delay):
    store = BroadcastStore(tmp_path)
    await store.storage.set(
        1,
        "session",
        {
            "channel_id": 55,
            "author_id": 77,
            "title": "Maintenance",
            "delay_seconds": delay,
        },
    )
    restored = await store.load(1)
    assert restored is not None
    assert restored.delay_seconds == delay
