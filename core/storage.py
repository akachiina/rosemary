"""Per-guild JSON persistence for Rosemary.

Each guild owns a settings file at ``data/<guild_id>/<filename>`` (default
``settings.json``). Feature stores can pass a different filename so their data
lives in its own file instead of mixing with /settings. Reads merge over
defaults; writes are atomic (temp file + ``os.replace``).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {"language": "en-US"}


class GuildStorage:
    """Reads and writes one JSON file per guild."""

    def __init__(
        self,
        data_dir: Path,
        *,
        filename: str = "settings.json",
        use_defaults: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.filename = filename
        self.use_defaults = use_defaults

    def _path(self, guild_id: int) -> Path:
        return self.data_dir / str(guild_id) / self.filename

    async def get(self, guild_id: int) -> dict[str, Any]:
        """Return the guild's data merged over defaults.

        Feature stores that pass ``use_defaults=False`` get only what was
        actually written (their file never carries default keys like
        ``language``). Reading never creates files; a missing or corrupt file
        yields defaults (or an empty dict without defaults).
        """
        data = dict(DEFAULTS) if self.use_defaults else {}
        path = self._path(guild_id)
        if not path.exists():
            return data
        try:
            with path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data.update(loaded)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read settings for guild %s: %s", guild_id, exc)
        return data

    async def set(self, guild_id: int, key: str, value: Any) -> None:
        """Update a single key and persist the file atomically."""
        data = await self.get(guild_id)
        data[key] = value
        await self._write(guild_id, data)

    async def delete_keys(self, guild_id: int, *keys: str) -> None:
        """Remove keys and persist the file atomically (no-op if absent)."""
        data = await self.get(guild_id)
        removed = False
        for key in keys:
            removed = data.pop(key, None) is not None or removed
        if removed:
            await self._write(guild_id, data)

    async def set_all(self, guild_id: int, data: dict[str, Any]) -> None:
        """Replace the guild's entire document and persist it atomically.

        ``data`` must be a full document (merged over defaults by the caller if
        needed); the stored JSON becomes exactly ``data``.
        """
        await self._write(guild_id, data)

    async def _write(self, guild_id: int, data: dict[str, Any]) -> None:
        path = self._path(guild_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
