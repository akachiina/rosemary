"""Partnership persistence: one record per partnered server/member."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rosemary.core.storage import GuildStorage

_PARTNERSHIPS_FILE = "partnerships.json"


class PartnershipStore:
    """Per-guild partnership records in ``data/<guild_id>/partnerships.json``."""

    def __init__(self, data_dir: Path | str) -> None:
        self.storage = GuildStorage(
            Path(data_dir), filename=_PARTNERSHIPS_FILE, use_defaults=False
        )

    async def _doc(self, guild_id: int) -> dict[str, Any]:
        doc = await self.storage.get(guild_id)
        doc.setdefault("partnerships", {})
        return doc

    async def _save(self, guild_id: int, doc: dict[str, Any]) -> None:
        await self.storage.set_all(guild_id, doc)

    async def add(
        self,
        guild_id: int,
        partner_id: int,
        rep_id: int,
        adder_id: int,
        message_id: int | None,
        content: str,
        attachments: list[str],
        raw_text: str = "",
    ) -> None:
        doc = await self._doc(guild_id)
        doc["partnerships"][str(partner_id)] = {
            "rep_id": rep_id,
            "adder_id": adder_id,
            "message_id": message_id,
            "content": content,
            "raw_text": raw_text,
            "attachments": attachments,
            "last_renewed_at": datetime.now(UTC).isoformat(),
            "warning_sent": False,
        }
        await self._save(guild_id, doc)

    async def edit(
        self,
        guild_id: int,
        partner_id: int,
        *,
        content: str,
        raw_text: str,
        message_id: int | None,
        new_rep_id: int | None = None,
    ) -> bool:
        """Update a partnership's ad and optionally move it to a new rep.

        An edit is not a renewal: ``last_renewed_at``, ``warning_sent``,
        ``attachments`` and ``adder_id`` stay untouched. A ``new_rep_id``
        re-keys the record (the key is the rep's id, which
        ``/partnerships renew`` looks up by author). Returns ``False`` when
        the record is missing or the target key is already taken; callers
        should check the collision first, this stays defensive.
        """
        doc = await self._doc(guild_id)
        entry = doc["partnerships"].get(str(partner_id))
        if not isinstance(entry, dict):
            return False
        swapping = new_rep_id is not None and new_rep_id != partner_id
        if swapping and str(new_rep_id) in doc["partnerships"]:
            return False
        entry["content"] = content
        entry["raw_text"] = raw_text
        entry["message_id"] = message_id
        if swapping:
            entry["rep_id"] = new_rep_id
            doc["partnerships"].pop(str(partner_id))
            doc["partnerships"][str(new_rep_id)] = entry
        await self._save(guild_id, doc)
        return True

    async def get(self, guild_id: int, partner_id: int) -> dict[str, Any] | None:
        doc = await self._doc(guild_id)
        entry = doc["partnerships"].get(str(partner_id))
        return dict(entry) if isinstance(entry, dict) else None

    async def remove(self, guild_id: int, partner_id: int) -> dict[str, Any] | None:
        doc = await self._doc(guild_id)
        entry = doc["partnerships"].pop(str(partner_id), None)
        await self._save(guild_id, doc)
        return dict(entry) if isinstance(entry, dict) else None

    async def all(self, guild_id: int) -> dict[int, dict[str, Any]]:
        doc = await self._doc(guild_id)
        return {
            int(pid): dict(entry)
            for pid, entry in (doc.get("partnerships") or {}).items()
            if isinstance(entry, dict)
        }

    async def touch_renew(
        self,
        guild_id: int,
        partner_id: int,
        message_id: int | None,
    ) -> bool:
        doc = await self._doc(guild_id)
        entry = doc["partnerships"].get(str(partner_id))
        if not isinstance(entry, dict):
            return False
        entry["last_renewed_at"] = datetime.now(UTC).isoformat()
        entry["warning_sent"] = False
        entry["message_id"] = message_id
        await self._save(guild_id, doc)
        return True

    async def mark_warned(self, guild_id: int, partner_id: int) -> None:
        doc = await self._doc(guild_id)
        entry = doc["partnerships"].get(str(partner_id))
        if isinstance(entry, dict):
            entry["warning_sent"] = True
            await self._save(guild_id, doc)

    @staticmethod
    def days_since(iso_value: str | None, now: datetime | None = None) -> float:
        if not iso_value:
            return 0.0
        try:
            moment = datetime.fromisoformat(iso_value)
        except ValueError:
            return 0.0
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        now = now or datetime.now(UTC)
        return (now - moment).total_seconds() / 86400.0
