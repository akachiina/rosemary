"""Starboard persistence: which messages are currently on the starboard.

One JSON file per guild (``starboard.json``), mapping the original message id
to the starboard post and its last known star count.
"""

from __future__ import annotations

from typing import Any

from rosemary.core.storage import GuildStorage


class StarboardStore:
    """CRUD for original-message → starboard-post links."""

    def __init__(self, data_dir) -> None:
        self._storage = GuildStorage(
            data_dir, filename="starboard.json", use_defaults=False
        )

    async def get_entry(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        data = await self._storage.get(guild_id)
        entry = data.get(str(message_id))
        return entry if isinstance(entry, dict) else None

    async def upsert(
        self,
        guild_id: int,
        message_id: int,
        *,
        post_id: int,
        stars: int,
        channel_id: int,
        author_id: int,
    ) -> None:
        data = await self._storage.get(guild_id)
        data[str(message_id)] = {
            "post_id": post_id,
            "stars": stars,
            "channel_id": channel_id,
            "author_id": author_id,
        }
        await self._storage.set_all(guild_id, data)

    async def update_stars(self, guild_id: int, message_id: int, stars: int) -> None:
        entry = await self.get_entry(guild_id, message_id)
        if entry is None:
            return
        data = await self._storage.get(guild_id)
        data[str(message_id)]["stars"] = stars
        await self._storage.set_all(guild_id, data)

    async def remove(self, guild_id: int, message_id: int) -> None:
        data = await self._storage.get(guild_id)
        if str(message_id) in data:
            del data[str(message_id)]
            await self._storage.set_all(guild_id, data)
