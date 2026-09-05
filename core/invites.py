"""Invite tracking persistence and attribution helpers.

Data lives per guild in its own JSON file (``data/<guild_id>/invites.json``).
Per inviter four counters are kept — ``regular``, ``bonus``, ``fake`` and
``left`` — and the public total follows the market-standard formula::

    total = regular - left - fake + bonus

Only joins that happen while the bot is present can be attributed; members
who joined before are ``unknown``. Vanity-URL joins are credited to the
server itself (no inviter).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rosemary.core.storage import GuildStorage

_INVITES_FILE = "invites.json"

#: Join statuses stored on the member record.
OK = "ok"
FAKE = "fake"
FLAGGED = "flagged"
REJOIN = "rejoin"
EXCLUDED = "excluded"
UNKNOWN = "unknown"

_CREDITED = (OK, FAKE)


@dataclass(frozen=True)
class InviteSnapshot:
    """Minimal invite data needed for attribution (API object or fake)."""

    code: str
    uses: int
    inviter_id: int | None


def find_used_invite(
    cached_uses: dict[str, int], current: list[InviteSnapshot]
) -> InviteSnapshot | None:
    """Return the invite whose ``uses`` grew since the cached snapshot.

    New codes with ``uses > 0`` also match. Ties resolve to the largest
    increase. Returns ``None`` when nothing grew (or the snapshot is empty).
    """
    if not cached_uses and not current:
        return None
    best: InviteSnapshot | None = None
    best_delta = 0
    for invite in current:
        previous = cached_uses.get(invite.code)
        if previous is None:
            if invite.uses > 0 and invite.uses > best_delta:
                best, best_delta = invite, invite.uses
            continue
        delta = invite.uses - previous
        if delta > best_delta:
            best, best_delta = invite, delta
    return best


def join_status(
    *,
    account_age_days: float,
    fake_delay_days: int,
    is_rejoin: bool,
    rejoin_changed_inviter: bool,
    anti_cheat_days: int,
    count_rejoins: bool,
    excluded: bool,
) -> str:
    """Classify a join for credit purposes (pure, easily tested)."""
    if excluded:
        return EXCLUDED
    if fake_delay_days > 0 and account_age_days < fake_delay_days:
        return FAKE
    if is_rejoin:
        if rejoin_changed_inviter and anti_cheat_days > 0:
            return FLAGGED
        if not count_rejoins:
            return REJOIN
    return OK


def total_of(counters: dict[str, int]) -> int:
    """Market-standard total from one inviter's counters."""
    return (
        counters.get("regular", 0)
        - counters.get("left", 0)
        - counters.get("fake", 0)
        + counters.get("bonus", 0)
    )


def _blank_counters() -> dict[str, int]:
    return {"regular": 0, "bonus": 0, "fake": 0, "left": 0}


