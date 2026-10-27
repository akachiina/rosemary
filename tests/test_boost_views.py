"""Build the boost menus with fakes to catch V2 construction regressions."""

from __future__ import annotations

import discord
import pytest

from rosemary.core.storage import GuildStorage
from rosemary.ui.boost_invite import ACCEPT_PREFIX, DECLINE_PREFIX, BoostInviteView
from rosemary.ui.boost_menu import AdminBoostMenuView, BoostMenuView, _Screen
from rosemary.ui.containers import ActionRow
from rosemary.ui.theme import load_theme


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        return key


class FakeBot:
    theme = load_theme()
    translator = FakeTranslator()

    def __init__(self, storage) -> None:
        self.storage = storage
        self.guilds = []

    def get_guild(self, guild_id):
        return None


class RoleWithIcon:
    """Stand-in role exposing the icon attributes the menus touch."""

    id = 123
    name = "test-role"
    mention = "<@&123>"
    color = "brand"
    unicode_emoji = None

    def __init__(self, has_icon: bool = True) -> None:
        icon = type("Icon", (), {"url": "https://example.com/icon.png"}) if has_icon else None
        self.icon = icon

    async def edit(self, **kwargs) -> None:
        return None


class GuildWithRoles:
    id = 1
    name = "test-guild"

    def __init__(self, has_icon: bool = True) -> None:
        self._role = RoleWithIcon(has_icon)
        self.member = None

    def get_role(self, role_id):
        return self._role

    def get_member(self, member_id):
        return self.member


class BotWithGuild(FakeBot):
    def __init__(self, storage, has_icon: bool = True) -> None:
        super().__init__(storage)
        self._guild = GuildWithRoles(has_icon)

    def get_guild(self, guild_id):
        return self._guild


@pytest.fixture
def bot(tmp_path):
    return FakeBot(GuildStorage(tmp_path))


async def test_boost_menu_home_builds(bot):
    view = BoostMenuView(bot, 1, owner_id=1)
    await view.prepare()
    assert view.children
    assert view.author_id == 1
    assert view.timeout is not None
    assert view.disable_on_timeout is True


async def test_boost_menu_registered_screens_build(bot):
    for screen in (
        _Screen.REGISTER,
        _Screen.REGISTER_OWNER,
        _Screen.LEAVE,
        _Screen.MY_ROLES,
        _Screen.PENDING,
    ):
        view = BoostMenuView(bot, 1, owner_id=1)
        view.screen = screen
        await view.prepare()
        assert view.children


async def test_admin_boost_menu_home_builds(bot):
    view = AdminBoostMenuView(bot, 1, owner_id=1)
    await view.prepare()
    assert view.children


async def test_admin_boost_menu_screens_build(bot):
    for screen in (
        _Screen.REGISTER,
        _Screen.REGISTER_OWNER,
        _Screen.INVITE_TARGET,
        _Screen.REMOVE_CONFIRM,
        _Screen.MY_ROLES,
    ):
        view = AdminBoostMenuView(bot, 1, owner_id=1)
        view.screen = screen
        await view.prepare()
        assert view.children


class FakeUser:
    id = 42


class FakeMember:
    id = 555
    bot = False
    mention = "<@555>"


class FakeResponse:
    """Record ack calls so tests can assert response ordering."""

    def __init__(self, order: list[str]) -> None:
        self._order = order
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self) -> None:
        self._order.append("defer")
        self._done = True


class FakeInteraction:
    def __init__(self, custom_id="", data=None):
        self.order: list[str] = []
        self.response = FakeResponse(self.order)
        self.data = data or {}
        self.custom_id = custom_id
        self.id = 999
        self.user = FakeUser()
        self.message = None

    async def edit(self, **kwargs) -> None:
        self.order.append("edit")


