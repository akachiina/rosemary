"""The drawn strip: the author line of a card as a picture.

A card that names an author (the member the card is about) ships the author
line as a **drawn strip**: a picture the bot rasterizes (the avatar and the
name, painted by the template of the theme) that rides the message as an
attachment and takes the place of the native author section.

The technique is generic and it never breaks a send:

* the trigger is the card's own vocabulary (the ``author`` slot), so the
  YAML of the cards says nothing new;
* without the renderer (or without a picture) the card keeps its native
  author line, so a host that cannot draw still ships the card;
* a theme turns the strip off by removing the author
  (``cards.<key>.author: null``).

These tests pin the three behaviours and the cache that keeps the bot from
drawing the same strip twice.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from card_forms import payload_texts
from golden_cards import make_bot
from offline import FIXED_PNG, offline_pictures
from PIL import Image

from rosemary.core.card_service import render_card_message
from rosemary.core.i18n import Translator
from rosemary.core.render import elements
from rosemary.core.render.elements import element_filename, render_element
from rosemary.core.render.identity import Identity, identity_key
from rosemary.core.storage import GuildStorage
from rosemary.core.themes import ThemeStore, preload_themes
from rosemary.ui.theme import load_theme

LANG_DIR = Path(__file__).resolve().parents[1] / "language"

#: The variables of the welcome event (the card with an author).
WELCOME = {
    "user": "<@42>",
    "user_name": "Ana",
    "user_id": "42",
    "server": "Serv",
    "count": "3",
    "user_avatar": "https://cdn.discordapp.com/embed/avatars/0.png",
    "inviter": "",
    "timestamp": "10/11/2026 17:08",
}

#: The picture the fixtures draw (the avatar of the sample variables).
FIXED_DATA_URI = "data:image/png;base64," + base64.b64encode(FIXED_PNG).decode("ascii")


@pytest.fixture(autouse=True)
def _clean_caches():
    """The caches of the renderer are global: every test starts clean."""
    from rosemary.core.render import identity

    elements._cache.clear()
    identity._cache.clear()
    yield
    elements._cache.clear()
    identity._cache.clear()


def _galleries(payload) -> list[str]:
    """The pictures of the galleries of a rendered card."""
    found: list[str] = []

    def walk(node) -> None:
        if type(node).__name__ == "MediaGallery":
            for item in getattr(node, "items", []) or []:
                url = getattr(item, "url", "")
                if isinstance(url, str) and url:
                    found.append(url)
        for child in list(getattr(node, "children", []) or []) + list(
            getattr(node, "items", []) or []
        ):
            walk(child)

    view = getattr(payload, "view", None)
    for item in getattr(view, "children", []) or []:
        walk(item)
    return found


class _ThemedBot:
    """A bot whose active theme customizes the cards."""

    def __init__(self, tmp_path: Path, theme_yaml: str) -> None:
        self.storage = GuildStorage(tmp_path)
        self.theme = load_theme()
        self.translator = Translator(
            LANG_DIR,
            resolver=lambda guild_id: "en-US",
            default_placeholders=self.theme.emojis,
        )
        self.guilds = [type("G", (), {"id": 1})()]
        self._theme_store = ThemeStore(tmp_path, themes_dir=tmp_path / "themes")
        (tmp_path / "themes").mkdir(exist_ok=True)
        (tmp_path / "themes" / "t.yaml").write_text(theme_yaml, encoding="utf-8")


async def _activate(bot: _ThemedBot) -> None:
    await bot._theme_store.set_active(1, "t")
    await preload_themes(bot)


async def test_the_author_line_ships_as_a_drawn_strip():
    """A card that names an author draws the strip and rides the picture."""
    bot = make_bot()
    with offline_pictures():
        payload, _allowed = await render_card_message(bot, 1, "events.welcome", WELCOME)
    assert payload is not None
    galleries = _galleries(payload)
    assert len(galleries) == 1, galleries
    assert galleries[0].startswith("attachment://author_")
    # The picture rides the message, and the gallery points at it.
    assert len(payload.files) == 1
    assert payload.files[0].filename == galleries[0].removeprefix("attachment://")
    # The native author line is gone: the strip carries the name now.
    assert "**Ana**" not in payload_texts(payload)


async def test_without_the_renderer_the_native_author_line_stays(monkeypatch):
    """A host that cannot draw keeps the card (and its native author line)."""
    monkeypatch.setattr(elements, "render_element", lambda *args, **kwargs: None)
    bot = make_bot()
    with offline_pictures():
        payload, _allowed = await render_card_message(bot, 1, "events.welcome", WELCOME)
    assert payload is not None
    assert _galleries(payload) == []
    assert payload.files == []
    assert "**Ana**" in payload_texts(payload)


async def test_a_theme_can_turn_the_strip_off(tmp_path):
    """The theme owns the strip: removing the author removes the picture."""
    bot = _ThemedBot(
        tmp_path,
        "name: t\ncards:\n  events.welcome:\n    author: null\n",
    )
    await _activate(bot)
    with offline_pictures():
        payload, _allowed = await render_card_message(bot, 1, "events.welcome", WELCOME)
    assert payload is not None
    assert _galleries(payload) == []
    assert payload.files == []
    assert "**Ana**" not in payload_texts(payload)


async def test_the_classic_forms_never_draw_the_strip():
    """The strip belongs to the Components V2: the logs and the DMs stay."""
    bot = make_bot()
    with offline_pictures():
        payload, _allowed = await render_card_message(
            bot, 1, "partnerships.audit", {"title": "Audit", "body": "B"}
        )
    assert payload is not None
    assert payload.embed is not None
    assert payload.files == []
    assert _galleries(payload) == []


async def test_the_picture_is_drawn_once(monkeypatch):
    """The cache keeps the bot from drawing the same strip twice."""
    calls: list[str] = []

    def counting_rasterize(template: str, values: dict[str, str]) -> bytes | None:
        calls.append(template)
        return FIXED_PNG

    monkeypatch.setattr(elements, "_rasterize", counting_rasterize)
    bot = make_bot()
    with offline_pictures():
        first, _ = await render_card_message(bot, 1, "events.welcome", WELCOME)
        second, _ = await render_card_message(bot, 1, "events.welcome", WELCOME)
    assert first is not None and second is not None
    assert len(calls) == 1, calls
    assert first.files[0].filename == second.files[0].filename


def test_the_renderer_draws_a_png():
    """The theme's template draws a real picture (the pipeline of the panel)."""
    pytest.importorskip("weasyprint")
    template = load_theme().elements["author"]["html"]
    png = render_element(
        "author",
        template,
        {
            "name": "Ana",
            "initial": "A",
            "avatar": FIXED_DATA_URI,
            "accent": "#57f287",
        },
    )
    assert png is not None
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_the_strip_is_transparent_and_small():
    """The strip draws the avatar and the name on a transparent picture.

    A box behind the author line would fight the card (the frame of the
    container) and a big picture would shout over the card itself, so the
    picture is transparent and small: an author line, nothing more.
    """
    pytest.importorskip("weasyprint")
    template = load_theme().elements["author"]["html"]
    png = render_element(
        "author",
        template,
        {
            "name": "Ana",
            "initial": "A",
            "avatar": FIXED_DATA_URI,
            "accent": "#57f287",
        },
    )
    assert png is not None
    with Image.open(io.BytesIO(png)) as img:
        width, height = img.size
        corner = img.convert("RGBA").getpixel((1, 1))
    # The background is transparent: the picture takes the color of the
    # message behind it.
    assert corner[3] == 0, corner
    # The picture is small: the author line, not a poster.
    assert width <= elements.MAX_WIDTH, width
    assert height <= elements.MAX_HEIGHT, height


def test_a_broken_template_draws_nothing():
    """A template that fails never breaks the card: it draws nothing."""
    assert render_element("author", "<style> @page { size: } </style>", {}) is None


def test_the_file_name_follows_the_identity():
    """The file is named after what does not change (the member, the accent)."""
    one = identity_key(Identity(name="Ana", avatar_url="https://a/b.png"), "#57f287")
    same = identity_key(Identity(name="Ana", avatar_url="https://a/b.png"), "#57f287")
    other = identity_key(Identity(name="Bia", avatar_url="https://a/b.png"), "#57f287")
    assert element_filename("author", one) == element_filename("author", same)
    assert element_filename("author", one) != element_filename("author", other)
