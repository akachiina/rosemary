"""Chat snapshot renderer (core/chat_image.py): templates, escaping, PNG.

The snapshot is optional visual sugar for the audit logs: every failure mode
(missing renderer, broken template, no avatar, no content) must degrade to
``None`` / empty strings and never raise. The PNG assertions only check
signature bytes and dimensions: rendering is WeasyPrint's job.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from rosemary.core.chat_image import (
    SnapshotMessage,
    _avatar_fallback,
    _capped,
    _css_color,
    _escape,
    build_snapshot_message,
    render_snapshot,
)


def _message(content="Durma bem amego", image_url=""):
    msg = MagicMock()
    msg.id = 987654321000000000
    msg.content = content
    msg.guild = None  # MagicMock auto-attr would leak into the role-color path
    msg.attachments = []
    msg.stickers = []
    msg.author.display_name = "Limao"
    msg.author.name = "limao"
    msg.author.bot = False
    msg.author.display_avatar.url = ""
    msg.created_at.timestamp.return_value = 1700000000.0
    return msg


#: escaping / caps -------------------------------------------------------------


def test_escape_newlines_and_html():
    assert _escape("<b>oi</b>\ntchau") == "&lt;b&gt;oi&lt;/b&gt;<br>tchau"
    assert _escape("") == ""
    assert _escape(None) == ""  # type: ignore[arg-type]


def test_capped_adds_ellipsis():
    assert _capped("curto") == "curto"
    long = "a" * 700
    capped = _capped(long)
    assert len(capped) == 601  # 600 chars + ellipsis
    assert capped.endswith("…")


def test_css_color_normalizes():
    assert _css_color("FF0000") == "#ff0000"
    assert _css_color("#00ff00") == "#00ff00"
    assert _css_color("vermelho") == "#99aab5"
    assert _css_color("") == "#99aab5"


def test_avatar_fallback_carries_initial():
    svg = _avatar_fallback("Limao")
    assert svg.startswith("data:image/svg+xml")
    assert "L" in svg


#: build_snapshot_message -------------------------------------------------------


async def test_build_snapshot_message_resolves_identity():
    guild = MagicMock()
    member = MagicMock()
    member.top_role.colour.value = 0xE74C3C
    guild.get_member.return_value = member
    msg = _message()
    msg.guild = guild
    msg.author.id = 5

    info = await build_snapshot_message(MagicMock(), msg)
    assert info.author_name == "Limao"
    assert info.author_color == "#e74c3c"
    assert info.author_avatar.startswith("data:image/svg+xml")
    assert info.content == "Durma bem amego"
    assert info.image_data_uri == ""
    assert not info.is_bot


async def test_build_snapshot_message_uses_before_for_edit():
    """The bubble documents the OLD state: identity and content from before."""
    before = _message("conteudo antigo")
    after = _message("conteudo novo")
    info = await build_snapshot_message(MagicMock(), after, before=before)
    assert info.content == "conteudo antigo"
    assert info.author_name == "Limao"


async def test_build_snapshot_message_embeds_image_attachment():
    import base64

    msg = _message()
    att = MagicMock()
    att.url = "https://cdn.discordapp.com/attachments/1/2/foto.png"
    att.content_type = "image/png"
    msg.attachments = [att]

    import rosemary.core.chat_image as chat_image

    original = chat_image._download_bytes

    def fake_download(url, *, timeout, max_bytes):
        return b"fakepng"

    chat_image._download_bytes = fake_download
    try:
        info = await build_snapshot_message(MagicMock(), msg)
    finally:
        chat_image._download_bytes = original
    assert info.image_data_uri.startswith("data:image/png;base64,")
    assert base64.b64decode(info.image_data_uri.split(",", 1)[1]) == b"fakepng"


async def test_build_snapshot_message_skips_lottie_stickers():
    msg = _message()
    sticker = MagicMock()
    sticker.format.name = "lottie"
    sticker.url = "https://cdn.discordapp.com/stickers/1.json"
    msg.stickers = [sticker]
    info = await build_snapshot_message(MagicMock(), msg)
    assert info.image_data_uri == ""


#: render_snapshot ---------------------------------------------------------------


def _info(content="Durma bem amego", image=""):
    return SnapshotMessage(
        author_name="Limao",
        author_color="#e74c3c",
        author_avatar=_avatar_fallback("Limao"),
        is_bot=False,
        timestamp=1700000000.0,
        content=content,
        image_data_uri=image,
    )


def test_render_snapshot_png_signature():
    weasyprint = __import__("weasyprint", fromlist=["HTML"])
    pymupdf = __import__("pymupdf")
    del weasyprint, pymupdf  # only assert the optional deps import

    png = render_snapshot([_info()])
    assert png is not None
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_avatar_renders_at_css_size():
    """Regression: WeasyPrint ignores HTML width/height attributes on <img>.

    The bubble used to size the avatar with ``width="40"`` attributes: live
    renders blew the avatar up to its natural size and swallowed the whole
    bubble (user screenshot). The template must size it via CSS; the PDF's
    own image placement proves it (30pt = 40 CSS px at 96dpi).
    """
    import base64
    import struct
    import zlib

    def chunk(tag, data):
        head = struct.pack(">I", len(data)) + tag + data
        return head + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 800, 800, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\xff" * 800 for _ in range(800))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    big_avatar = "data:image/png;base64," + base64.b64encode(png).decode()

    from rosemary.core.chat_image import _bubble_markup, default_templates

    info = SnapshotMessage(
        author_name="Chiina",
        author_color="#e74c3c",
        author_avatar=big_avatar,
        is_bot=False,
        timestamp=1700000000.0,
        content="a",
        image_data_uri="",
    )
    page_template, bubble_template = default_templates()
    items = _bubble_markup(info, bubble_template, "<img src=\"{image}\" />", {})

    import pymupdf
    from weasyprint import HTML

    pdf = HTML(
        string=page_template.replace("__ITEMS__", items)
        .replace("__PAGE_W__", "480")
        .replace("__PAGE_H__", "160")
    ).write_pdf()
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    rects = [
        r
        for image in doc[0].get_images(full=True)
        for r in doc[0].get_image_rects(image[0])
    ]
    assert rects, "avatar image missing from the rendered PDF"
    width_css_px = rects[0].width / 0.75
    assert 44 <= width_css_px <= 52, (
        f"avatar rendered at {width_css_px:.0f} CSS px, expected ~48"
    )


def test_render_snapshot_empty_inputs_degrade_to_none():
    assert render_snapshot([]) is None
    assert render_snapshot([_info(content="")]) is not None  # empty bubble frame


def test_render_snapshot_broken_template_returns_none():
    # WeasyPrint rejects invalid CSS with its own exception: must not raise.
    assert render_snapshot([_info()], template="<style> @page { size: } </style>") is None


def test_render_snapshot_edit_pair_renders_two_bubbles():
    one = render_snapshot([_info("antes")])
    two = render_snapshot([_info("antes"), _info("depois")])
    assert one is not None and two is not None
    assert len(two) > len(one), "two bubbles rasterize more content"