async def test_pick_register_owner_acks_before_rerender(monkeypatch, bot):
    """Slow handlers must defer immediately or Discord invalidates the token (10062)."""
    import rosemary.ui.boost_dm as boost_dm

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(boost_dm, "send", noop)
    monkeypatch.setattr(boost_dm, "send_preview", noop)

    guild_bot = BotWithGuild(bot.storage)
    guild_bot._guild.member = FakeMember()
    guild_bot._guild._role.guild = guild_bot._guild

    view = AdminBoostMenuView(guild_bot, 1, owner_id=1)
    view.screen = _Screen.REGISTER_OWNER
    view.role_id = 123

    interaction = FakeInteraction(data={"values": ["555"]})
    await view._pick_register_owner(interaction)

    assert interaction.order == ["defer", "edit"]
    assert await view.store.role_exists(1, 123)


async def test_invite_view_is_persistent(bot):
    view = BoostInviteView(
        bot,
        guild_id=1,
        invite_id="10-20-30",
        accept_label="Accept",
        decline_label="Decline",
    )
    assert view.timeout is None
    assert view.is_persistent() is True
    custom_ids = [
        item.custom_id for item in view.walk_children() if getattr(item, "custom_id", None)
    ]
    assert f"{ACCEPT_PREFIX}:10-20-30" in custom_ids
    assert f"{DECLINE_PREFIX}:10-20-30" in custom_ids


async def test_invite_id_parsing():
    from rosemary.ui.boost_invite import invite_id_from_custom_id

    assert invite_id_from_custom_id("boost_invite_accept:abc", ACCEPT_PREFIX) == "abc"
    assert invite_id_from_custom_id("boost_invite_decline:abc", DECLINE_PREFIX) == "abc"
    assert invite_id_from_custom_id("other", ACCEPT_PREFIX) is None


class FakeSendUser:
    """Records every send() call to catch V2/content mixing regressions."""

    id = 7
    mention = "<@7>"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, **kwargs) -> None:
        self.calls.append(kwargs)


class EditRecorder:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def edit_message(self, **kwargs) -> None:
        self.kwargs = kwargs


class EditInteraction:
    def __init__(self) -> None:
        self.response = EditRecorder()
        self.kwargs: dict | None = None

    async def edit(self, **kwargs) -> None:
        # Mirrors Interaction.edit: routes depending on deferred state.
        self.kwargs = kwargs


async def test_invite_view_is_buttons_only(bot):
    view = BoostInviteView(
        bot,
        guild_id=1,
        invite_id="10-20-30",
        accept_label="Accept",
        decline_label="Decline",
    )
    assert [type(item).__name__ for item in view.children] == ["ActionRow"]


async def test_dm_invite_sends_text_and_v2_view_separately(monkeypatch, bot):
    """Components V2 messages cannot carry content: two separate sends."""
    from datetime import UTC, datetime, timedelta

    import rosemary.ui.boost_dm as boost_dm

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(boost_dm, "send_preview", noop)

    guild_bot = BotWithGuild(bot.storage)
    guild = guild_bot._guild
    guild._role.guild = guild
    member = FakeSendUser()
    menu = BoostMenuView(guild_bot, 1, owner_id=1)
    expires = datetime.now(UTC) + timedelta(hours=1)

    await menu._dm_invite(guild.get_role(123), member, {"owner_id": 1}, "123-7-999", expires)

    assert len(member.calls) == 2
    assert set(member.calls[0]) == {"content"}
    assert set(member.calls[1]) == {"view"}
    assert isinstance(member.calls[1]["view"], BoostInviteView)


async def test_invite_edit_replaces_components_without_content(bot):
    view = BoostInviteView(
        bot,
        guild_id=1,
        invite_id="10-20-30",
        accept_label="Accept",
        decline_label="Decline",
    )
    interaction = EditInteraction()

    await view._edit(interaction, "done")

    assert interaction.kwargs.keys() == {"view"}
    assert interaction.kwargs["view"] is view
    kinds = [type(item).__name__ for item in view.children]
    assert kinds == ["TextDisplay", "ActionRow"]
    assert view.accept_button.disabled is True
    assert view.decline_button.disabled is True


#: component placement validation ------------------------------------------


