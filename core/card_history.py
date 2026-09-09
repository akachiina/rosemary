"""Per-card version history for the /personalizar composer.

Versions live in their own ``cards_history.json`` (via :class:`GuildStorage`),
capped per card, so restoring never touches the live override until the admin
saves. Guilds without history simply start empty.
"""

from __future__ import annotations

import copy
import time
from typing import Any

from rosemary.core.storage import GuildStorage

_HISTORY_FILE = "cards_history.json"

#: Versions kept per card (oldest pruned on append).
MAX_VERSIONS = 20


class CardHistory:
    """Append-only version log for card documents."""

    def __init__(self, data_dir) -> None:
        self._storage = GuildStorage(data_dir, filename=_HISTORY_FILE, use_defaults=False)

    async def append(
        self, guild_id: int, key: str, doc: dict[str, Any], actor_id: int | None = None
    ) -> int:
        """Record a version; returns its number. Prunes beyond the cap."""
        data = await self._storage.get(guild_id)
        versions = data.get(key) or []
        number = (versions[-1].get("ver", 0) if versions else 0) + 1
        versions.append(
            {
                "ver": number,
                "at": int(time.time()),
                "actor": actor_id,
                "doc": copy.deepcopy(doc),
            }
        )
        await self._storage.set(guild_id, key, versions[-MAX_VERSIONS:])
        return number

    async def list(self, guild_id: int, key: str) -> list[dict[str, Any]]:
        """Version metadata (no documents) newest last."""
        data = await self._storage.get(guild_id)
        versions = data.get(key) or []
        return [
            {"ver": v.get("ver"), "at": v.get("at"), "actor": v.get("actor")}
            for v in versions
            if isinstance(v, dict)
        ]

    async def get(self, guild_id: int, key: str, ver: int) -> dict[str, Any] | None:
        """Full document for one version."""
        data = await self._storage.get(guild_id)
        for version in data.get(key) or []:
            if isinstance(version, dict) and version.get("ver") == ver:
                doc = version.get("doc")
                return copy.deepcopy(doc) if isinstance(doc, dict) else None
        return None


def history_store(bot) -> CardHistory:
    """Default history bound to the bot's data directory."""
    return CardHistory(bot.storage.data_dir)
