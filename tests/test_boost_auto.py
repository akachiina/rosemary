"""Auto-created boost roles: the boost listener, unboost actions and catch-up.

``on_member_update`` drives the whole flow: a member who starts boosting gets a
role created below the configured anchor (or restored from the archive), and a
member whose boost ends loses it according to ``boost.auto_create.unboost_action``.
These tests lock the live Discord shapes the handlers touch (``premium_since``,
``role.colour.value``, ``unicode_emoji``, ``role in member.roles``) with
explicit fakes, so a discord.py-style rename breaks here instead of live.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import rosemary.core.card_specs  # noqa: F401  (fills the registry)
from rosemary.cogs.boost_roles import BoostRolesCog
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key

    async def raw(self, guild_id, key):
        return key


class FakeBot:
    theme = load_theme()

    def __init__(self, tmp_path) -> None:
        self.storage = GuildStorage(tmp_path)
        self.translator = FakeTranslator()
        self._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
        self.user = SimpleNamespace(id=42)
        self.guilds = []

    def get_guild(self, guild_id):
        return next((guild for guild in self.guilds if guild.id == guild_id), None)

    def get_channel(self, channel_id):
        return None


class FakeColour:
    def __init__(self, value: int) -> None:
        self.value = value


class FakeRole:
    _next_id = 1000

    def __init__(
        self, guild, name: str, color: int = 0, emoji: str | None = None, position: int = 1
    ) -> None:
        FakeRole._next_id += 1
        self.id = FakeRole._next_id
        self.guild = guild
        self.name = name
        self.colour = FakeColour(color)
        self.unicode_emoji = emoji
        self.icon = None
        self.position = position
        self.members: list = []
        self.edits: list[dict] = []
        self.deleted = False

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"

    def is_default(self) -> bool:
        return False

    async def edit(self, **kwargs) -> None:
        self.edits.append(kwargs)
        if "position" in kwargs:
            self.position = kwargs["position"]
        if "unicode_emoji" in kwargs:
            self.unicode_emoji = kwargs["unicode_emoji"]

    async def delete(self, **kwargs) -> None:
        self.deleted = True


class FakeMember:
    def __init__(self, guild, member_id, *, name=None, premium=False, bot=False) -> None:
        self.guild = guild
        self.id = member_id
        self.name = name or f"member{member_id}"
        self.display_name = self.name
        self.bot = bot
        self.premium_since = datetime.now(UTC) if premium else None
        self.roles: list[FakeRole] = []
        self.send = AsyncMock()

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    async def add_roles(self, *roles, **kwargs) -> None:
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)
            if self not in role.members:
                role.members.append(self)

    async def remove_roles(self, *roles, **kwargs) -> None:
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)
            if self in role.members:
                role.members.remove(self)


class FakeGuild:
    def __init__(self, guild_id=1, name="Girassol") -> None:
        self.id = guild_id
        self.name = name
        self.members: list[FakeMember] = []
        self.roles: list[FakeRole] = []
        self.created: list[FakeRole] = []

    def get_member(self, member_id):
        return next((member for member in self.members if member.id == member_id), None)

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)

    async def create_role(self, *, name, colour=None, reason=None, **kwargs):
        color = colour.value if colour is not None else 0
        role = FakeRole(self, name, color=color)
        self.roles.append(role)
        self.created.append(role)
        return role


async def _world(tmp_path, *, action=None, below_role=None):
    """Bot + cog + one guild with a boosting member and an invited friend."""
    bot = FakeBot(tmp_path)
    guild = FakeGuild()
    bot.guilds = [guild]
    cog = BoostRolesCog(bot)
    member = FakeMember(guild, 100, name="Rosa", premium=True)
    friend = FakeMember(guild, 200, name="Margarida")
    guild.members = [member, friend]
    if action is not None:
        await bot.storage.set(guild.id, "boost.auto_create.unboost_action", action)
    if below_role is not None:
        below_role.guild = guild
        guild.roles.append(below_role)
        await bot.storage.set(guild.id, "boost.auto_create.below_role", below_role.id)
    return bot, cog, guild, member, friend


def _boost_start(member):
    member.premium_since = datetime.now(UTC)
    before = SimpleNamespace(premium_since=None)
    return before, member


def _dm_contents(member):
    """Text of every DM sent to the member (positional content or kwarg)."""
    return [
        call.args[0] if call.args else call.kwargs.get("content")
        for call in member.send.call_args_list
    ]


def _boost_end(member):
    member.premium_since = None
    before = SimpleNamespace(premium_since=datetime.now(UTC))
    return before, member


async def _run(cog, before, after):
    await cog.on_member_update(before, after)


async def test_boost_start_creates_and_registers_the_role(tmp_path):
    bot, cog, guild, member, _friend = await _world(tmp_path)
    before, after = _boost_start(member)

    await _run(cog, before, after)

    assert len(guild.created) == 1
    role = guild.created[0]
    assert role.name == "Rosa"  # display name
    data = await cog.store.get_role(guild.id, role.id)
    assert data is not None
    assert data["owner_id"] == member.id
    assert data["auto"] is True
    assert data["created_by"] == bot.user.id
    assert role in member.roles  # the booster wears their role
    contents = _dm_contents(member)
    assert "boost.dm.auto_created" in contents


async def test_boost_start_places_role_below_the_anchor(tmp_path):
    anchor = FakeRole(None, "Staff", position=5)
    bot, cog, guild, member, _friend = await _world(tmp_path, below_role=anchor)

    await _run(cog, *_boost_start(member))

    role = guild.created[0]
    assert role.edits and role.edits[0].get("position") == anchor.position - 1


async def test_boost_start_without_reachable_anchor_keeps_the_bottom(tmp_path):
    anchor = FakeRole(None, "Fundo", position=1)  # too low to insert below
    bot, cog, guild, member, _friend = await _world(tmp_path, below_role=anchor)

    await _run(cog, *_boost_start(member))

    role = guild.created[0]
    assert role.edits == []  # stays where Discord created it
    assert await cog.store.get_archive(guild.id, member.id) is None


async def test_boost_start_missing_anchor_falls_back_to_bottom(tmp_path):
    bot, cog, guild, member, _friend = await _world(tmp_path)
    await bot.storage.set(guild.id, "boost.auto_create.below_role", 999999)

    await _run(cog, *_boost_start(member))

    role = guild.created[0]
    assert role.edits == []


async def test_boost_start_skips_when_auto_create_is_disabled(tmp_path):
    bot, cog, guild, member, _friend = await _world(tmp_path)
    await bot.storage.set(guild.id, "boost.auto_create.enabled", False)

    await _run(cog, *_boost_start(member))

    assert guild.created == []
    assert not await cog.store.get_roles(guild.id)


async def test_boost_start_skips_when_member_already_owns_a_role(tmp_path):
    bot, cog, guild, member, _friend = await _world(tmp_path)
    await cog.store.add_role(guild.id, 555, member.id, 7, auto=False)

    await _run(cog, *_boost_start(member))

    assert guild.created == []
    assert not await cog.store.get_auto_role_by_owner(guild.id, member.id)


async def test_member_update_ignores_unrelated_changes(tmp_path):
    bot, cog, guild, member, _friend = await _world(tmp_path)
    await _run(cog, SimpleNamespace(premium_since=member.premium_since), member)

    assert guild.created == []


async def test_unboost_delete_archives_and_next_boost_restores(tmp_path):
    bot, cog, guild, member, friend = await _world(tmp_path)  # default action: delete
    await _run(cog, *_boost_start(member))
    role = guild.created[0]
    await cog.store.add_member(guild.id, role.id, friend.id, max_members=10)
    await friend.add_roles(role)

    await _run(cog, *_boost_end(member))

    assert role.deleted is True
    assert await cog.store.get_role(guild.id, role.id) is None
    archive = await cog.store.get_archive(guild.id, member.id)
    assert archive is not None
    assert archive["members"] == [friend.id]
    contents = _dm_contents(member)
    assert "boost.dm.unboost_deleted" in contents

    member.send.reset_mock()
    await _run(cog, *_boost_start(member))

    assert len(guild.created) == 2  # a brand new role, not the deleted one
    restored = guild.created[1]
    assert restored.name == "Rosa"
    assert restored.colour.value == role.colour.value
    assert restored in member.roles
    assert restored in friend.roles  # previous members are back
    assert await cog.store.get_archive(guild.id, member.id) is None
    data = await cog.store.get_role(guild.id, restored.id)
    assert data["members"] == [friend.id]
    contents = _dm_contents(member)
    assert "boost.dm.auto_restored" in contents


async def test_unboost_remove_keeps_role_and_next_boost_reapplies(tmp_path):
    bot, cog, guild, member, friend = await _world(tmp_path, action="remove")
    await _run(cog, *_boost_start(member))
    role = guild.created[0]
    await cog.store.add_member(guild.id, role.id, friend.id, max_members=10)
    await friend.add_roles(role)

    await _run(cog, *_boost_end(member))

    assert role.deleted is False  # the role still exists
    assert await cog.store.get_role(guild.id, role.id) is not None  # still registered
    assert role not in member.roles and role not in friend.roles
    contents = _dm_contents(member)
    assert "boost.dm.unboost_removed" in contents

    member.send.reset_mock()
    await _run(cog, *_boost_start(member))

    assert len(guild.created) == 1  # no new role: the same one is handed back
    assert role in member.roles and role in friend.roles
    contents = _dm_contents(member)
    assert "boost.dm.auto_restored" in contents


async def test_unboost_keep_touches_nothing_but_notifies(tmp_path):
    bot, cog, guild, member, friend = await _world(tmp_path, action="keep")
    await _run(cog, *_boost_start(member))
    role = guild.created[0]
    await cog.store.add_member(guild.id, role.id, friend.id, max_members=10)
    await friend.add_roles(role)

    await _run(cog, *_boost_end(member))

    assert role.deleted is False
    assert role in member.roles and role in friend.roles
    assert await cog.store.get_role(guild.id, role.id) is not None
    contents = _dm_contents(member)
    assert "boost.dm.unboost_kept" in contents

    member.send.reset_mock()
    await _run(cog, *_boost_start(member))
    assert len(guild.created) == 1  # nothing new: the member never lost it
    assert not member.send.call_args_list


async def test_unboost_never_touches_manually_registered_roles(tmp_path):
    bot, cog, guild, member, _friend = await _world(tmp_path, action="delete")
    manual = FakeRole(guild, "Manual")
    guild.roles.append(manual)
    await cog.store.add_role(guild.id, manual.id, member.id, 7, auto=False)
    await member.add_roles(manual)

    await _run(cog, *_boost_end(member))

    assert manual.deleted is False
    assert await cog.store.get_role(guild.id, manual.id) is not None
    assert manual in member.roles
    assert await cog.store.get_archive(guild.id, member.id) is None
    assert not member.send.call_args_list


async def test_catchup_creates_for_boosters_who_boosted_offline(tmp_path):
    bot, cog, guild, member, friend = await _world(tmp_path)
    idle = FakeMember(guild, 300, name="Miojo", premium=False)
    guild.members.append(idle)

    await cog._catchup_boosters()

    assert len(guild.created) == 1  # only the booster
    role = guild.created[0]
    assert role.name == "Rosa"
    assert (await cog.store.get_role(guild.id, role.id))["auto"] is True


async def test_catchup_respects_the_disabled_setting(tmp_path):
    bot, cog, guild, member, _friend = await _world(tmp_path)
    await bot.storage.set(guild.id, "boost.auto_create.enabled", False)

    await cog._catchup_boosters()

    assert guild.created == []