def _validate_components(d, path, errors):
    """Walk a component dict tree asserting Discord's V2 placement rules.

    Thumbnails may only be a Section accessory; Section items must be
    TextDisplay (10); Container children must be ActionRow/TextDisplay/
    MediaGallery/File/Separator/Section.
    """
    ctype = d.get("type")
    if ctype == 9:  # Section
        for child in d.get("components", []):
            if child.get("type") != 10:
                errors.append(f"{path}: Section item type {child.get('type')} (not 10)")
        accessory = d.get("accessory")
        if accessory is not None and accessory.get("type") not in (11, 2):
            errors.append(f"{path}: Section accessory type {accessory.get('type')}")
    elif ctype == 17:  # Container
        for child in d.get("components", []):
            if child.get("type") not in (1, 9, 10, 12, 13, 14):
                errors.append(f"{path}: Container child type {child.get('type')}")
            _validate_components(child, f"{path}/child", errors)
    elif ctype == 11:  # Thumbnail must only appear as a Section accessory
        errors.append(f"{path}: Thumbnail outside a Section accessory")


def _assert_valid_layout(view, label):
    errors = []
    for item in view.to_components():
        d = item.to_component_dict() if not isinstance(item, dict) else item
        _validate_components(d, label, errors)
    assert errors == [], "\n".join(errors)


@pytest.mark.parametrize(
    "screen",
    [
        _Screen.HOME,
        _Screen.REGISTER,
        _Screen.REGISTER_OWNER,
        _Screen.LEAVE,
        _Screen.MY_ROLES,
        _Screen.PENDING,
        _Screen.MANAGE,
        _Screen.COLOR,
        _Screen.MEMBERS,
        _Screen.INVITES,
        _Screen.INVITE_TARGET,
        _Screen.LEAVE_CONFIRM,
        _Screen.REMOVE_CONFIRM,
    ],
)
@pytest.mark.parametrize("has_icon", [True, False])
async def test_boost_menu_layout_valid_with_icon(bot, screen, has_icon):
    bot = BotWithGuild(bot.storage, has_icon=has_icon)
    view = BoostMenuView(bot, 1, owner_id=1)
    view.screen = screen
    view.role_id = 123
    await view.prepare()
    _assert_valid_layout(view, f"boost_menu.{screen}")


async def test_boost_invite_view_layout_valid(bot):
    view = BoostInviteView(
        bot,
        guild_id=1,
        invite_id="10-20-30",
        accept_label="Accept",
        decline_label="Decline",
    )
    _assert_valid_layout(view, "boost_invite")


#: duplicate custom_id guard -------------------------------------------------


def _payload_custom_ids(view) -> list[str]:
    """Collect custom_ids from the exact serialized payload, accessory included.

    ``walk_children`` already reaches Section accessories, but walking the
    ``to_components()`` dicts pins the contract against py-cord serialization
    changes instead of our assumptions about the item tree.
    """
    ids: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            custom_id = node.get("custom_id")
            if custom_id:
                ids.append(custom_id)
            accessory = node.get("accessory")
            if isinstance(accessory, dict):
                walk(accessory)
            for child in node.get("components", []):
                walk(child)

    for top in view.to_components():
        walk(top)
    return ids


@pytest.mark.usefixtures("bot")
@pytest.mark.parametrize("screen", list(_Screen))
async def test_every_screen_serializes_unique_custom_ids(bot, screen):
    """Discord rejects the WHOLE payload (400 50035) when two components share
    a custom_id; the members screen used to ship two ``boost_back`` buttons,
    poisoning every later edit of the menu (live: "rerender edit failed")."""
    for cls in (BoostMenuView, AdminBoostMenuView):
        view = cls(bot, 1, owner_id=1)
        view.screen = screen
        view.role_id = 123
        await view.prepare()
        ids = _payload_custom_ids(view)
        assert len(ids) == len(set(ids)), (
            f"{cls.__name__}/{screen}: duplicate custom_ids {sorted(ids)}"
        )


