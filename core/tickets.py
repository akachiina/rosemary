"""Ticket persistence: one record per ticket channel, plus the panel message."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rosemary.core.storage import GuildStorage

_TICKETS_FILE = "tickets.json"

OPEN = "open"
CLOSED = "closed"


class TicketStore:
    """Per-guild ticket records in ``data/<guild_id>/tickets.json``."""

    def __init__(self, data_dir: Path | str) -> None:
        self.storage = GuildStorage(Path(data_dir), filename=_TICKETS_FILE, use_defaults=False)

    async def _doc(self, guild_id: int) -> dict[str, Any]:
        doc = await self.storage.get(guild_id)
        doc.setdefault("tickets", {})
        doc.setdefault("panel_message_id", None)
        return doc

    async def _save(self, guild_id: int, doc: dict[str, Any]) -> None:
        await self.storage.set_all(guild_id, doc)

    async def open_ticket(
        self,
        guild_id: int,
        channel_id: int,
        owner_id: int,
        ticket_type: str,
    ) -> None:
        doc = await self._doc(guild_id)
        doc["tickets"][str(channel_id)] = {
            "owner_id": owner_id,
            "type": ticket_type,
            "status": OPEN,
            "created_at": datetime.now(UTC).isoformat(),
            "closed_at": None,
        }
        await self._save(guild_id, doc)

    async def get_ticket(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        doc = await self._doc(guild_id)
        entry = doc["tickets"].get(str(channel_id))
        return dict(entry) if isinstance(entry, dict) else None

    async def set_status(
        self, guild_id: int, channel_id: int, status: str
    ) -> bool:
        doc = await self._doc(guild_id)
        entry = doc["tickets"].get(str(channel_id))
        if not isinstance(entry, dict):
            return False
        entry["status"] = status
        entry["closed_at"] = (
            datetime.now(UTC).isoformat() if status == CLOSED else None
        )
        await self._save(guild_id, doc)
        return True

    async def delete_ticket(self, guild_id: int, channel_id: int) -> bool:
        doc = await self._doc(guild_id)
        if str(channel_id) not in doc["tickets"]:
            return False
        del doc["tickets"][str(channel_id)]
        await self._save(guild_id, doc)
        return True

    async def open_tickets(self, guild_id: int) -> dict[int, dict[str, Any]]:
        doc = await self._doc(guild_id)
        return {
            int(cid): dict(entry)
            for cid, entry in (doc.get("tickets") or {}).items()
            if isinstance(entry, dict) and entry.get("status") == OPEN
        }

    async def user_open_count(self, guild_id: int, user_id: int) -> int:
        tickets = await self.open_tickets(guild_id)
        return sum(1 for entry in tickets.values() if entry.get("owner_id") == user_id)

    async def set_panel(self, guild_id: int, message_id: int | None) -> None:
        doc = await self._doc(guild_id)
        doc["panel_message_id"] = message_id
        await self._save(guild_id, doc)

    async def get_panel(self, guild_id: int) -> int | None:
        doc = await self._doc(guild_id)
        panel = doc.get("panel_message_id")
        return int(panel) if panel else None
