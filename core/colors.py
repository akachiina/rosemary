"""Color panel persistence: guild color entries and the posted panel.

One record per guild in ``data/<guild_id>/colors.json``::

    {
      "colors": [{"id": "a1b2c3", "name": "Rosa", "color": "#FFB7C5",
                  "role_id": 123}, ...],   # list order = panel order
      "panel_message_id": 456,
      "seeded": true
    }

The list is the single source of truth for everything derived: the panel
buttons/select, the rendered image and the ``/cores`` listing all consume
:attr:`colors` in stored order. Entries are linked to real guild roles via
``role_id`` (``None`` = entry without a role yet - it renders but cannot be
picked); the live role's color wins at render time, so admins recoloring the
role on Discord see the panel catch up on the next repaint.

Seed: the first time the manager opens, twelve pastel entries (names from
the catalogs under ``colors.defaults.<slug>``) are created so the panel works
out of the box; creation only touches this file - role attachment is the
admin's explicit action.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rosemary.core.storage import GuildStorage

_STORE_FILE = "colors.json"

#: Sentinel for :meth:`ColorStore.update_color` role_id "keep current".
_UNSET = object()

#: Hard cap: 5 ActionRows of 5 buttons is Discord's per-message ceiling and
#: the select picker shares the same 25-option limit.
MAX_COLORS = 25

#: First-run pastel seed: (slug, hex). Names resolve from
#: ``colors.defaults.<slug>`` so the guild language decides the label.
PASTEL_SEEDS: tuple[tuple[str, str], ...] = (
    ("rose", "#FFB7C5"),
    ("salmon", "#FFB3A7"),
    ("peach", "#FFDAB9"),
    ("butter", "#FDFD96"),
    ("mint", "#B5EAD7"),
    ("sage", "#C1E1C1"),
    ("sky", "#AEC6CF"),
    ("lavender", "#C3B1E1"),
    ("lilac", "#C8A2C8"),
    ("periwinkle", "#CCCCFF"),
    ("cotton_candy", "#F7C9DE"),
    ("sand", "#F5E6D3"),
)


@dataclass(frozen=True)
class ColorEntry:
    """One pickable color: identity, display name, hex and attached role."""

    id: str
    name: str
    color: str
    role_id: int | None = None


def new_color_id() -> str:
    """Short opaque id for custom_ids and store lookups."""
    return secrets.token_hex(3)


#: Manager modal input: ``#``-optional six-digit hex (the store stores with #).
_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _HEX_OK(value: str) -> bool:
    """Whether ``value`` is a valid hex color for the manager modals."""
    return bool(_HEX_RE.match(value or ""))


class ColorStore:
    """Per-guild color records in ``data/<guild_id>/colors.json``."""

    def __init__(self, data_dir: Path | str) -> None:
        self.storage = GuildStorage(Path(data_dir), filename=_STORE_FILE, use_defaults=False)

    async def _doc(self, guild_id: int) -> dict[str, Any]:
        doc = await self.storage.get(guild_id)
        if not isinstance(doc.get("colors"), list):
            doc["colors"] = []
        return doc

    async def _save(self, guild_id: int, doc: dict[str, Any]) -> None:
        await self.storage.set_all(guild_id, doc)

    @staticmethod
    def _entry(raw: Any) -> ColorEntry | None:
        if not isinstance(raw, dict):
            return None
        color_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        color = str(raw.get("color") or "").strip()
        if not color_id or not name or not color:
            return None
        role_id = raw.get("role_id")
        return ColorEntry(
            id=color_id,
            name=name,
            color=color,
            role_id=int(role_id) if role_id else None,
        )

    # reads ====================

    async def list_colors(self, guild_id: int) -> list[ColorEntry]:
        """Stored colors in panel order (corrupt rows dropped silently)."""
        doc = await self._doc(guild_id)
        entries = [self._entry(raw) for raw in doc["colors"]]
        return [entry for entry in entries if entry is not None]

    async def get_color(self, guild_id: int, color_id: str) -> ColorEntry | None:
        for entry in await self.list_colors(guild_id):
            if entry.id == color_id:
                return entry
        return None

    async def find_by_role(self, guild_id: int, role_id: int) -> ColorEntry | None:
        for entry in await self.list_colors(guild_id):
            if entry.role_id == role_id:
                return entry
        return None

    async def seeded(self, guild_id: int) -> bool:
        return bool((await self._doc(guild_id)).get("seeded"))

    async def get_panel(self, guild_id: int) -> int | None:
        panel = (await self._doc(guild_id)).get("panel_message_id")
        return int(panel) if panel else None

    # writes ====================

    async def set_colors(self, guild_id: int, entries: list[ColorEntry]) -> None:
        """Full-list replace (manager operations rebuild the list first)."""
        doc = await self._doc(guild_id)
        doc["colors"] = [
            {
                "id": entry.id,
                "name": entry.name,
                "color": entry.color,
                "role_id": entry.role_id,
            }
            for entry in entries[:MAX_COLORS]
        ]
        await self._save(guild_id, doc)

    async def add_color(
        self,
        guild_id: int,
        name: str,
        color: str,
        role_id: int | None = None,
    ) -> ColorEntry | None:
        """Append one color; ``None`` when the guild hit :data:`MAX_COLORS`."""
        entries = await self.list_colors(guild_id)
        if len(entries) >= MAX_COLORS:
            return None
        entry = ColorEntry(new_color_id(), name, color, role_id)
        await self.set_colors(guild_id, [*entries, entry])
        return entry

    async def update_color(
        self,
        guild_id: int,
        color_id: str,
        *,
        name: str | None = None,
        color: str | None = None,
        role_id: int | None | object = _UNSET,
    ) -> ColorEntry | None:
        """Patch name/hex/role. ``role_id`` uses a sentinel: pass ``None``
        explicitly to detach, omit it to keep the current role."""
        entries = await self.list_colors(guild_id)
        updated: list[ColorEntry] = []
        found: ColorEntry | None = None
        for entry in entries:
            if entry.id != color_id:
                updated.append(entry)
                continue
            found = ColorEntry(
                id=entry.id,
                name=name if name is not None else entry.name,
                color=color if color is not None else entry.color,
                role_id=entry.role_id if role_id is _UNSET else role_id,
            )
            updated.append(found)
        if found is not None:
            await self.set_colors(guild_id, updated)
        return found

    async def remove_color(self, guild_id: int, color_id: str) -> ColorEntry | None:
        entries = await self.list_colors(guild_id)
        removed = next((entry for entry in entries if entry.id == color_id), None)
        if removed is not None:
            await self.set_colors(
                guild_id, [entry for entry in entries if entry.id != color_id]
            )
        return removed

    async def move(self, guild_id: int, color_id: str, delta: int) -> bool:
        """Shift one color by ``delta`` positions (edge moves clamp to a no-op)."""
        entries = await self.list_colors(guild_id)
        index = next((i for i, entry in enumerate(entries) if entry.id == color_id), -1)
        if index < 0:
            return False
        target = min(max(index + delta, 0), len(entries) - 1)
        if target == index:
            return False
        entries.insert(target, entries.pop(index))
        await self.set_colors(guild_id, entries)
        return True

    async def mark_seeded(self, guild_id: int) -> None:
        doc = await self._doc(guild_id)
        doc["seeded"] = True
        await self._save(guild_id, doc)

    async def seed_pastels(self, guild_id: int, names: dict[str, str]) -> list[ColorEntry]:
        """Create the pastel defaults once; existing entries are kept.

        ``names`` maps slug -> translated label (resolved by the caller from
        ``colors.defaults.<slug>``). Only runs when nothing is stored yet and
        the guild was never seeded, so clearing the manager re-seeds only via
        an explicit delete-all.
        """
        entries = await self.list_colors(guild_id)
        if entries or await self.seeded(guild_id):
            return entries
        created = [
            ColorEntry(new_color_id(), names.get(slug, slug.title()), color)
            for slug, color in PASTEL_SEEDS
        ]
        await self.set_colors(guild_id, created)
        await self.mark_seeded(guild_id)
        return created

    async def set_panel(self, guild_id: int, message_id: int | None) -> None:
        doc = await self._doc(guild_id)
        doc["panel_message_id"] = message_id
        await self._save(guild_id, doc)
