"""Persistent metadata for live broadcast sessions.

Each guild keeps at most one session in ``data/<guild_id>/broadcast.json``.
Runtime state (locks and tasks) lives in the cog; this module only owns the
JSON-safe payload so a restart can restore the same live session.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rosemary.core.storage import GuildStorage

_BROADCAST_FILE = "broadcast.json"


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@dataclass
class BroadcastSession:
    """JSON-safe state for one live broadcast session."""

    channel_id: int
    author_id: int
    title: str
    target_role_id: int | None = None
    delay_seconds: int = 2
    sent: int = 0
    failed: int = 0
    relayed: int = 0
    last_message_id: int | None = None
    started_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # Explicit fields: subclasses add runtime-only attributes (locks and
        # tasks) that must never reach the JSON document.
        return {
            "channel_id": int(self.channel_id),
            "author_id": int(self.author_id),
            "title": self.title,
            "target_role_id": (
                int(self.target_role_id) if self.target_role_id is not None else None
            ),
            "delay_seconds": int(self.delay_seconds),
            "sent": int(self.sent),
            "failed": int(self.failed),
            "relayed": int(self.relayed),
            "last_message_id": (
                int(self.last_message_id) if self.last_message_id is not None else None
            ),
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BroadcastSession | None:
        channel_id = _int_or_none(data.get("channel_id"))
        author_id = _int_or_none(data.get("author_id"))
        if channel_id is None or author_id is None:
            return None
        try:
            delay = int(data.get("delay_seconds", 2))
        except (TypeError, ValueError):
            delay = 2
        return cls(
            channel_id=channel_id,
            author_id=author_id,
            title=str(data.get("title") or ""),
            target_role_id=_int_or_none(data.get("target_role_id")),
            delay_seconds=max(0, min(30, delay)),
            sent=max(0, _int_or_none(data.get("sent")) or 0),
            failed=max(0, _int_or_none(data.get("failed")) or 0),
            relayed=max(0, _int_or_none(data.get("relayed")) or 0),
            last_message_id=_int_or_none(data.get("last_message_id")),
            started_at=(
                str(data["started_at"]) if data.get("started_at") is not None else None
            ),
        )


class BroadcastStore:
    """Per-guild broadcast session in its own ``broadcast.json``."""

    def __init__(self, data_dir: Path | str) -> None:
        self.storage = GuildStorage(
            Path(data_dir), filename=_BROADCAST_FILE, use_defaults=False
        )

    async def load(self, guild_id: int) -> BroadcastSession | None:
        doc = await self.storage.get(guild_id)
        raw = doc.get("session")
        if not isinstance(raw, dict):
            return None
        return BroadcastSession.from_dict(raw)

    async def save(self, guild_id: int, session: BroadcastSession) -> None:
        await self.storage.set(guild_id, "session", session.to_dict())

    async def clear(self, guild_id: int) -> None:
        await self.storage.delete_keys(guild_id, "session")
