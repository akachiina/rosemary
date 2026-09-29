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
    import io

    from PIL import Image

    msg = _message()
    att = MagicMock()
    att.url = "https://cdn.discordapp.com/attachments/1/2/foto.png"
    att.content_type = "image/png"
    msg.attachments = [att]

    scratch = io.BytesIO()
    Image.new("RGBA", (12, 12), (10, 200, 30, 255)).save(scratch, format="PNG")
    real_png = scratch.getvalue()

    import rosemary.core.chat_image as chat_image

    original = chat_image._download_bytes

    def fake_download(url, *, timeout, max_bytes):
        return real_png

    chat_image._download_bytes = fake_download
    try:
        info = await build_snapshot_message(MagicMock(), msg)
    finally:
        chat_image._download_bytes = original
    assert info.image_data_uri.startswith("data:image/png;base64,")
    # Re-encoded through Pillow: the decoded image matches the source.
    decoded = Image.open(io.BytesIO(base64.b64decode(info.image_data_uri.split(",", 1)[1])))
    assert decoded.size == (12, 12)


async def test_build_snapshot_message_skips_lottie_stickers():
    msg = _message()
    sticker = MagicMock()
    sticker.format.name = "lottie"
    sticker.url = "https://cdn.discordapp.com/stickers/1.json"
    msg.stickers = [sticker]
    info = await build_snapshot_message(MagicMock(), msg)
    assert info.image_data_uri == ""


async def test_build_snapshot_message_embeds_webp_by_extension():
    """Live regression: the webp attachment came with empty content_type and
    was skipped, so the deleted-image message rendered text-only."""
    msg = _message()
    att = MagicMock()
    att.url = "https://cdn.discordapp.com/attachments/1/2/jacket_s_647.webp"
    att.content_type = ""  # what Discord actually served
    msg.attachments = [att]

    import rosemary.core.chat_image as chat_image

    captured: dict = {}

    def fake_download(url, *, timeout, max_bytes):
        captured["url"] = url
        return b"fakebytes"

    def fake_normalize(data, mime, label):
        return "data:image/png;base64,QUJD"

    original_download = chat_image._download_bytes
    original_normalize = chat_image._normalize_image
    chat_image._download_bytes = fake_download
    chat_image._normalize_image = fake_normalize
    try:
        info = await build_snapshot_message(MagicMock(), msg)
    finally:
        chat_image._download_bytes = original_download
        chat_image._normalize_image = original_normalize
    assert captured["url"].endswith(".webp")
    assert info.image_data_uri == "data:image/png;base64,QUJD"


