"""Build the bump reminder/thank-you cards with fakes to catch V2 regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import discord
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


#: default builders (the production render path) ------------------------------


def test_bump_builders_registered():
    """The rich bump cards resolve through builders, never through seeds."""
    from rosemary.core.bump_cards import (
        default_bump_leaderboard_document,
        default_bump_thank_you_document,
    )
    from rosemary.core.cards import get_default_builder

    assert get_default_builder("bump.thank_you") is default_bump_thank_you_document
    assert get_default_builder("bump.leaderboard") is default_bump_leaderboard_document


def test_bump_builder_cards_have_no_seed():
    """A seed entry would shadow the registered builder and strip the avatar
    (AGENTS.md rule; bump.thank_you once carried a seed that contradicted the
    map's own comment)."""
    from rosemary.core.card_service import SEED_PARTS_BY_KEY

    for key in ("bump.thank_you", "bump.leaderboard"):
        assert key not in SEED_PARTS_BY_KEY, f"seed would shadow the builder: {key}"


def _builder_bot():
    """A bot with the real catalogs (pt-BR) so builder copy resolves for real."""
    from pathlib import Path

    from rosemary.core.i18n import Translator

    async def resolver(gid):
        return "pt-BR"

    theme = load_theme()

    class _Bot:
        def __init__(self) -> None:
            self.theme = theme
            self._theme_cache: dict = {}
            self.translator = Translator(
                Path(__file__).resolve().parents[1] / "language",
                resolver=resolver,
                default_placeholders=theme.emojis,
            )

    return _Bot()


_THANK_YOU_VARS = {
    "mention": "<@456>",
    "cooldown": "2h",
    "next_bump_timestamp": 9999999999,
    "user_avatar": "https://example.com/avatar.png",
    "user": "<@456>",
    "timestamp": "<t:1:R>",
}

_LEADERBOARD_VARS = {
    "winner_mention": "<@111>",
    "winner_count": 5,
    "role_name": "MVP",
    "leaderboard_list": "{medal} **1º** · <@111> **5** bumps",
    "start_timestamp": 1000,
    "end_timestamp": 2000,
    "next_reset_timestamp": 3000,
    "winner_avatar": "https://example.com/winner.png",
    "user": "<@111>",
    "timestamp": "<t:1:R>",
}


def _doc_texts(view) -> list[str]:
    return [
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]


@pytest.mark.parametrize(
    ("builder", "key", "style", "avatar_var", "variables", "has_author_footer"),
    [
        (
            "default_bump_thank_you_document",
            "bump.thank_you",
            "bump_reminder",
            "user_avatar",
            _THANK_YOU_VARS,
            True,
        ),
        (
            "default_bump_leaderboard_document",
            "bump.leaderboard",
            "bump_leaderboard",
            "winner_avatar",
            _LEADERBOARD_VARS,
            False,
        ),
    ],
)
async def test_bump_builder_document_layout_and_style(
    builder, key, style, avatar_var, variables, has_author_footer
):
    """Builders follow the theme styles and produce valid V2 documents.

    The thank-you card keeps the author footer (``{user}`` is the bumper);
    the leaderboard must not carry one: it is a bot-posted ranking, and the
    footer used to credit whoever happened to be in first place.
    """
    import rosemary.core.bump_cards as bump_cards
    from rosemary.core.card_service import render_document

    bot = _builder_bot()
    doc = await getattr(bump_cards, builder)(bot, 1, **variables)
    container = doc["blocks"][0]
    assert container["type"] == "container"
    # The container color is the theme style token, not a hardcoded name.
    assert container["color"] == bot.theme.style(style).color

    section = container["children"][0]
    assert section["type"] == "section"
    assert section["accessory"]["url"] == variables[avatar_var]
    bodies = [c["body"] for c in container["children"] if c["type"] == "text"]
    # The leaderboard description itself ends with a "-# ..." hint line, so
    # the check targets the author attribution, not just any small line.
    assert any("-# por" in body for body in bodies) is has_author_footer

    view = await render_document(bot, doc, dict(variables), guild_id=1, card_key=key)
    _assert_valid_layout(view, key)
    texts = _doc_texts(view)
    assert any("Bump" in text for text in texts)  # resolved catalog copy
    assert any("-# por <@" in text for text in texts) is has_author_footer


@pytest.mark.parametrize(
    ("builder", "avatar_var", "variables"),
    [
        ("default_bump_thank_you_document", "user_avatar", _THANK_YOU_VARS),
        ("default_bump_leaderboard_document", "winner_avatar", _LEADERBOARD_VARS),
    ],
)
async def test_bump_builder_without_avatar_omits_accessory(builder, avatar_var, variables):
    """A missing avatar must degrade to plain text, never an empty accessory."""
    import rosemary.core.bump_cards as bump_cards

    bot = _builder_bot()
    bare = dict(variables)
    bare[avatar_var] = ""
    doc = await getattr(bump_cards, builder)(bot, 1, **bare)
    section = doc["blocks"][0]["children"][0]
    assert "accessory" not in section


async def test_bump_builder_colors_follow_theme_styles(tmp_path):
    """Restyling ``styles.bump_*`` in a theme file changes the built cards:
    the token in the document tracks the guild's active theme."""
    import yaml

    import rosemary.core.bump_cards as bump_cards
    from rosemary.ui.theme import load_theme as _load

    default_yaml = (
        Path(__file__).resolve().parents[1] / "themes" / "default.yaml"
    ).read_text(encoding="utf-8")
    data = yaml.safe_load(default_yaml)
    data["styles"]["bump_reminder"]["color"] = "success"
    data["styles"]["bump_leaderboard"]["color"] = "danger"
    theme_file = tmp_path / "custom.yaml"
    theme_file.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    bot = _builder_bot()
    bot._theme_cache[1] = _load(theme_file)

    thanks = await bump_cards.default_bump_thank_you_document(bot, 1, **_THANK_YOU_VARS)
    board = await bump_cards.default_bump_leaderboard_document(
        bot, 1, **_LEADERBOARD_VARS
    )
    assert thanks["blocks"][0]["color"] == "success"
    assert board["blocks"][0]["color"] == "danger"
