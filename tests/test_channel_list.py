"""CHANNEL_LIST settings: registry, management screens and anti-invite use.

Covers the generic multi-place setting type (coerce/format/legacy migration),
the /settings management screens (rows with Remove, batch Add via a native
multi-select, Clear all, pagination) and the anti-invite listener honoring
channels and whole categories.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import yaml

from rosemary.core.settings import (
    SETTINGS,
    SettingSpec,
    SettingType,
    coerce_value,
    format_value,
    get_setting,
    set_setting,
)
from rosemary.core.storage import GuildStorage
from rosemary.ui.settings_menu import LIST_ITEMS_PER_PAGE, SettingsMenuView

_LIST_SPEC = SettingSpec(
    key="test.places",
    category=None,  # type: ignore[arg-type]
    value_type=SettingType.CHANNEL_LIST,
    default=(),
)


#: coercion and display -------------------------------------------------------


def test_channel_list_coerce_accepts_lists_scalars_and_empty():
    assert coerce_value(_LIST_SPEC, [1, 2, 3]) == (True, (1, 2, 3))
    assert coerce_value(_LIST_SPEC, 7) == (True, (7,))
    assert coerce_value(_LIST_SPEC, "7") == (True, (7,))
    assert coerce_value(_LIST_SPEC, ()) == (True, ())
    assert coerce_value(_LIST_SPEC, None) == (True, ())
    assert coerce_value(_LIST_SPEC, 0) == (True, ())
    # Deduplicated, order preserved.
    assert coerce_value(_LIST_SPEC, [3, 1, 3, 2]) == (True, (3, 1, 2))


def test_channel_list_coerce_rejects_garbage():
    assert coerce_value(_LIST_SPEC, ["a"]) == (False, None)
    assert coerce_value(_LIST_SPEC, [-1]) == (False, None)
    assert coerce_value(_LIST_SPEC, [[2]]) == (False, None)


def test_channel_list_format_mentions_and_none():
    fv = format_value(_LIST_SPEC, (11, 22))
    assert fv.display == "<#11> <#22>"
    assert fv.translate is False
    fv_empty = format_value(_LIST_SPEC, ())
    assert fv_empty.display == "settings.none"
    assert fv_empty.translate is True


async def test_channel_list_roundtrip_through_storage(tmp_path):
    storage = GuildStorage(tmp_path)
    spec = SETTINGS["anti_invite.exempt_places"]
    await set_setting(storage, 1, spec.key, [5, 6, 5])
    assert await get_setting(storage, 1, spec.key) == (5, 6)


async def test_legacy_single_channel_migrates_into_the_list(tmp_path):
    storage = GuildStorage(tmp_path)
    # Old-world data: the renamed scalar key still holds the value.
    await storage.set(1, "anti_invite.exempt_channel", 42)
    assert await get_setting(storage, 1, "anti_invite.exempt_places") == (42,)
    # Once the new key is written, the legacy value is ignored.
    await set_setting(storage, 1, "anti_invite.exempt_places", [7])
    assert await get_setting(storage, 1, "anti_invite.exempt_places") == (7,)


def test_exempt_places_spec_keeps_legacy_key():
    spec = SETTINGS["anti_invite.exempt_places"]
    assert spec.value_type is SettingType.CHANNEL_LIST
    assert spec.legacy_single_key == "anti_invite.exempt_channel"


#: management screens ----------------------------------------------------------


class FakeTranslator:
    async def t(self, guild_id, key, **variables):
        suffix = "".join(f";{k}={v}" for k, v in sorted(variables.items()))
        return f"{key}{suffix}"

    async def raw(self, guild_id, key):
        return key


def make_menu(tmp_path):
    bot = MagicMock()
    bot.theme = load_theme()
    bot.translator = FakeTranslator()
    bot.storage = GuildStorage(tmp_path)
    from rosemary.core.themes import ThemeStore

    bot._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
    menu = SettingsMenuView(bot, 1)
    return bot, menu


def _prepared(view: SettingsMenuView):
    return view.walk_children()


def row_buttons(view: SettingsMenuView) -> dict[str, discord.ui.Button]:
    return {
        item.custom_id: item
        for item in _prepared(view)
        if isinstance(item, discord.ui.Button)
    }


def select_of(view: SettingsMenuView) -> discord.ui.Select | None:
    for item in _prepared(view):
        if isinstance(item, discord.ui.Select):
            return item
    return None


from rosemary.ui.theme import load_theme  # noqa: E402


async def test_list_page_renders_row_per_place_with_remove(tmp_path):
    bot, menu = make_menu(tmp_path)
    menu.editing_key = "anti_invite.exempt_places"
    await set_setting(bot.storage, 1, "anti_invite.exempt_places", [11, 22])
    await menu.prepare()
    buttons = row_buttons(menu)
    assert "settings_list_remove:anti_invite.exempt_places:11" in buttons
    assert "settings_list_remove:anti_invite.exempt_places:22" in buttons
    assert "settings_list_add:anti_invite.exempt_places" in buttons


async def test_list_page_empty_shows_only_add(tmp_path):
    bot, menu = make_menu(tmp_path)
    menu.editing_key = "anti_invite.exempt_places"
    await menu.prepare()
    buttons = row_buttons(menu)
    # Nothing to clear when the list is empty.
    assert "settings_list_clear:anti_invite.exempt_places" not in buttons
    assert "settings_list_add:anti_invite.exempt_places" in buttons


async def test_add_mode_shows_multi_select_and_confirm_disabled(tmp_path):
    bot, menu = make_menu(tmp_path)
    menu.editing_key = "anti_invite.exempt_places"
    menu.add_mode = True
    await menu.prepare()
    select = select_of(menu)
    assert select is not None
    assert select.max_values == 25
    assert discord.ChannelType.category in (select.channel_types or [])
    assert discord.ChannelType.text in (select.channel_types or [])
    buttons = row_buttons(menu)
    confirm = buttons["settings_list_confirm:anti_invite.exempt_places"]
    assert confirm.disabled is True  # nothing pending yet


async def test_pick_merges_then_confirm_saves_list(tmp_path):
    bot, menu = make_menu(tmp_path)
    menu.editing_key = "anti_invite.exempt_places"
    menu.add_mode = True

    interaction = MagicMock()
    interaction.custom_id = "settings_list_pick:anti_invite.exempt_places"
    interaction.data = {"values": ["31", "32"]}
    interaction.response.is_done.return_value = False
    interaction.response.defer = AsyncMock()
    interaction.edit = AsyncMock()
    await menu._list_pick(interaction)
    assert menu._pending_adds == (31, 32)

    # Another pick overlapping must not duplicate.
    interaction.data = {"values": ["32", "33"]}
    await menu._list_pick(interaction)
    assert menu._pending_adds == (31, 32, 33)

    confirm_interaction = MagicMock()
    confirm_interaction.custom_id = "settings_list_confirm:anti_invite.exempt_places"
    confirm_interaction.response.is_done.return_value = False
    confirm_interaction.response.defer = AsyncMock()
    confirm_interaction.edit = AsyncMock()
    await menu._list_confirm(confirm_interaction)
    assert await get_setting(bot.storage, 1, "anti_invite.exempt_places") == (31, 32, 33)
    assert menu.add_mode is False


async def test_remove_deletes_only_that_place_and_repages(tmp_path):
    bot, menu = make_menu(tmp_path)
    menu.editing_key = "anti_invite.exempt_places"
    places = list(range(100, 100 + LIST_ITEMS_PER_PAGE + 2))
    await set_setting(bot.storage, 1, "anti_invite.exempt_places", places)
    menu.list_page = 1  # second page has the last two rows

    interaction = MagicMock()
    interaction.custom_id = f"settings_list_remove:anti_invite.exempt_places:{places[-1]}"
    interaction.response.is_done.return_value = False
    interaction.response.defer = AsyncMock()
    interaction.edit = AsyncMock()
    await menu._list_remove(interaction)
    expected = tuple(p for p in places if p != places[-1])
    assert await get_setting(bot.storage, 1, "anti_invite.exempt_places") == expected
    assert menu.list_page == 1  # one row left on page 2; page still valid

    # Removing that last row must clamp the page instead of stranding it.
    interaction.custom_id = f"settings_list_remove:anti_invite.exempt_places:{places[-2]}"
    await menu._list_remove(interaction)
    assert await get_setting(bot.storage, 1, "anti_invite.exempt_places") == tuple(
        p for p in places if p not in (places[-1], places[-2])
    )
    assert menu.list_page == 0


async def test_clear_all_empties_the_list(tmp_path):
    bot, menu = make_menu(tmp_path)
    menu.editing_key = "anti_invite.exempt_places"
    await set_setting(bot.storage, 1, "anti_invite.exempt_places", [9, 8])
    interaction = MagicMock()
    interaction.custom_id = "settings_list_clear:anti_invite.exempt_places"
    interaction.response.is_done.return_value = False
    interaction.response.defer = AsyncMock()
    interaction.edit = AsyncMock()
    await menu._list_clear(interaction)
    assert await get_setting(bot.storage, 1, "anti_invite.exempt_places") == ()
    assert menu.editing_key == "anti_invite.exempt_places"  # stayed on the screen


#: anti-invite honors channels and categories ----------------------------------


def _anti_cog(tmp_path):
    from rosemary.cogs.anti_invite import AntiInviteCog
    from rosemary.ui.theme import load_theme

    bot = MagicMock()
    bot.theme = load_theme()
    bot.translator = FakeTranslator()
    bot.storage = GuildStorage(tmp_path)
    log_channel = AsyncMock(spec=discord.TextChannel)
    bot.get_channel.side_effect = lambda cid: log_channel if cid == 55 else None
    return AntiInviteCog(bot), bot


async def test_anti_invite_skips_exempt_category_channels(tmp_path, monkeypatch):
    cog, bot = _anti_cog(tmp_path)

    async def patched(storage, guild_id, key):
        data = {
            "anti_invite.enabled": True,
            "anti_invite.warn_on_delete": False,
            "anti_invite.exempt_places": (10,),  # category 10
        }
        if key in data:
            return data[key]
        return SETTINGS[key].default

    monkeypatch.setattr("rosemary.cogs.anti_invite.get_setting", patched)
    assert await cog._is_exempt(1, _chan(channel_id=3, category_id=10)) is True
    assert await cog._is_exempt(1, _chan(channel_id=10, category_id=None)) is True
    assert await cog._is_exempt(1, _chan(channel_id=4, category_id=11)) is False


def _chan(channel_id: int, category_id: int | None):
    chan = MagicMock()
    chan.id = channel_id
    chan.category_id = category_id
    return chan


async def test_anti_invite_listener_deletes_outside_exempt(tmp_path, monkeypatch):
    cog, bot = _anti_cog(tmp_path)

    async def patched(storage, guild_id, key):
        data = {
            "anti_invite.enabled": True,
            "anti_invite.warn_on_delete": False,
            "anti_invite.exempt_places": (10,),
        }
        if key in data:
            return data[key]
        return SETTINGS[key].default

    monkeypatch.setattr("rosemary.cogs.anti_invite.get_setting", patched)
    message = MagicMock()
    message.guild = MagicMock()
    message.guild.id = 1
    message.author.bot = False
    message.author.guild_permissions = discord.Permissions.none()
    message.content = "join discord.gg/abc123"
    message.channel = _chan(channel_id=4, category_id=11)
    message.delete = AsyncMock()

    # Own-code lookup: not our guild -> not exempt.
    bot.fetch_invite = AsyncMock(return_value=MagicMock(guild=None))
    await cog.on_message(message)
    message.delete.assert_awaited_once()


async def test_anti_invite_listener_spares_exempt_category(tmp_path, monkeypatch):
    cog, bot = _anti_cog(tmp_path)

    async def patched(storage, guild_id, key):
        data = {
            "anti_invite.enabled": True,
            "anti_invite.warn_on_delete": False,
            "anti_invite.exempt_places": (10,),
        }
        if key in data:
            return data[key]
        return SETTINGS[key].default

    monkeypatch.setattr("rosemary.cogs.anti_invite.get_setting", patched)
    message = MagicMock()
    message.guild = MagicMock()
    message.guild.id = 1
    message.author.bot = False
    message.author.guild_permissions = discord.Permissions.none()
    message.content = "join discord.gg/abc123"
    message.channel = _chan(channel_id=4, category_id=10)
    message.delete = AsyncMock()

    await cog.on_message(message)
    message.delete.assert_not_awaited()


#: catalogs ---------------------------------------------------------------------


def test_every_static_settings_menu_key_exists_in_both_catalogs():
    """Sweep: a missing catalog key renders the raw key on a button (real bug).

    Parses every static ``settings.*`` literal from ui/settings_menu.py and
    demands it in both flattened catalogs. New code with a new literal fails
    here until the catalogs carry it: buttons never show raw keys again.
    """
    import re

    from rosemary.core.i18n import _flatten

    with open("ui/settings_menu.py", encoding="utf-8") as fh:
        src = fh.read()
    keys = sorted(set(re.findall(r'"(settings\.[a-z0-9_.]+)"', src)))
    assert keys, "sweep found no keys; check the regex"
    for lang in ("en-US", "pt-BR"):
        with open(f"language/{lang}.yaml", encoding="utf-8") as fh:
            flat = _flatten(yaml.safe_load(fh))
        missing = [key for key in keys if key not in flat]
        assert missing == [], f"{lang}: keys missing from catalog: {missing}"


def test_list_ui_keys_in_both_catalogs():
    from rosemary.core.i18n import _flatten

    for lang in ("en-US", "pt-BR"):
        with open(f"language/{lang}.yaml", encoding="utf-8") as fh:
            flat = _flatten(yaml.safe_load(fh))
        for key in (
            "settings.list.empty",
            "settings.list.remove",
            "settings.list.add",
            "settings.list.clear",
            "settings.list.add_hint",
            "settings.list.confirm",
            "settings.list.pending",
            "settings.list.pick_placeholder",
            "settings.cancel",
        ):
            assert isinstance(flat.get(key), str), f"{lang}:{key}"
        for key in (
            "settings.anti_invite.exempt_places.label",
            "settings.anti_invite.exempt_places.description",
        ):
            assert isinstance(flat.get(key), str), f"{lang}:{key}"