async def test_build_snapshot_message_skips_undecodable_attachment():
    """A fetchable but undecodable file drops out (no blank reserved box)."""
    msg = _message()
    att = MagicMock()
    att.url = "https://cdn.discordapp.com/attachments/1/2/arquivo.png"
    att.content_type = "image/png"
    msg.attachments = [att]

    import rosemary.core.chat_image as chat_image

    original_download = chat_image._download_bytes
    chat_image._download_bytes = lambda url, *, timeout, max_bytes: b"garbage"
    try:
        info = await build_snapshot_message(MagicMock(), msg)
    finally:
        chat_image._download_bytes = original_download
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
    assert 36 <= width_css_px <= 44, (
        f"avatar rendered at {width_css_px:.0f} CSS px, expected ~40"
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


def test_edited_marker_renders_only_on_flagged_bubbles():
    """The localized marker rides the content only when ``edited`` is set."""
    plain = _info("Teste")
    marked = SnapshotMessage(
        author_name="Limao",
        author_color="#e74c3c",
        author_avatar=_avatar_fallback("Limao"),
        is_bot=False,
        timestamp=1700000000.0,
        content="Teste",
        image_data_uri="",
        edited=True,
    )
    png = render_snapshot([plain, marked], edited_marker="(editado)")
    assert png is not None
    # PNG pixels cannot be text-scanned; assert on the markup layer instead.
    from rosemary.core.chat_image import _bubble_markup, default_templates

    page_t, bubble_t = default_templates()
    plain_html = _bubble_markup(plain, bubble_t, "<img src=\"{image}\" />", {}, "(editado)")
    marked_html = _bubble_markup(marked, bubble_t, "<img src=\"{image}\" />", {}, "(editado)")
    assert "(editado)" not in plain_html
    assert "(editado)" in marked_html
    # The marker is escape-safe (a marker containing HTML stays literal).
    evil = _bubble_markup(
        marked, bubble_t, "<img src=\"{image}\" />", {}, "<b>x</b>"
    )
    assert "<b>x</b>" not in evil and "&lt;b&gt;x&lt;/b&gt;" in evil


async def test_edit_pair_flags_only_the_before_bubble():
    """The snapshot documents the edit's BEFORE state: only its bubble gets
    the marker, resolved from the guild's catalog (pt or en)."""
    from unittest.mock import MagicMock

    import rosemary.core.chat_image as chat_image

    async def fake_build(bot_, message, *, before=None):
        source = before if before is not None else message
        return SnapshotMessage(
            author_name="Limao",
            author_color="#e74c3c",
            author_avatar=_avatar_fallback("Limao"),
            is_bot=False,
            timestamp=1700000000.0,
            content=source.content,
            image_data_uri="",
        )

    before = MagicMock(content="antes")
    after = MagicMock(content="depois")
    after.guild_id = 1
    translator = MagicMock()

    async def raw(guild_id, key):
        return {"audit.edited_marker": "(editado)"}.get(key, key)

    translator.raw = raw
    bot = MagicMock(translator=translator)
    bot.theme = MagicMock()

    captured: dict = {}

    def fake_render(infos, *args, **kwargs):
        captured["flags"] = [i.edited for i in infos]
        captured["marker"] = kwargs.get("edited_marker")
        return b"\x89PNG\r\n\x1a\n"

    original_build = chat_image.build_snapshot_message
    original_render = chat_image.render_snapshot
    original_theme_for = chat_image.theme_for if hasattr(chat_image, "theme_for") else None
    import rosemary.core.themes as themes_mod

    original_theme_for = themes_mod.theme_for
    chat_image.build_snapshot_message = fake_build
    chat_image.render_snapshot = fake_render
    themes_mod.theme_for = lambda bot_, gid: MagicMock(snapshot={}, emojis={})
    try:
        await chat_image.render_message_snapshot(bot, after, before=before)
    finally:
        chat_image.build_snapshot_message = original_build
        chat_image.render_snapshot = original_render
        themes_mod.theme_for = original_theme_for
    assert captured["flags"] == [True, False]
    assert captured["marker"] == "(editado)"


def _png_bytes(size: int, rgba: bytes) -> bytes:
    import struct
    import zlib

    def chunk(tag, data):
        head = struct.pack(">I", len(data)) + tag + data
        return head + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + rgba * size for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_normalize_image_reencodes_and_rejects_garbage():
    """Any decodable input re-encodes to PNG; garbage degrades to ``""``.

    A file WeasyPrint cannot decode used to reserve a blank rectangle in the
    bubble (live webp bug): the contract is drop, never blank space.
    """
    import base64
    import io

    from PIL import Image

    from rosemary.core.chat_image import _normalize_image

    scratch = io.BytesIO()
    Image.new("RGBA", (30, 30), (200, 60, 60, 255)).save(scratch, format="PNG")
    good = scratch.getvalue()
    out = _normalize_image(good, "image/png", "test")
    assert out.startswith("data:image/png;base64,")
    roundtrip = Image.open(io.BytesIO(base64.b64decode(out.split(",", 1)[1])))
    assert roundtrip.size == (30, 30)

    assert _normalize_image(b"not an image at all", "image/webp", "test") == ""
    assert _normalize_image(b"", "image/png", "test") == ""


def test_looks_like_image_by_extension():
    from rosemary.core.chat_image import _looks_like_image

    assert _looks_like_image("https://cdn.discordapp.com/a/b/jacket_s_647.webp?x=1")
    assert _looks_like_image("https://x/y.PNG")
    assert not _looks_like_image("https://x/y.mp4")
    assert not _looks_like_image("https://x/y")
    assert not _looks_like_image("")


def test_render_snapshot_fixed_width_regardless_of_content():
    """Every snapshot PNG ships at the same width (fixed-page full-width crop).

    The variable-width crop made each card render at a different scale on
    Discord (tiny one-word bubbles, huge multi-line ones): inconsistent with
    the bot's visual standard.
    """
    import struct

    short = render_snapshot([_info("a")])
    long = render_snapshot([_info("palavra " * 80)])
    assert short is not None and long is not None
    # IHDR pixel width (pymupdf's rect mixes dpi into the number).
    w_short = struct.unpack(">II", short[16:24])[0]
    w_long = struct.unpack(">II", long[16:24])[0]
    assert w_short == w_long, f"{w_short} != {w_long}: width must be constant"
    # 360pt page (480 CSS px) at the 960-side cap: zoom clamps to 960/360.
    assert w_short == 960


def test_render_snapshot_tall_attachment_stays_inside_the_bubble():
    """Live regression: a tall manga scan vanished from the bubble.

    The page budget (220px/bubble) was smaller than a width-capped 2:3
    image: WeasyPrint paginated the overflow onto a second PDF page and the
    rasterizer only ships page 1, leaving an empty tall bubble. The image
    template now caps the preview height and the budget covers it; the PNG
    must actually show the attachment's light pixels below the header.
    """
    import base64
    import io
    import struct

    from PIL import Image, ImageDraw

    scan = Image.new("RGB", (800, 1200), (245, 245, 245))
    ImageDraw.Draw(scan).ellipse([200, 300, 600, 700], fill=(120, 90, 160))
    scratch = io.BytesIO()
    scan.save(scratch, format="PNG")
    scan_uri = "data:image/png;base64," + base64.b64encode(scratch.getvalue()).decode()

    png = render_snapshot([_info("", image=scan_uri)])
    assert png is not None
    assert struct.unpack(">II", png[16:24])[0] == 960

    decoded = Image.open(io.BytesIO(png)).convert("RGBA")
    width, height = decoded.size
    # Count light pixels (the scan's background) in the bubble's LOWER half:
    # an empty bubble has none there (only #32353b background).
    light = sum(
        1
        for x in range(0, width, 6)
        for y in range(height // 2, height, 6)
        if decoded.getpixel((x, y))[:3] >= (230, 230, 230)
    )
    assert light > 200, f"attachment missing from the rendered bubble ({light} light px)"