async def test_prepare_rejects_duplicate_custom_ids(bot):
    """The guard must fail at build time, not as a live-only Discord 400."""
    async def duplicate_build(self):
        return [
            ActionRow(discord.ui.Button(custom_id="same", label="a")),
            ActionRow(discord.ui.Button(custom_id="same", label="b")),
        ]

    view = BoostMenuView(bot, 1, owner_id=1)
    view.build_items = duplicate_build.__get__(view, BoostMenuView)
    with pytest.raises(ValueError, match="duplicate custom_id"):
        await view.prepare()


#: modal handlers rebuild before editing -------------------------------------


class _FakeHTTPResponse:
    status = 403
    reason = "Forbidden"


async def test_emoji_submit_boost_forbidden_flashes_and_rerenders(monkeypatch, bot):
    """Editing the emoji on a guild without boost level 2 fails with 403 50101;
    the handler must flash the localized error and re-render (never let the
    exception escape the modal dispatch)."""
    guild_bot = BotWithGuild(bot.storage)

    class ForbiddenRole(RoleWithIcon):
        async def edit(self, **kwargs):
            raise discord.Forbidden(
                _FakeHTTPResponse(),
                {"code": 50101, "message": "This server needs more boosts"},
            )

    guild_bot._guild._role = ForbiddenRole(True)
    view = BoostMenuView(guild_bot, 1, owner_id=1)
    view.role_id = 123
    rerenders: list[object] = []

    async def fake_rerender(interaction):
        rerenders.append(interaction)

    monkeypatch.setattr(view, "rerender", fake_rerender)
    interaction = FakeInteraction()

    await view._emoji_submit(interaction, "\U0001F451")

    assert rerenders, "handler must re-render after the forbidden role edit"
    assert view.flash == "boost.errors.requires_boost_level_2"
    assert interaction.order == ["defer"]


async def test_rename_submit_empty_name_rebuilds_before_edit(monkeypatch, bot):
    """Modal handlers must run prepare() before editing: a bare edit serializes
    the stale previous screen and can 400 over the validation flash."""
    view = BoostMenuView(bot, 1, owner_id=1)
    rerenders: list[object] = []

    async def fake_rerender(interaction):
        rerenders.append(interaction)

    monkeypatch.setattr(view, "rerender", fake_rerender)
    interaction = FakeInteraction()

    await view._rename_submit(interaction, "   ")

    assert rerenders
    assert view.flash == "boost.errors.empty_name"


#: membership grows only through invites ------------------------------------


async def test_invite_view_is_storable(bot):
    """The DM view must be storable: py-cord's ViewStore skips views with
    ``store=False`` (both the send-time registration and the startup
    ``bot.add_view``), so every click would die as "did not respond"."""
    view = BoostInviteView(
        bot,
        guild_id=1,
        invite_id="10-20-30",
        accept_label="Accept",
        decline_label="Decline",
    )
    assert view._store is True


async def test_members_screen_offers_invite_not_direct_add(bot):
    """The members screen must route growth through the invite flow: no user
    select and no by-ID modal button, only the invite button + navigation."""
    bot = BotWithGuild(bot.storage)
    await bot.storage.set(1, "boost_roles", {"123": {"owner_id": 1, "members": []}})
    view = BoostMenuView(bot, 1, owner_id=1)
    view.screen = _Screen.MEMBERS
    view.role_id = 123
    await view.prepare()
    custom_ids = [
        item.custom_id for item in view.walk_children() if getattr(item, "custom_id", None)
    ]
    assert "boost_add_invite:123" in custom_ids
    assert not any(cid.startswith("boost_pick_member:") for cid in custom_ids)
    assert not any(cid.startswith("boost_by_id_member:") for cid in custom_ids)
    # Selects are only allowed for removal (per-member buttons are not selects).
    assert not [i for i in view.walk_children() if type(i).__name__ == "UserSelect"]


