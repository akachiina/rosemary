"""Build the bump reminder/thank-you cards with fakes to catch V2 regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from card_forms import (
    payload_accent,
    payload_footer_lines,
    payload_texts,
    payload_thumbnail,
)

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
    reminder = await cog._build_reminder_payload(guild, cooldown_display)
    thanks = await cog._build_thank_you_payload(
        guild, 456, cooldown_display, next_bump_timestamp
    )
    return reminder, thanks


async def test_bump_cards_build_without_avatar(bot):
    reminder, thanks = await _build_views(bot, GuildWithMember(False))
    # The bump cards are content: they ship as Components V2 cards.
    assert reminder.view is not None
    assert thanks.view is not None
    assert reminder.embed is None
    assert thanks.embed is None


async def test_bump_cards_build_with_avatar(bot):
    reminder, thanks = await _build_views(bot, GuildWithMember(True))
    assert reminder.view is not None
    assert thanks.view is not None
    # The code fallback keeps the subject's picture as the thumbnail.
    assert payload_thumbnail(thanks) == "https://example.com/avatar.png"


async def test_bump_card_layout_valid(bot):
    """The card carries the whole card: a title, a body and nothing else."""
    bot = BotWithGuild(bot.storage, has_avatar=True)
    guild = bot.get_guild(1)
    reminder, thanks = await _build_views(bot, guild)
    for payload in (reminder, thanks):
        texts = payload_texts(payload)
        # The title is a bold line (the size of the title of an embed), not
        # a heading: the card does not shout over its own body.
        assert any(text.startswith("**") for text in texts), texts
        assert len(texts) > 1, texts
        assert payload.embed is None


async def test_bump_cards_use_reminder_style_color(bot):
    reminder, thanks = await _build_views(bot, GuildWithMember(True))
    expected = bot.theme.color(bot.theme.style("bump_reminder").color)
    for payload in (reminder, thanks):
        assert payload_accent(payload) == expected.value


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


#: the card documents (the production render path) ----------------------------


def test_bump_cards_are_documents():
    """The rich bump cards live in the default package: the document is the
    source of truth (no builder, no seed)."""
    from rosemary.core.cards import get_card
    from rosemary.core.theme_package import default_package

    package = default_package()
    for key in ("bump.thank_you", "bump.leaderboard"):
        assert key in package.documents
        assert get_card(key) is not None


def _card_bot(tmp_path):
    """A bot with the real catalogs (pt-BR) so the card copy resolves for real."""
    from rosemary.core.i18n import Translator
    from rosemary.core.storage import GuildStorage

    async def resolver(gid):
        return "pt-BR"

    theme = load_theme()

    class _Bot:
        def __init__(self) -> None:
            self.theme = theme
            self._theme_cache: dict = {}
            self.storage = GuildStorage(tmp_path)
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
    "user_name": "Ana",
    "timestamp": "<t:1:R>",
}

_LEADERBOARD_VARS = {
    "winner_mention": "<@111>",
    "winner_name": "Bea",
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


@pytest.mark.parametrize(
    ("key", "style", "avatar_var", "name_var", "variables"),
    [
        (
            "bump.thank_you",
            "bump_reminder",
            "user_avatar",
            "user_name",
            _THANK_YOU_VARS,
        ),
        (
            "bump.leaderboard",
            "bump_leaderboard",
            "winner_avatar",
            "winner_name",
            _LEADERBOARD_VARS,
        ),
    ],
)
async def test_bump_card_document_layout_and_style(
    key, style, avatar_var, name_var, variables, tmp_path
):
    """The cards follow the theme styles and keep their frame.

    The subject's avatar is the thumbnail and the footer keeps the date only:
    the card never repeats the name above the title, and the leaderboard no
    longer credits the winner as if they had posted the ranking.
    """
    from rosemary.core.card_service import render_card_message
    from rosemary.core.themes import theme_for

    bot = _card_bot(tmp_path)
    state_color = theme_for(bot, 1).style(style).color
    payload, _allowed = await render_card_message(
        bot, 1, key, {**variables, "state_color": state_color}
    )
    assert payload is not None
    texts = payload_texts(payload)
    # The color is the token the theme style names.
    assert payload_accent(payload) == theme_for(bot, 1).color(state_color).value
    # The title is a bold line (the size of the title of an embed).
    assert any(text.startswith("**") for text in texts), texts
    assert len(texts) > 1, texts
    # The subject's avatar is the thumbnail; the name is not a line.
    assert not any(text == f"**{variables[name_var]}**" for text in texts), texts
    assert payload_thumbnail(payload) == variables[avatar_var]
    footer = "\n".join(payload_footer_lines(payload))
    assert "<@" not in footer and "<t:" not in footer


@pytest.mark.parametrize(
    ("key", "avatar_var", "variables"),
    [
        ("bump.thank_you", "user_avatar", _THANK_YOU_VARS),
        ("bump.leaderboard", "winner_avatar", _LEADERBOARD_VARS),
    ],
)
async def test_bump_card_without_avatar_has_no_thumbnail(
    key, avatar_var, variables, tmp_path
):
    """A missing avatar leaves the card without a picture, never an empty one."""
    from rosemary.core.card_service import render_card_message

    bot = _card_bot(tmp_path)
    bare = {**variables, avatar_var: ""}
    payload, _allowed = await render_card_message(
        bot, 1, key, {**bare, "state_color": "brand"}
    )
    assert payload is not None
    assert payload_thumbnail(payload) == ""


async def test_bump_card_colors_follow_theme_styles(tmp_path):
    """Restyling ``styles.bump_*`` in a theme file changes the cards: the
    send site resolves the style to a token and the document uses it."""
    import yaml

    from rosemary.core.card_service import render_card_message
    from rosemary.core.themes import theme_for
    from rosemary.ui.theme import load_theme as _load

    default_yaml = (
        Path(__file__).resolve().parents[1] / "themes" / "default" / "tokens.yaml"
    ).read_text(encoding="utf-8")
    data = yaml.safe_load(default_yaml)
    data["styles"]["bump_reminder"]["color"] = "success"
    data["styles"]["bump_leaderboard"]["color"] = "danger"
    theme_file = tmp_path / "custom.yaml"
    theme_file.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    bot = _card_bot(tmp_path)
    bot._theme_cache[1] = _load(theme_file)

    thanks_color = theme_for(bot, 1).style("bump_reminder").color
    board_color = theme_for(bot, 1).style("bump_leaderboard").color
    thanks, _ = await render_card_message(
        bot, 1, "bump.thank_you", {**_THANK_YOU_VARS, "state_color": thanks_color}
    )
    board, _ = await render_card_message(
        bot, 1, "bump.leaderboard", {**_LEADERBOARD_VARS, "state_color": board_color}
    )
    assert payload_accent(thanks) == theme_for(bot, 1).color("success").value
    assert payload_accent(board) == theme_for(bot, 1).color("danger").value
