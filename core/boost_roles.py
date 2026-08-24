"""Boost role persistence and helpers for Rosemary.

Data lives per guild in its own JSON file (``data/<guild_id>/boost_roles.json``)
so it never mixes with the /settings file. It holds two flat keys:

* ``boost_roles``  : ``{role_id: {"owner_id", "members", "created_at", "created_by"}}``
* ``boost_invites`` : ``{invite_id: {"role_id", "inviter_id", "invitee_id",
  "created_at", "expires_at"}}``

Values are read/written with ``storage.get``/``storage.set`` like the ``warnings``
store in :mod:`rosemary.cogs.moderation`, so persistence is atomic and per guild.
Role editing itself (rename/color/icon) is not persisted; the Discord role object
is the source of truth for boost roles.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import discord

from rosemary.core.storage import GuildStorage

log = logging.getLogger(__name__)

_ROLES_KEY = "boost_roles"
_INVITES_KEY = "boost_invites"
_BOOST_FILE = "boost_roles.json"


def is_pickable_role(role: discord.Role) -> bool:
    """Whether a role may be registered as a boost role.

    Rejects ``@everyone`` and roles managed by integrations (bots/boosts).
    """
    return not role.is_default() and not role.managed


def parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO timestamp stored in the store, tolerating bad values."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class BoostRoleStore:
    """Reads and writes boost-role and invite data for one guild.

    The store always persists to ``boost_roles.json`` inside the given data
    directory, separate from the /settings file.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self.storage = GuildStorage(Path(data_dir), filename=_BOOST_FILE, use_defaults=False)

    async def _get(self, guild_id: int, key: str) -> dict[str, Any]:
        data = await self.storage.get(guild_id)
        return dict(data.get(key) or {})

    async def _set(self, guild_id: int, key: str, value: dict[str, Any]) -> None:
        await self.storage.set(guild_id, key, value)

    # -- roles --------------------------------------------------------------

    async def get_roles(self, guild_id: int) -> dict[str, dict[str, Any]]:
        """Return every registered boost role keyed by ``str(role_id)``."""
        return await self._get(guild_id, _ROLES_KEY)

    async def get_role(self, guild_id: int, role_id: int) -> dict[str, Any] | None:
        return (await self.get_roles(guild_id)).get(str(role_id))

    async def role_exists(self, guild_id: int, role_id: int) -> bool:
        return str(role_id) in await self.get_roles(guild_id)

    async def get_roles_by_owner(self, guild_id: int, user_id: int) -> dict[str, dict[str, Any]]:
        """Return roles owned by ``user_id``."""
        return {
            role_id: data
            for role_id, data in (await self.get_roles(guild_id)).items()
            if data.get("owner_id") == user_id
        }

    async def get_roles_as_member(self, guild_id: int, user_id: int) -> dict[str, dict[str, Any]]:
        """Return roles where ``user_id`` is a member (not the owner)."""
        return {
            role_id: data
            for role_id, data in (await self.get_roles(guild_id)).items()
            if user_id in data.get("members", [])
        }

    async def add_role(self, guild_id: int, role_id: int, owner_id: int, created_by: int) -> None:
        """Register a new boost role owned by ``owner_id``."""
        roles = await self.get_roles(guild_id)
        roles[str(role_id)] = {
            "owner_id": owner_id,
            "members": [],
            "created_at": datetime.now(UTC).isoformat(),
            "created_by": created_by,
        }
        await self._set(guild_id, _ROLES_KEY, roles)

    async def remove_role(self, guild_id: int, role_id: int) -> bool:
        roles = await self.get_roles(guild_id)
        if roles.pop(str(role_id), None) is not None:
            await self._set(guild_id, _ROLES_KEY, roles)
            return True
        return False

    async def transfer_ownership(self, guild_id: int, role_id: int, new_owner_id: int) -> bool:
        roles = await self.get_roles(guild_id)
        data = roles.get(str(role_id))
        if data is None:
            return False
        data["owner_id"] = new_owner_id
        await self._set(guild_id, _ROLES_KEY, roles)
        return True

    async def add_member(
        self, guild_id: int, role_id: int, member_id: int, max_members: int
    ) -> bool:
        """Add ``member_id`` to a role, respecting ``max_members``.

        Returns whether the member was added (already-present and full roles
        return ``False``).
        """
        roles = await self.get_roles(guild_id)
        data = roles.get(str(role_id))
        if data is None:
            return False
        members = list(data.get("members", []))
        if member_id in members or len(members) >= max_members:
            return False
        members.append(member_id)
        data["members"] = members
        await self._set(guild_id, _ROLES_KEY, roles)
        return True

    async def remove_member(self, guild_id: int, role_id: int, member_id: int) -> bool:
        roles = await self.get_roles(guild_id)
        data = roles.get(str(role_id))
        if data is None:
            return False
        members = list(data.get("members", []))
        if member_id not in members:
            return False
        members.remove(member_id)
        data["members"] = members
        await self._set(guild_id, _ROLES_KEY, roles)
        return True

    async def sync_members_with_guild(self, guild: discord.Guild) -> bool:
        """Reconcile stored members with the role's actual Discord membership.

        Members are kept in stored order; real members discovered missing from
        the store are appended at the end. Returns whether anything changed.
        """
        roles = await self.get_roles(guild.id)
        changed = False
        for role_id_str, data in roles.items():
            try:
                role_id = int(role_id_str)
            except ValueError:
                continue
            role = guild.get_role(role_id)
            if role is None:
                continue
            stored = list(data.get("members", []))
            owner_id = data.get("owner_id")
            actual = {member.id for member in role.members}
            if owner_id in actual:
                actual.discard(owner_id)
            kept = [member_id for member_id in stored if member_id in actual]
            for member_id in sorted(actual):
                if member_id not in kept:
                    kept.append(member_id)
            if kept != stored:
                data["members"] = kept
                changed = True
        if changed:
            await self._set(guild.id, _ROLES_KEY, roles)
        return changed

    # -- invites ------------------------------------------------------------

    async def add_invite(
        self,
        guild_id: int,
        invite_id: str,
        role_id: int,
        inviter_id: int,
        invitee_id: int,
        expires_at: datetime,
    ) -> None:
        invites = await self._get(guild_id, _INVITES_KEY)
        invites[invite_id] = {
            "role_id": role_id,
            "inviter_id": inviter_id,
            "invitee_id": invitee_id,
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        await self._set(guild_id, _INVITES_KEY, invites)

    async def remove_invite(self, guild_id: int, invite_id: str) -> bool:
        invites = await self._get(guild_id, _INVITES_KEY)
        if invites.pop(invite_id, None) is not None:
            await self._set(guild_id, _INVITES_KEY, invites)
            return True
        return False

    async def get_invite(self, guild_id: int, invite_id: str) -> dict[str, Any] | None:
        return (await self._get(guild_id, _INVITES_KEY)).get(invite_id)

    async def get_invites(self, guild_id: int) -> dict[str, dict[str, Any]]:
        """Return every pending invite keyed by invite id."""
        return await self._get(guild_id, _INVITES_KEY)

    async def get_invites_by_role(self, guild_id: int, role_id: int) -> dict[str, dict[str, Any]]:
        return {
            invite_id: data
            for invite_id, data in (await self._get(guild_id, _INVITES_KEY)).items()
            if data.get("role_id") == role_id
        }

    async def get_invites_by_user(self, guild_id: int, user_id: int) -> dict[str, dict[str, Any]]:
        return {
            invite_id: data
            for invite_id, data in (await self._get(guild_id, _INVITES_KEY)).items()
            if data.get("invitee_id") == user_id
        }

    async def get_invites_by_inviter(
        self, guild_id: int, user_id: int
    ) -> dict[str, dict[str, Any]]:
        """Return invites issued by ``user_id`` (invites this user sent)."""
        return {
            invite_id: data
            for invite_id, data in (await self._get(guild_id, _INVITES_KEY)).items()
            if data.get("inviter_id") == user_id
        }

    async def has_pending_invite(self, guild_id: int, role_id: int, invitee_id: int) -> bool:
        for data in (await self._get(guild_id, _INVITES_KEY)).values():
            if data.get("role_id") == role_id and data.get("invitee_id") == invitee_id:
                return True
        return False

    async def cleanup_expired_invites(
        self, guild_id: int, now: datetime | None = None
    ) -> list[str]:
        """Remove expired invites; returns the removed invite ids."""
        now = now or datetime.now(UTC)
        invites = await self._get(guild_id, _INVITES_KEY)
        expired: list[str] = []
        for invite_id, data in list(invites.items()):
            expires_at = parse_datetime(data.get("expires_at"))
            if expires_at is not None and expires_at <= now:
                expired.append(invite_id)
                invites.pop(invite_id, None)
        if expired:
            await self._set(guild_id, _INVITES_KEY, invites)
        return expired

    async def pending_invite_count(self, guild_id: int, role_id: int) -> int:
        return len(await self.get_invites_by_role(guild_id, role_id))
