"""Build the boost menus with fakes to catch V2 construction regressions."""

from __future__ import annotations

import pytest

from rosemary.core.storage import GuildStorage
from rosemary.ui.boost_invite import ACCEPT_PREFIX, DECLINE_PREFIX, BoostInviteView
from rosemary.ui.boost_menu import AdminBoostMenuView, BoostMenuView, _Screen
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


# -- component placement validation ------------------------------------------


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
