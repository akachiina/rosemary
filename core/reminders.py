"""Canned DM reminders: named messages stored per guild and fanned out later."""

from __future__ import annotations

from pathlib import Path

from rosemary.core.storage import GuildStorage

_REMINDERS_FILE = "reminders.json"


class ReminderStore:
    """Per-guild named reminder messages (``name -> text``)."""

    def __init__(self, data_dir: Path | str) -> None:
        self.storage = GuildStorage(Path(data_dir), filename=_REMINDERS_FILE, use_defaults=False)

    async def all(self, guild_id: int) -> dict[str, str]:
        doc = await self.storage.get(guild_id)
        data = doc.get("reminders") or {}
        return {str(k): str(v) for k, v in data.items() if v}

    async def save(self, guild_id: int, name: str, message: str) -> None:
        doc = await self.storage.get(guild_id)
        data = doc.get("reminders") or {}
        data[name.strip().lower()] = message
        doc["reminders"] = data
        await self.storage.set_all(guild_id, doc)

    async def delete(self, guild_id: int, name: str) -> bool:
        doc = await self.storage.get(guild_id)
        data = doc.get("reminders") or {}
        if name.strip().lower() not in data:
            return False
        del data[name.strip().lower()]
        doc["reminders"] = data
        await self.storage.set_all(guild_id, doc)
        return True

    async def get(self, guild_id: int, name: str) -> str | None:
        return (await self.all(guild_id)).get(name.strip().lower())
