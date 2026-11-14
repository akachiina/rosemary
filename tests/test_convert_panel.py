"""The ``/convert file`` picker and its ephemeral result card."""

from __future__ import annotations

import io

from PIL import Image
from tests.golden_panels import make_bot

from rosemary.core.host import load_host_config
from rosemary.ui.convert_panel import ConvertPanel, human_size


class _Followup:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def send(self, **kwargs) -> None:
        self.kwargs = kwargs


class _Interaction:
    def __init__(self) -> None:
        self.followup = _Followup()


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (255, 0, 0)).save(buffer, "PNG")
    return buffer.getvalue()


def test_human_size():
    assert human_size(512) == "512 B"
    assert human_size(2048) == "2.0 KB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"


async def test_actions_group_by_category():
    bot = make_bot()
    panel = ConvertPanel(bot, 1, _png(), "picture.png", "image", load_host_config())
    await panel.open_screen()
    rows = await panel.actions()
    ids = [item.custom_id for row in rows for item in row.children]
    assert ids == ["convert:go:image", "convert:go:document", "convert:go:sticker"]
    image_select = rows[0].children[0]
    values = [option.value for option in image_select.options]
    assert "sticker" not in values
    all_values = [
        option.value
        for row in rows
        for item in row.children
        for option in item.options
    ]
    assert "png" in values and "pdf" in all_values and "sticker" in all_values
    assert str(image_select.options[0].emoji) == "🖼️"


async def test_format_label():
    bot = make_bot()
    panel = ConvertPanel(bot, 1, _png(), "picture.png", "image", load_host_config())
    assert await panel._format_label("jpg") == "JPEG"
    assert await panel._format_label("sticker") == "Discord Sticker (512 KB)"


async def test_result_is_ephemeral_and_carries_the_file():
    bot = make_bot()
    panel = ConvertPanel(bot, 1, _png(), "picture.png", "image", load_host_config())
    interaction = _Interaction()
    await panel._send_result(interaction, b"jpeg-bytes", "picture.jpg", "JPEG", "")
    kwargs = interaction.followup.kwargs
    assert kwargs is not None
    assert kwargs["ephemeral"] is True
    assert kwargs["file"].filename == "picture.jpg"


async def test_video_panel_has_the_sticker_category():
    bot = make_bot()
    panel = ConvertPanel(bot, 1, b"", "clip.mp4", "video", load_host_config())
    await panel.open_screen()
    rows = await panel.actions()
    ids = [item.custom_id for row in rows for item in row.children]
    assert "convert:go:video" in ids
    assert "convert:go:audio" in ids
    assert "convert:go:sticker" in ids


async def test_choose_reads_the_selected_value(monkeypatch):
    """The select callback reads the raw interaction values (no `.values`)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    bot = make_bot()
    panel = ConvertPanel(bot, 1, b"", "picture.png", "image", load_host_config())
    panel._ack = AsyncMock(return_value=True)
    run = AsyncMock()
    monkeypatch.setattr(panel, "_run", run)
    interaction = SimpleNamespace(data={"values": ["sticker"]})

    await panel._choose(interaction)

    run.assert_awaited_once_with(interaction, "sticker")
