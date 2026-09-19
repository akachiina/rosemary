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

Seed: the first open seeds the 20 pastels (names from the catalogs under
``colors.defaults.<slug>``) AND the cog creates their Discord roles, so the
panel works out of the box. Guilds seeded by the earlier 12-color palette
migrate through :meth:`ColorStore.migrate_seed_v2` (version stamp
``seed_version``), which keeps entry ids and role links alive so posted
buttons never die.
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

#: First-run pastel seed: (slug, hex). Twenty colors alternating a light
#: and a darker pastel per hue (the Color-Chan look). Names resolve from
#: ``colors.defaults.<slug>`` so the guild language decides the label.
PASTEL_SEEDS: tuple[tuple[str, str], ...] = (
    ("rose_light", "#FFD6E0"),
    ("rose_dark", "#E8A0B4"),
    ("peach_light", "#FFE4CE"),
    ("peach_dark", "#F0B48A"),
    ("butter_light", "#FFF2AE"),
    ("butter_dark", "#E8D27C"),
    ("mint_light", "#D5F5DF"),
    ("mint_dark", "#9FD6AE"),
    ("sky_light", "#D8EDF9"),
    ("sky_dark", "#A3CBE3"),
    ("lavender_light", "#E6D9F7"),
    ("lavender_dark", "#C2ABE3"),
    ("periwinkle_light", "#DFE5FA"),
    ("periwinkle_dark", "#ADB9E6"),
    ("cotton_light", "#FBD9E9"),
    ("cotton_dark", "#E7A6C4"),
    ("sand_light", "#F3E6CF"),
    ("sand_dark", "#D8BC94"),
    ("sage_light", "#DEEDDE"),
    ("sage_dark", "#A5C6A5"),
)

#: The earlier 12-color palette (pre seed-v2): guilds seeded with these hexes
#: migrate to the 20-color set. Order matches the old PASTEL_SEEDS.
LEGACY_SEEDS: tuple[tuple[str, str], ...] = (
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

#: Legacy slug -> v2 slug: each old hue lands on its closest v2 color, so
#: donor entries (and their live Discord roles) survive the migration.
LEGACY_MAP: dict[str, str] = {
    "rose": "rose_light",
    "salmon": "peach_dark",
    "peach": "peach_light",
    "butter": "butter_light",
    "mint": "mint_light",
    "sage": "sage_light",
    "sky": "sky_light",
    "lavender": "lavender_light",
    "lilac": "lavender_dark",
    "periwinkle": "periwinkle_light",
    "cotton_candy": "cotton_light",
    "sand": "sand_light",
}


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

    async def panel_state(self, guild_id: int) -> tuple[int | None, str | None]:
        """``(posted message id, stored fingerprint)`` in one read.

        The fingerprint is the content hash the cog computes for everything
        the panel renders (entries, picker mode, chunk size, theme); a
        repaint whose computed hash matches skips every HTTP call, so boot
        restores dispatch without re-sending the panel (the ticket-panel
        contract).
        """
        doc = await self._doc(guild_id)
        panel = doc.get("panel_message_id")
        fingerprint = doc.get("panel_fingerprint")
        return (
            int(panel) if panel else None,
            str(fingerprint) if fingerprint else None,
        )

    async def add_known_panel(self, guild_id: int, message_id: int) -> None:
        """Record every panel message id ever posted.

        Older buggy posts predate the single-panel invariant; the repost
        path sweeps these ids so stacked panels self-heal instead of
        staying on the channel forever.
        """
        doc = await self._doc(guild_id)
        known: list = doc.setdefault("known_panel_ids", [])
        if message_id not in known:
            known.append(message_id)
        await self._save(guild_id, doc)

    async def known_panels(self, guild_id: int) -> list[int]:
        """Every panel message id ever posted for the guild."""
        doc = await self._doc(guild_id)
        known = doc.get("known_panel_ids") or []
        current = doc.get("panel_message_id")
        ids = {int(mid) for mid in known if mid}
        if current:
            ids.add(int(current))
        return sorted(ids)

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

    async def migrate_seed_v2(
        self, guild_id: int, names: dict[str, str]
    ) -> tuple[list[ColorEntry], list[tuple[int, str, str]]] | None:
        """Legacy 12-color seed -> the 20-color set, one-time.

        Detection is by stored hex (the old palette's exact values): a guild
        whose entries carry at least 8 legacy hexes is a stock v1 seed and
        gets replaced in place; anything else is admin-customized and left
        untouched (only stamped so the check never runs again).

        Donor entries keep their id (posted buttons stay valid) and their
        ``role_id`` (members keep wearing a color role); the role itself is
        renamed/recolored by the caller via the returned
        ``(role_id, name, hex)`` updates. New hues get fresh entries whose
        roles the cog creates. Returns ``None`` when there is nothing to do.
        """
        doc = await self._doc(guild_id)
        if doc.get("seed_version") == 2:
            return None
        doc["seed_version"] = 2
        entries = await self.list_colors(guild_id)
        if not doc.get("seeded") or not entries:
            await self._save(guild_id, doc)
            return None
        legacy_hex = {hex_.upper() for _slug, hex_ in LEGACY_SEEDS}
        by_hex: dict[str, ColorEntry] = {}
        for entry in entries:
            by_hex.setdefault(entry.color.upper(), entry)
        matched = sum(1 for hex_ in legacy_hex if hex_ in by_hex)
        if matched < 8:
            await self._save(guild_id, doc)
            return None
        v2_to_legacy: dict[str, str] = {}
        for old_slug, new_slug in LEGACY_MAP.items():
            v2_to_legacy.setdefault(new_slug, old_slug)
        used: set[str] = set()
        new_entries: list[ColorEntry] = []
        role_updates: list[tuple[int, str, str]] = []
        for slug, hex_ in PASTEL_SEEDS:
            label = names.get(slug, slug.title())
            donor: ColorEntry | None = None
            old_slug = v2_to_legacy.get(slug)
            if old_slug is not None:
                old_hex = next((h for s, h in LEGACY_SEEDS if s == old_slug), None)
                candidate = by_hex.get(old_hex.upper()) if old_hex else None
                if candidate is not None and candidate.id not in used:
                    donor = candidate
            if donor is not None:
                used.add(donor.id)
                new_entries.append(
                    ColorEntry(donor.id, label, hex_, donor.role_id)
                )
                if donor.role_id and (
                    donor.name != label or donor.color.upper() != hex_.upper()
                ):
                    role_updates.append((donor.role_id, label, hex_))
            else:
                new_entries.append(ColorEntry(new_color_id(), label, hex_))
        # Admin entries outside the stock palette ride along untouched.
        new_entries.extend(entry for entry in entries if entry.id not in used)
        # set_colors re-reads the stored doc; stamp on a fresh read so the
        # save never clobbers the new list with this method's stale snapshot.
        await self.set_colors(guild_id, new_entries)
        doc = await self._doc(guild_id)
        doc["seed_version"] = 2
        await self._save(guild_id, doc)
        return new_entries, role_updates

    async def set_panel(
        self, guild_id: int, message_id: int | None, fingerprint: str | None = None
    ) -> None:
        doc = await self._doc(guild_id)
        doc["panel_message_id"] = message_id
        doc["panel_fingerprint"] = fingerprint
        if message_id is not None:
            # The live panel is the whole history: the repost swept every
            # older message, so keeping their ids would re-delete later.
            doc["known_panel_ids"] = [message_id]
        await self._save(guild_id, doc)
