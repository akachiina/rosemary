"""Tests for ``rosemary.core.boost_roles`` (store + role helpers)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from rosemary.core.boost_roles import BoostRoleStore, is_pickable_role
from rosemary.core.storage import GuildStorage


def _store(tmp_path) -> BoostRoleStore:
    return BoostRoleStore(tmp_path)


async def test_storage_uses_separate_json_file(tmp_path):
    store = _store(tmp_path)
    await store.add_role(1, 10, owner_id=100, created_by=100)
    boost_file = tmp_path / "1" / "boost_roles.json"
    settings_file = tmp_path / "1" / "settings.json"
    assert boost_file.exists()
    assert not settings_file.exists()
    with boost_file.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert "boost_roles" in data and "boost_invites" not in data
    assert data["boost_roles"]["10"]["owner_id"] == 100


async def test_legacy_settings_keys_can_be_removed(tmp_path):
    settings = GuildStorage(tmp_path)
    await settings.set(1, "boost_roles", {"10": {"owner_id": 100, "members": []}})
    await settings.set(1, "boost_invites", {})
    await settings.set(1, "language", "pt-BR")
    await settings.delete_keys(1, "boost_roles", "boost_invites")
    assert await settings.get(1) == {"language": "pt-BR"}



def test_is_pickable_role():
    everyone = SimpleNamespace(id=0, name="@everyone", is_default=lambda: True, managed=False)
    managed = SimpleNamespace(id=1, name="Managed", is_default=lambda: False, managed=True)
    normal = SimpleNamespace(id=2, name="Boost", is_default=lambda: False, managed=False)
    assert not is_pickable_role(everyone)
    assert not is_pickable_role(managed)
    assert is_pickable_role(normal)


async def test_role_roundtrip(tmp_path):
    store = _store(tmp_path)
    assert await store.role_exists(1, 10) is False
    assert await store.get_role(1, 10) is None

    await store.add_role(1, 10, owner_id=100, created_by=100)
    assert await store.role_exists(1, 10) is True
    data = await store.get_role(1, 10)
    assert data["owner_id"] == 100
    assert data["members"] == []
    assert "created_at" in data

    assert (await store.get_roles_by_owner(1, 100)).get("10")
    assert await store.get_roles_by_owner(1, 999) == {}

    assert await store.transfer_ownership(1, 10, 200) is True
    assert (await store.get_role(1, 10))["owner_id"] == 200

    assert await store.remove_role(1, 10) is True
    assert await store.role_exists(1, 10) is False
    assert await store.remove_role(1, 10) is False


async def test_role_members(tmp_path):
    store = _store(tmp_path)
    await store.add_role(1, 10, owner_id=100, created_by=100)

    assert await store.add_member(1, 10, 200, max_members=2) is True
    assert await store.add_member(1, 10, 200, max_members=2) is False  # already present
    assert await store.add_member(1, 10, 300, max_members=2) is True
    assert await store.add_member(1, 10, 400, max_members=2) is False  # full
    assert await store.add_member(1, 99, 500, max_members=5) is False  # unknown role

    assert set((await store.get_role(1, 10))["members"]) == {200, 300}
    assert (await store.get_roles_as_member(1, 200)).get("10")
    assert await store.get_roles_as_member(1, 100) == {}  # owner is not a member

    assert await store.remove_member(1, 10, 200) is True
    assert await store.remove_member(1, 10, 200) is False
    assert (await store.get_role(1, 10))["members"] == [300]


async def test_invite_roundtrip(tmp_path):
    store = _store(tmp_path)
    expires = datetime.now(UTC) + timedelta(hours=1)
    await store.add_invite(1, "inv1", 10, inviter_id=100, invitee_id=200, expires_at=expires)

    data = await store.get_invite(1, "inv1")
    assert data["role_id"] == 10
    assert data["invitee_id"] == 200
    assert await store.has_pending_invite(1, 10, 200) is True
    assert await store.has_pending_invite(1, 10, 999) is False
    assert await store.pending_invite_count(1, 10) == 1

    assert (await store.get_invites_by_role(1, 10)).get("inv1")
    assert (await store.get_invites_by_user(1, 200)).get("inv1")
    assert (await store.get_invites_by_inviter(1, 100)).get("inv1")

    assert await store.remove_invite(1, "inv1") is True
    assert await store.remove_invite(1, "inv1") is False


async def test_invite_cleanup_expired(tmp_path):
    store = _store(tmp_path)
    now = datetime.now(UTC)
    await store.add_invite(1, "old", 10, 100, 200, expires_at=now - timedelta(minutes=5))
    await store.add_invite(1, "fresh", 10, 100, 300, expires_at=now + timedelta(minutes=5))

    assert sorted(await store.cleanup_expired_invites(1, now=now)) == ["old"]
    assert await store.get_invite(1, "old") is None
    assert await store.get_invite(1, "fresh") is not None


async def test_sync_members_with_guild(tmp_path):
    store = _store(tmp_path)
    await store.add_role(1, 10, owner_id=100, created_by=100)
    await store.add_member(1, 10, 200, max_members=5)

    member200 = SimpleNamespace(id=200)
    member400 = SimpleNamespace(id=400)

    class Role:
        def __init__(self) -> None:
            self.members = [member200, member400]

    role = Role()

    class Guild:
        id = 1

        def get_role(self, role_id):
            return role if role_id == 10 else None

    changed = await store.sync_members_with_guild(Guild())
    assert changed is True
    assert (await store.get_role(1, 10))["members"] == [200, 400]

    role.members = [member200, member400]
    assert await store.sync_members_with_guild(Guild()) is False
