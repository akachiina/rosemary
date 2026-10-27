"""Tests for ``rosemary.core.boost_roles`` (store + role helpers)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

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
    assert data["auto"] is False
    assert "created_at" in data

    assert (await store.get_roles_by_owner(1, 100)).get("10")
    assert await store.get_roles_by_owner(1, 999) == {}

    assert await store.transfer_ownership(1, 10, 200) is True
    assert (await store.get_role(1, 10))["owner_id"] == 200

    assert await store.remove_role(1, 10) is True
    assert await store.role_exists(1, 10) is False
    assert await store.remove_role(1, 10) is False


async def test_transfer_strips_new_owner_from_members(tmp_path):
    """Store invariant: the owner is never also a member. Transferring to a
    current member must drop them from the member list; live, the missing
    strip listed the role twice in the "my roles" screen and kept their slot
    counted against ``max_members``.
    """
    store = _store(tmp_path)
    await store.add_role(1, 10, owner_id=100, created_by=100)
    await store.add_member(1, 10, 200, max_members=5)
    await store.add_member(1, 10, 300, max_members=5)

    assert await store.transfer_ownership(1, 10, 200) is True

    data = await store.get_role(1, 10)
    assert data["owner_id"] == 200
    assert data["members"] == [300]
    assert await store.get_roles_as_member(1, 200) == {}
    assert (await store.get_roles_by_owner(1, 200)).get("10")


async def test_auto_flag_marks_bot_created_roles(tmp_path):
    store = _store(tmp_path)
    await store.add_role(1, 10, owner_id=100, created_by=42, auto=True)
    await store.add_role(1, 20, owner_id=100, created_by=7, auto=False)

    assert (await store.get_role(1, 10))["auto"] is True
    assert (await store.get_role(1, 20))["auto"] is False

    assert await store.get_auto_role_by_owner(1, 100) == (10, await store.get_role(1, 10))
    assert await store.get_auto_role_by_owner(1, 999) is None


async def test_archive_roundtrip(tmp_path):
    store = _store(tmp_path)
    await store.add_role(1, 10, owner_id=100, created_by=42, auto=True)
    await store.add_member(1, 10, 200, max_members=5)
    assert await store.get_archive(1, 100) is None

    entry = await store.archive_role(
        1, 10, role_name="Rosa", color=0xFFB7C5, emoji="🌹"
    )
    assert entry is not None
    assert entry["role_name"] == "Rosa"
    assert entry["color"] == 0xFFB7C5
    assert entry["emoji"] == "🌹"
    assert entry["members"] == [200]
    assert "archived_at" in entry

    # the registration is gone, the memory stays
    assert await store.role_exists(1, 10) is False
    assert await store.get_archive(1, 100) == entry

    popped = await store.pop_archive(1, 100)
    assert popped == entry
    assert await store.get_archive(1, 100) is None
    assert await store.pop_archive(1, 100) is None

    # archiving an unknown role is a no-op
    assert await store.archive_role(1, 77, role_name="x", color=0, emoji=None) is None


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


#: on_member_remove: transfer or unregister -------------------------------


class _FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id
        self.name = f"role-{role_id}"
        self.mention = f"<@&{role_id}>"


class _FakeMember:
    def __init__(self, member_id: int, *, bot: bool = False) -> None:
        self.id = member_id
        self.bot = bot
        self.mention = f"<@{member_id}>"


class _FakeGuild:
    def __init__(self, roles=(), members=()) -> None:
        self.id = 1
        self.name = "Guild"
        self._roles = {role.id: role for role in roles}
        self._members = {member.id: member for member in members}

    def get_role(self, role_id):
        return self._roles.get(role_id)

    def get_member(self, member_id):
        return self._members.get(member_id)


def _boost_cog(tmp_path, monkeypatch):
    """A cog over a tmp store, with every DM and log call spied."""
    import rosemary.cogs.boost_roles as boost_roles_cog
    import rosemary.ui.boost_dm as boost_dm

    bot = MagicMock()
    bot.storage.data_dir = tmp_path
    world = SimpleNamespace(logs=[], dm_keys=[], previews=[])

    async def spy_send(*args, **kwargs):
        world.dm_keys.append(str(args[3]))

    async def spy_preview(*args, **kwargs):
        world.previews.append(args[3].id)

    async def spy_log(_bot, _guild_id, event, **variables):
        world.logs.append((event, variables))

    monkeypatch.setattr(boost_dm, "send", spy_send)
    monkeypatch.setattr(boost_dm, "send_preview", spy_preview)
    monkeypatch.setattr(boost_roles_cog, "send_log", spy_log)
    world.cog = boost_roles_cog.BoostRolesCog(bot)
    world.store = world.cog.store
    return world


def _departed(world, guild):
    member = _FakeMember(100)
    member.guild = guild
    return member


async def test_departed_owner_transfers_role_and_registration_survives(tmp_path, monkeypatch):
    """Live bug: the listener unregistered the role before the (then no-op)
    ownership transfer, so the role never showed up in the new owner's "my
    roles" screen even though the DM and the log claimed it had.
    """
    world = _boost_cog(tmp_path, monkeypatch)
    await world.store.add_role(1, 10, owner_id=100, created_by=100)
    await world.store.add_member(1, 10, 200, max_members=5)
    expires = datetime.now(UTC) + timedelta(hours=1)
    await world.store.add_invite(
        1, "inv1", 10, inviter_id=100, invitee_id=300, expires_at=expires
    )

    guild = _FakeGuild(roles=[_FakeRole(10)], members=[_FakeMember(200)])

    await world.cog.on_member_remove(_departed(world, guild))

    data = await world.store.get_role(1, 10)
    assert data is not None, "the registration must survive the transfer"
    assert data["owner_id"] == 200
    assert data["members"] == []  # the new owner leaves the member list
    assert await world.store.get_invite(1, "inv1") is None  # invites die with the owner
    assert world.dm_keys == ["boost.dm.auto_transferred"]
    assert world.previews == [10]
    assert world.logs[-1] == (
        "boost.logs.auto_transfer",
        {"role": "<@&10>", "old_owner": "<@100>", "new_owner": "<@200>"},
    )


async def test_departed_owner_role_goes_to_first_resolvable_human(tmp_path, monkeypatch):
    """Stale entries (the departed owner themselves, bots) never inherit the
    role; the first resolvable human member does."""
    world = _boost_cog(tmp_path, monkeypatch)
    await world.store.add_role(1, 10, owner_id=100, created_by=100)
    for member_id in (100, 999, 200):
        await world.store.add_member(1, 10, member_id, max_members=5)

    guild = _FakeGuild(
        roles=[_FakeRole(10)],
        members=[_FakeMember(100), _FakeMember(999, bot=True), _FakeMember(200)],
    )

    await world.cog.on_member_remove(_departed(world, guild))

    data = await world.store.get_role(1, 10)
    assert data is not None
    assert data["owner_id"] == 200
    # the departed owner's own legacy membership is cleaned by the listener
    assert data["members"] == [999]


async def test_departed_owner_without_members_unregisters(tmp_path, monkeypatch):
    world = _boost_cog(tmp_path, monkeypatch)
    await world.store.add_role(1, 10, owner_id=100, created_by=100)

    guild = _FakeGuild(roles=[_FakeRole(10)])

    await world.cog.on_member_remove(_departed(world, guild))

    assert await world.store.role_exists(1, 10) is False
    assert world.dm_keys == []
    assert world.previews == []
    assert world.logs[-1][0] == "boost.logs.orphan_remove"


async def test_departed_owner_with_only_gone_members_unregisters(tmp_path, monkeypatch):
    """Members stored but nobody resolvable (they left while the bot was
    offline): unregister and log it, never a silent wipe."""
    world = _boost_cog(tmp_path, monkeypatch)
    await world.store.add_role(1, 10, owner_id=100, created_by=100)
    await world.store.add_member(1, 10, 555, max_members=5)

    guild = _FakeGuild(roles=[_FakeRole(10)])  # 555 is not in the member cache

    await world.cog.on_member_remove(_departed(world, guild))

    assert await world.store.role_exists(1, 10) is False
    assert world.dm_keys == []
    assert world.logs[-1][0] == "boost.logs.orphan_remove"


async def test_departed_member_loses_memberships_but_owner_keeps_role(tmp_path, monkeypatch):
    world = _boost_cog(tmp_path, monkeypatch)
    await world.store.add_role(1, 10, owner_id=100, created_by=100)
    await world.store.add_role(1, 20, owner_id=300, created_by=300)
    await world.store.add_member(1, 20, 100, max_members=5)

    guild = _FakeGuild(roles=[_FakeRole(20)], members=[_FakeMember(300)])

    await world.cog.on_member_remove(_departed(world, guild))

    data = await world.store.get_role(1, 20)
    assert data["owner_id"] == 300
    assert data["members"] == []
    assert await world.store.role_exists(1, 10) is False  # orphan role, no members
