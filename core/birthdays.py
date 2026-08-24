"""Birthday persistence per guild (``birthdays.json``).

Each entry stores the day/month plus bookkeeping: ``last_changed`` enforces the
self-change cooldown and ``last_announced_year`` prevents duplicate yearly
announcements. Changing a date never resets the announcement year.
"""

from __future__ import annotations

import time
from typing import Any

from rosemary.core.storage import GuildStorage


class BirthdaysStore:
    def __init__(self, data_dir) -> None:
        self._storage = GuildStorage(
            data_dir, filename="birthdays.json", use_defaults=False
        )

    async def all(self, guild_id: int) -> dict[int, dict[str, Any]]:
        data = await self._storage.get(guild_id)
        return {
            int(user_id): entry
            for user_id, entry in data.items()
            if isinstance(entry, dict) and "day" in entry
        }

    async def get(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        return (await self.all(guild_id)).get(user_id)

    async def set(
        self,
        guild_id: int,
        user_id: int,
        *,
        day: int,
        month: int,
        last_changed: float | None = None,
        keep_year: bool = True,
    ) -> None:
        data = await self._storage.get(guild_id)
        previous = data.get(str(user_id)) or {}
        entry: dict[str, Any] = {
            "day": day,
            "month": month,
            "last_changed": last_changed if last_changed is not None else time.time(),
        }
        if keep_year and previous.get("last_announced_year"):
            entry["last_announced_year"] = previous["last_announced_year"]
        data[str(user_id)] = entry
        await self._storage.set_all(guild_id, data)

    async def clear(self, guild_id: int, user_id: int) -> bool:
        data = await self._storage.get(guild_id)
        removed = data.pop(str(user_id), None) is not None
        if removed:
            await self._storage.set_all(guild_id, data)
        return removed

    async def mark_announced(self, guild_id: int, user_id: int, year: int) -> None:
        data = await self._storage.get(guild_id)
        entry = data.get(str(user_id))
        if isinstance(entry, dict):
            entry["last_announced_year"] = year
            await self._storage.set_all(guild_id, data)


_DAYS_IN_MONTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def validate_date(day: int, month: int) -> str | None:
    """Return an error code ('bad_month'/'bad_day') or None when acceptable."""
    if not 1 <= month <= 12:
        return "bad_month"
    if not 1 <= day <= _DAYS_IN_MONTH[month - 1]:
        return "bad_day"
    return None
