"""Build the bump reminder/thank-you cards with fakes to catch V2 regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from rosemary.cogs.bump_leaderboard import _next_reset
from rosemary.core.storage import GuildStorage
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


class FakeMember:
    id = 456
    name = "member"
    display_name = "member"

    def __init__(self, has_avatar: bool = True) -> None:
        self.display_avatar = (
            type("Avatar", (), {"url": "https://example.com/avatar.png"}) if has_avatar else None
        )


class GuildWithMember:
    id = 1
    name = "test-guild"

    def __init__(self, has_avatar: bool = True) -> None:
        self._member = FakeMember(has_avatar)

    def get_member(self, member_id):
        return self._member


class BotWithGuild(FakeBot):
    def __init__(self, storage, has_avatar: bool = True) -> None:
        super().__init__(storage)
        self._guild = GuildWithMember(has_avatar)

    def get_guild(self, guild_id):
        return self._guild


@pytest.fixture
def bot(tmp_path):
    return FakeBot(GuildStorage(tmp_path))


def _validate_components(d, path, errors):
    """Walk a component dict tree asserting Discord's V2 placement rules."""
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


async def _build_views(bot, guild, cooldown_display="2h", next_bump_timestamp=9999999999):
    from rosemary.cogs.bump_reminder import BumpReminderCog

    cog = BumpReminderCog(bot)
    reminder = await cog._build_reminder_view(guild, cooldown_display)
    thanks = await cog._build_thank_you_view(
        guild, 456, cooldown_display, next_bump_timestamp
    )
    return reminder, thanks


async def test_bump_cards_build_without_avatar(bot):
    reminder, thanks = await _build_views(bot, GuildWithMember(False))
    assert reminder.children
    assert thanks.children


async def test_bump_cards_build_with_avatar(bot):
    reminder, thanks = await _build_views(bot, GuildWithMember(True))
    assert reminder.children
    assert thanks.children


@pytest.mark.parametrize("has_avatar", [True, False])
async def test_bump_card_layout_valid(bot, has_avatar):
    bot = BotWithGuild(bot.storage, has_avatar=has_avatar)
    guild = bot.get_guild(1)
    reminder, thanks = await _build_views(bot, guild)
    _assert_valid_layout(reminder, "bump_reminder")
    _assert_valid_layout(thanks, "bump_thank_you")


async def test_bump_cards_use_reminder_style_color(bot):
    reminder, thanks = await _build_views(bot, GuildWithMember(True))
    expected = bot.theme.color(bot.theme.style("bump_reminder").color)
    for view in (reminder, thanks):
        container = view.children[0]
        assert container.color == expected


def test_next_reset():
    """The next weekly reset matches the reset_day/reset_hour settings."""
    wed = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)  # a Wednesday
    sunday_2000 = datetime(2026, 8, 23, 20, 0, tzinfo=UTC)

    # Mid-week → next Sunday at the configured hour
    assert _next_reset(wed, "Sunday", 20) == sunday_2000
    # Same day before the reset hour → same day at the reset hour
    assert _next_reset(datetime(2026, 8, 23, 12, 0, tzinfo=UTC), "Sunday", 20) == sunday_2000
    # Same day after the reset hour → one week later
    assert (
        _next_reset(datetime(2026, 8, 23, 21, 0, tzinfo=UTC), "Sunday", 20)
        == datetime(2026, 8, 30, 20, 0, tzinfo=UTC)
    )
    # Unknown reset_day → now + 7 days
    assert _next_reset(wed, "Funday", 20) == wed + timedelta(days=7)
    # Out-of-range reset_hour → default hour
    assert _next_reset(wed, "Sunday", 99) == sunday_2000


def test_next_reset_honors_guild_timezone():
    """reset_day/reset_hour are interpreted in the guild's timezone."""
    tz = timezone(timedelta(hours=-3))
    wed = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)  # 07:00 in UTC-3
    # Sunday 20:00 UTC-3 == Sunday 23:00 UTC
    assert _next_reset(wed, "Sunday", 20, tz) == datetime(2026, 8, 23, 23, 0, tzinfo=UTC)
    # Sunday 20:00 UTC-3 on the same local day, before the reset → same day
    sunday_13_utc = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)  # 10:00 local
    assert _next_reset(sunday_13_utc, "Sunday", 20, tz) == datetime(2026, 8, 23, 23, 0, tzinfo=UTC)
    # Sunday 22:00 UTC-3 (Monday 01:00 UTC) already passed locally → next week
    monday_01_utc = datetime(2026, 8, 24, 1, 0, tzinfo=UTC)  # Sunday 22:00 local
    assert _next_reset(monday_01_utc, "Sunday", 20, tz) == datetime(2026, 8, 30, 23, 0, tzinfo=UTC)