async def test_accept_registers_member_in_store(monkeypatch, bot):
    """Accepting an invite grants the role AND tracks the membership in the
    store (ownership transfer, limits and menus read the store, not Discord)."""
    from datetime import UTC, datetime, timedelta

    import rosemary.ui.boost_dm as boost_dm
    import rosemary.ui.boost_invite as boost_invite

    added: list[tuple[int, str]] = []
    granted: list[int] = []

    class FakeMemberObj:
        id = 555
        display_avatar = None

        async def add_roles(self, role, reason=None):
            granted.append(role.id)

    class FakeGuild:
        id = 1

        def get_role(self, role_id):
            return RoleWithIcon(True) if role_id == 123 else None

        def get_member(self, member_id):
            return FakeMemberObj() if member_id == 555 else None

    async def noop_dm(*args, **kwargs):
        return None

    async def noop_log(*args, **kwargs):
        return None

    monkeypatch.setattr(boost_dm, "send", noop_dm)
    monkeypatch.setattr(boost_dm, "send_preview", noop_dm)
    monkeypatch.setattr(boost_invite.BoostInviteView, "_log", noop_log)

    expires = datetime.now(UTC) + timedelta(hours=1)
    guild_bot = BotWithGuild(bot.storage)
    guild_bot._guild.member = FakeMemberObj()
    await guild_bot.storage.set(1, "boost_roles", {"123": {"owner_id": 1, "members": []}})
    store = boost_invite.BoostRoleStore(guild_bot.storage.data_dir)
    await store.add_role(1, 123, owner_id=1, created_by=1)
    await store.add_invite(1, "inv1", 123, inviter_id=1, invitee_id=555, expires_at=expires)

    view = BoostInviteView(
        guild_bot, guild_id=1, invite_id="inv1", accept_label="A", decline_label="D"
    )
    real_add = store.add_member

    async def spy_add_member(guild_id, role_id, member_id, max_members):
        added.append((role_id, member_id))
        return await real_add(guild_id, role_id, member_id, max_members)

    monkeypatch.setattr(view.store, "add_member", spy_add_member)
    interaction = FakeInteraction()
    interaction.user = FakeUser()
    interaction.user.id = 555

    await view._accept(interaction)

    assert added == [(123, 555)]
    assert granted == [123]
    data = await store.get_role(1, 123)
    assert 555 in data["members"]
    assert await store.get_invite(1, "inv1") is None


#: ownership transfer survives and reaches the new owner's screen -----------


async def test_manual_transfer_then_my_roles_lists_role_once(monkeypatch, bot):
    """The reported live bug: after an ownership transfer the new owner's
    "my roles" screen must list the role exactly once (as owner, never also
    as a member), with the registration intact."""
    import rosemary.ui.boost_dm as boost_dm

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(boost_dm, "send", noop)
    monkeypatch.setattr(boost_dm, "send_preview", noop)

    guild_bot = BotWithGuild(bot.storage)
    guild_bot._guild.member = FakeMember()  # the transfer target, id 555
    guild_bot._guild._role.guild = guild_bot._guild

    admin = AdminBoostMenuView(guild_bot, 1, owner_id=1)
    await admin.store.add_role(1, 123, owner_id=42, created_by=1)
    await admin.store.add_member(1, 123, 555, max_members=5)

    await admin._transfer_to(FakeInteraction(), 123, 555)

    data = await admin.store.get_role(1, 123)
    assert data is not None, "the registration must survive the transfer"
    assert data["owner_id"] == 555
    assert data["members"] == []

    mine = BoostMenuView(guild_bot, 1, owner_id=555)
    mine.screen = _Screen.MY_ROLES
    await mine.prepare()
    texts = [
        getattr(item, "content", "")
        for item in mine.walk_children()
        if type(item).__name__ == "TextDisplay"
    ]
    assert "\n".join(texts).count("@test-role") == 1
    custom_ids = [
        item.custom_id for item in mine.walk_children() if getattr(item, "custom_id", None)
    ]
    assert "boost_manage:123" in custom_ids
    assert "boost_leave_confirm:123" not in custom_ids
