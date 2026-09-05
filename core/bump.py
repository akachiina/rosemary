"""Persistence and helpers for the Bump system.

Data lives per guild in its own JSON file (``data/<guild_id>/bump.json``).
It holds three main sections:
* ``reminder``: State of the last bump and anti-camping locks.
* ``leaderboard``: Bump counts for the current week.
* ``winner``: History and saved customizations for the Bump-MVP.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rosemary.core.storage import GuildStorage

log = logging.getLogger(__name__)

_BUMP_FILE = "bump.json"


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class BumpStore:
    """Manages bump reminder and leaderboard persistence."""

    def __init__(self, data_dir: Path | str) -> None:
        self.storage = GuildStorage(Path(data_dir), filename=_BUMP_FILE, use_defaults=False)

    async def _update(self, guild_id: int, key: str, data: Any) -> None:
        doc = await self.storage.get(guild_id)
        doc[key] = data
        await self.storage.set_all(guild_id, doc)

    # -- Reminder State ------------------------------------------------------

    async def get_reminder(self, guild_id: int) -> dict[str, Any]:
        doc = await self.storage.get(guild_id)
        return doc.get("reminder") or {
            "last_bump_timestamp": None,
            "last_bump_user_id": None,
            "reminder_sent": True,
            "channel_locked": False,
            "week_start": datetime.now(UTC).isoformat(),
        }

    async def _set_reminder(self, guild_id: int, data: dict[str, Any]) -> None:
        await self._update(guild_id, "reminder", data)

    async def record_bump(self, guild_id: int, user_id: int) -> bool:
        """Record a bump. Returns False if the bump was stale (replayed message)."""
        state = await self.get_reminder(guild_id)
        existing = parse_datetime(state.get("last_bump_timestamp"))
        new_time = datetime.now(UTC)
        if existing and new_time <= existing:
            return False
        state["last_bump_timestamp"] = new_time.isoformat()
        state["last_bump_user_id"] = user_id
        state["reminder_sent"] = False
        await self._set_reminder(guild_id, state)
        return True

    async def get_last_bump_time(self, guild_id: int) -> datetime | None:
        state = await self.get_reminder(guild_id)
        return parse_datetime(state.get("last_bump_timestamp"))

    async def mark_reminder_sent(self, guild_id: int) -> None:
        state = await self.get_reminder(guild_id)
        state["reminder_sent"] = True
        await self._set_reminder(guild_id, state)

    async def mark_channel_locked(self, guild_id: int) -> None:
        state = await self.get_reminder(guild_id)
        state["channel_locked"] = True
        await self._set_reminder(guild_id, state)

    async def mark_channel_unlocked(self, guild_id: int) -> None:
        state = await self.get_reminder(guild_id)
        state["channel_locked"] = False
        await self._set_reminder(guild_id, state)

    async def get_lock_message_id(self, guild_id: int) -> int | None:
        state = await self.get_reminder(guild_id)
        return state.get("lock_message_id")

    async def set_lock_message_id(self, guild_id: int, message_id: int | None) -> None:
        state = await self.get_reminder(guild_id)
        state["lock_message_id"] = message_id
        await self._set_reminder(guild_id, state)

    async def is_channel_locked(self, guild_id: int) -> bool:
        state = await self.get_reminder(guild_id)
        return state.get("channel_locked", False)

    async def get_week_start(self, guild_id: int) -> datetime | None:
        state = await self.get_reminder(guild_id)
        if "week_start" not in state:
            state["week_start"] = datetime.now(UTC).isoformat()
            await self._set_reminder(guild_id, state)
        return parse_datetime(state.get("week_start"))

    async def set_week_start(self, guild_id: int, start_time: datetime) -> None:
        state = await self.get_reminder(guild_id)
        state["week_start"] = start_time.isoformat()
        await self._set_reminder(guild_id, state)

    # -- Leaderboard State ---------------------------------------------------

    async def get_leaderboard(self, guild_id: int) -> dict[str, int]:
        doc = await self.storage.get(guild_id)
        return doc.get("leaderboard") or {}

    async def add_bump(self, guild_id: int, user_id: int) -> int:
        doc = await self.storage.get(guild_id)
        lb = doc.get("leaderboard") or {}
        uid = str(user_id)
        lb[uid] = lb.get(uid, 0) + 1
        doc["leaderboard"] = lb
        await self.storage.set_all(guild_id, doc)
        return lb[uid]

    async def reset_week(self, guild_id: int) -> None:
        doc = await self.storage.get(guild_id)
        doc["leaderboard"] = {}
        await self.storage.set_all(guild_id, doc)
        state = await self.get_reminder(guild_id)
        state["week_start"] = datetime.now(UTC).isoformat()
        await self._set_reminder(guild_id, state)

    # -- Winner State --------------------------------------------------------

    async def get_winner_data(self, guild_id: int) -> dict[str, Any]:
        doc = await self.storage.get(guild_id)
        return doc.get("winner") or {
            "current_winner": None,
            "wins": {},
            "customizations": {},
        }

    async def _set_winner_data(self, guild_id: int, data: dict[str, Any]) -> None:
        await self._update(guild_id, "winner", data)

    async def get_current_winner(self, guild_id: int) -> int | None:
        data = await self.get_winner_data(guild_id)
        return data.get("current_winner")

    async def set_current_winner(self, guild_id: int, user_id: int | None) -> None:
        data = await self.get_winner_data(guild_id)
        data["current_winner"] = user_id
        await self._set_winner_data(guild_id, data)

    async def add_win(self, guild_id: int, user_id: int) -> int:
        data = await self.get_winner_data(guild_id)
        uid = str(user_id)
        data["wins"][uid] = data["wins"].get(uid, 0) + 1
        await self._set_winner_data(guild_id, data)
        return data["wins"][uid]

    async def get_total_wins(self, guild_id: int, user_id: int) -> int:
        data = await self.get_winner_data(guild_id)
        return data["wins"].get(str(user_id), 0)

    async def get_customization(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        data = await self.get_winner_data(guild_id)
        return data["customizations"].get(str(user_id))

    async def save_customization(
        self,
        guild_id: int,
        user_id: int,
        customization: dict[str, Any],
    ) -> None:
        data = await self.get_winner_data(guild_id)
        data["customizations"][str(user_id)] = customization
        await self._set_winner_data(guild_id, data)
