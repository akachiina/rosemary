"""Suggestion persistence: per-guild counter and vote entries.

One JSON file per guild (``suggestions.json``), holding a monotonically
increasing suggestion number plus one entry per posted suggestion message.
Votes are stored as voter id lists so button clicks can toggle and switch
sides with exactly one vote per person.
"""

from __future__ import annotations

from typing import Any

from rosemary.core.storage import GuildStorage


class SuggestionStore:
    """CRUD for suggestion posts and their votes."""

    def __init__(self, data_dir) -> None:
        self._storage = GuildStorage(
            data_dir, filename="suggestions.json", use_defaults=False
        )

    async def _doc(self, guild_id: int) -> dict[str, Any]:
        data = await self._storage.get(guild_id)
        if not isinstance(data.get("counter"), int):
            data["counter"] = 0
        if not isinstance(data.get("entries"), dict):
            data["entries"] = {}
        return data

    async def next_number(self, guild_id: int) -> int:
        """Increment the per-guild counter and return the new number."""
        data = await self._doc(guild_id)
        data["counter"] = int(data.get("counter") or 0) + 1
        await self._storage.set_all(guild_id, data)
        return data["counter"]

    async def put(
        self,
        guild_id: int,
        message_id: int,
        *,
        number: int,
        author_id: int,
        channel_id: int,
        body: str = "",
        user_name: str = "",
        user_avatar: str = "",
        image_url: str = "",
    ) -> None:
        """Record a newly posted suggestion message."""
        data = await self._doc(guild_id)
        entries = data["entries"]
        entries[str(message_id)] = {
            "number": number,
            "author_id": author_id,
            "channel_id": channel_id,
            "body": body,
            "user_name": user_name,
            "user_avatar": user_avatar,
            "image_url": image_url,
            "upvoters": [],
            "downvoters": [],
            "status": "open",
        }
        await self._storage.set_all(guild_id, data)

    async def get(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        """Return one entry, or ``None`` when unknown."""
        data = await self._doc(guild_id)
        entry = data["entries"].get(str(message_id))
        return dict(entry) if isinstance(entry, dict) else None

    async def get_by_number(
        self, guild_id: int, number: int
    ) -> tuple[int, dict[str, Any]] | None:
        """Find an entry by its suggestion number: ``(message_id, entry)``."""
        data = await self._doc(guild_id)
        for message_id, entry in data["entries"].items():
            if isinstance(entry, dict) and entry.get("number") == number:
                return int(message_id), dict(entry)
        return None

    async def set_status(
        self,
        guild_id: int,
        message_id: int,
        status: str,
        decided_by: int = 0,
    ) -> dict[str, Any] | None:
        """Mark a suggestion ``approved``/``rejected`` (staff decision)."""
        if status not in ("open", "approved", "rejected"):
            raise ValueError(f"unknown status {status!r}")
        data = await self._doc(guild_id)
        entry = data["entries"].get(str(message_id))
        if not isinstance(entry, dict):
            return None
        entry["status"] = status
        entry["decided_by"] = decided_by
        await self._storage.set_all(guild_id, data)
        return dict(entry)

    async def all_entries(self, guild_id: int) -> dict[str, dict[str, Any]]:
        """Return every entry for a guild (used to restore vote views)."""
        data = await self._doc(guild_id)
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return {}
        return {k: dict(v) for k, v in entries.items() if isinstance(v, dict)}

    async def toggle_vote(
        self, guild_id: int, message_id: int, voter_id: int, side: str
    ) -> dict[str, Any] | None:
        """Toggle a vote. Returns the updated entry, or ``None`` when unknown.

        Clicking the same side twice removes the vote; clicking the other
        side moves it. ``side`` must be ``"up"`` or ``"down"``.
        """
        if side not in ("up", "down"):
            raise ValueError(f"unknown vote side {side!r}")
        data = await self._doc(guild_id)
        entries = data["entries"]
        entry = entries.get(str(message_id))
        if not isinstance(entry, dict):
            return None
        upvoters = [int(v) for v in entry.get("upvoters", []) if int(v) > 0]
        downvoters = [int(v) for v in entry.get("downvoters", []) if int(v) > 0]
        if side == "up":
            if voter_id in upvoters:
                upvoters.remove(voter_id)
            else:
                upvoters.append(voter_id)
                if voter_id in downvoters:
                    downvoters.remove(voter_id)
        else:
            if voter_id in downvoters:
                downvoters.remove(voter_id)
            else:
                downvoters.append(voter_id)
                if voter_id in upvoters:
                    upvoters.remove(voter_id)
        entry["upvoters"] = upvoters
        entry["downvoters"] = downvoters
        await self._storage.set_all(guild_id, data)
        return dict(entry)

    @staticmethod
    def counts(entry: dict[str, Any]) -> tuple[int, int]:
        """``(upvotes, downvotes)`` for an entry."""
        up = len(entry.get("upvoters") or [])
        down = len(entry.get("downvoters") or [])
        return up, down

    async def remove(self, guild_id: int, message_id: int) -> None:
        """Drop one entry (message deleted)."""
        data = await self._doc(guild_id)
        if str(message_id) in data["entries"]:
            del data["entries"][str(message_id)]
            await self._storage.set_all(guild_id, data)