class InviteStore:
    """Per-guild invite tracking persistence."""

    def __init__(self, data_dir: Path | str) -> None:
        self.storage = GuildStorage(Path(data_dir), filename=_INVITES_FILE, use_defaults=False)

    async def _doc(self, guild_id: int) -> dict[str, Any]:
        doc = await self.storage.get(guild_id)
        doc.setdefault("members", {})
        doc.setdefault("inviters", {})
        doc.setdefault("blacklist", {"users": [], "roles": [], "hidden": []})
        doc.setdefault("labels", {})
        return doc

    async def _save(self, guild_id: int, doc: dict[str, Any]) -> None:
        await self.storage.set_all(guild_id, doc)

    # -- joins / leaves ----------------------------------------------------

    async def record_join(
        self,
        guild_id: int,
        member_id: int,
        inviter_id: int | None,
        code: str | None,
        status: str,
        *,
        joined_at: datetime | None = None,
    ) -> None:
        doc = await self._doc(guild_id)
        doc["members"][str(member_id)] = {
            "inviter_id": inviter_id,
            "code": code,
            "status": status,
            "joined_at": (joined_at or datetime.now(UTC)).isoformat(),
        }
        if status in _CREDITED and inviter_id is not None:
            counters = doc["inviters"].get(str(inviter_id)) or _blank_counters()
            counters["regular"] += 1
            if status == FAKE:
                counters["fake"] += 1
            doc["inviters"][str(inviter_id)] = counters
        await self._save(guild_id, doc)

    async def record_leave(self, guild_id: int, member_id: int) -> int | None:
        """Credit ``left`` to the recorded inviter. Returns the inviter id."""
        doc = await self._doc(guild_id)
        entry = doc["members"].get(str(member_id))
        if not entry:
            return None
        inviter_id = entry.get("inviter_id")
        if entry.get("status") in _CREDITED and inviter_id is not None:
            counters = doc["inviters"].get(str(inviter_id)) or _blank_counters()
            counters["left"] += 1
            doc["inviters"][str(inviter_id)] = counters
            await self._save(guild_id, doc)
        return inviter_id

    async def previous_record(self, guild_id: int, member_id: int) -> dict[str, Any] | None:
        doc = await self._doc(guild_id)
        entry = doc["members"].get(str(member_id))
        return dict(entry) if isinstance(entry, dict) else None

    # -- stats ---------------------------------------------------------------

    async def get_stats(self, guild_id: int, user_id: int) -> dict[str, int]:
        doc = await self._doc(guild_id)
        counters = doc["inviters"].get(str(user_id)) or _blank_counters()
        return {**counters, "total": total_of(counters)}

    async def leaderboard(self, guild_id: int) -> list[tuple[int, dict[str, int]]]:
        """All inviters with a non-empty record, best total first.

        Users hidden via the blacklist are excluded from display (still tracked).
        """
        doc = await self._doc(guild_id)
        hidden = {int(uid) for uid in (doc["blacklist"].get("hidden") or [])}
        rows = []
        for uid, counters in (doc.get("inviters") or {}).items():
            if int(uid) in hidden:
                continue
            full = {**_blank_counters(), **counters}
            if any(full[key] for key in ("regular", "bonus", "fake", "left")):
                rows.append((int(uid), {**full, "total": total_of(full)}))
        rows.sort(key=lambda row: (row[1]["total"], row[1]["regular"]), reverse=True)
        return rows

    async def invited_by(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        return await self.previous_record(guild_id, user_id)

    async def invited_list(
        self, guild_id: int, inviter_id: int, limit: int = 20
    ) -> list[tuple[int, dict[str, Any]]]:
        doc = await self._doc(guild_id)
        rows = [
            (int(uid), entry)
            for uid, entry in (doc.get("members") or {}).items()
            if isinstance(entry, dict) and entry.get("inviter_id") == inviter_id
        ]
        rows.sort(key=lambda row: str(row[1].get("joined_at") or ""), reverse=True)
        return rows[: max(limit, 0)]

    async def server_totals(self, guild_id: int) -> dict[str, int]:
        doc = await self._doc(guild_id)
        totals = {"regular": 0, "bonus": 0, "fake": 0, "left": 0, "joins": 0}
        for counters in (doc.get("inviters") or {}).values():
            for key in ("regular", "bonus", "fake", "left"):
                totals[key] += int(counters.get(key, 0) or 0)
        totals["joins"] = len(doc.get("members") or {})
        totals["total"] = totals["regular"] - totals["left"] - totals["fake"] + totals["bonus"]
        return totals

    # -- bonus (manual accounting) -------------------------------------------

    async def add_bonus(self, guild_id: int, user_id: int, amount: int) -> dict[str, int]:
        doc = await self._doc(guild_id)
        counters = doc["inviters"].get(str(user_id)) or _blank_counters()
        counters["bonus"] += amount
        doc["inviters"][str(user_id)] = counters
        await self._save(guild_id, doc)
        return {**counters, "total": total_of(counters)}

    # -- recount ---------------------------------------------------------------

    async def reconcile_uses(
        self, guild_id: int, live: list[InviteSnapshot]
    ) -> dict[str, int]:
        """Rebuild per-code baselines after an outage. Returns fresh ``uses``.

        Only the attribution baseline is affected; counters are untouched.
        Callers persist the returned mapping wherever they keep the cache.
        """
        return {invite.code: invite.uses for invite in live}

    # -- blacklist / hidden ----------------------------------------------------

    async def _id_list(self, guild_id: int, key: str) -> list[int]:
        doc = await self._doc(guild_id)
        return [int(uid) for uid in (doc["blacklist"].get(key) or [])]

    async def blacklist_add(self, guild_id: int, key: str, user_or_role_id: int) -> bool:
        """Add an id to ``users``/``roles``/``hidden``. Returns False if present."""
        if key not in ("users", "roles", "hidden"):
            raise ValueError(f"Unknown blacklist {key!r}")
        doc = await self._doc(guild_id)
        current = [int(uid) for uid in (doc["blacklist"].get(key) or [])]
        if int(user_or_role_id) in current:
            return False
        doc["blacklist"][key] = [*current, int(user_or_role_id)]
        await self._save(guild_id, doc)
        return True

    async def blacklist_remove(self, guild_id: int, key: str, user_or_role_id: int) -> bool:
        doc = await self._doc(guild_id)
        current = [int(uid) for uid in (doc["blacklist"].get(key) or [])]
        if int(user_or_role_id) not in current:
            return False
        doc["blacklist"][key] = [uid for uid in current if uid != int(user_or_role_id)]
        await self._save(guild_id, doc)
        return True

    async def is_excluded(
        self, guild_id: int, inviter_id: int, inviter_role_ids: list[int]
    ) -> bool:
        doc = await self._doc(guild_id)
        users = {int(uid) for uid in (doc["blacklist"].get("users") or [])}
        roles = {int(rid) for rid in (doc["blacklist"].get("roles") or [])}
        if inviter_id in users:
            return True
        return any(rid in roles for rid in inviter_role_ids)

    # -- labels ------------------------------------------------------------------

    async def set_label(self, guild_id: int, code: str, label: str | None) -> None:
        doc = await self._doc(guild_id)
        labels = doc.get("labels") or {}
        if label:
            labels[code] = label
        else:
            labels.pop(code, None)
        doc["labels"] = labels
        await self._save(guild_id, doc)

    async def get_label(self, guild_id: int, code: str | None) -> str | None:
        if not code:
            return None
        doc = await self._doc(guild_id)
        label = (doc.get("labels") or {}).get(code)
        return str(label) if label else None

    # -- maintenance ---------------------------------------------------------------

    async def reset(self, guild_id: int) -> None:
        await self.storage.set_all(
            guild_id,
            {
                "members": {},
                "inviters": {},
                "blacklist": {"users": [], "roles": [], "hidden": []},
                "labels": {},
            },
        )
