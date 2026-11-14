"""The conversion engine: image formats, PDFs, media and the limits."""

from __future__ import annotations

import io
import shutil
import subprocess
import wave

import pytest
from PIL import Image

from rosemary.core.convert import (
    STICKER_MAX_BYTES,
    STICKER_SIZE,
    ConvertError,
    convert,
    kind_of,
    make_sticker,
    targets_for,
)


def _png_bytes(size=(40, 30), color=(255, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def _pdf_bytes(text="hello") -> bytes:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    return document.tobytes()


def test_kind_of_and_targets():
    assert kind_of("a.png") == "image"
    assert kind_of("a.MP4") == "video"
    assert kind_of("a.mp3") == "audio"
    assert kind_of("a.pdf") == "pdf"
    assert kind_of("a.txt") is None
    assert "sticker" in targets_for("image")
    assert "pdf" in targets_for("image")
    assert "bmp" in targets_for("image")
    assert "mov" in targets_for("video")
    assert "flac" in targets_for("video")
    assert "flac" in targets_for("audio")
    assert targets_for("pdf") == ("png", "txt")


def test_image_conversions():
    data = _png_bytes()
    for target in ("png", "jpg", "webp", "gif", "bmp", "tiff", "ico"):
        output, name = convert(data, "picture.png", target, max_mb=25, timeout=30)
        assert output
        assert name == f"picture.{target}"
        if target != "ico":
            Image.open(io.BytesIO(output)).verify()


def test_image_to_pdf():
    output, name = convert(
        _png_bytes((120, 80)), "picture.png", "pdf", max_mb=25, timeout=30
    )
    assert name == "picture.pdf"
    assert output.startswith(b"%PDF")


def test_pdf_to_png_and_text():
    pdf = _pdf_bytes("hello rosemary")
    output, name = convert(pdf, "doc.pdf", "png", max_mb=25, timeout=30)
    assert name == "doc.png"
    Image.open(io.BytesIO(output)).verify()
    text, name = convert(pdf, "doc.pdf", "txt", max_mb=25, timeout=30)
    assert name == "doc.txt"
    assert "hello rosemary" in text.decode("utf-8")


def test_sticker_is_320_and_under_the_limit():
    data = _png_bytes((900, 600))
    output, name = make_sticker(data, "big.png")
    assert name == "sticker.png"
    assert len(output) <= STICKER_MAX_BYTES
    image = Image.open(io.BytesIO(output))
    assert image.size == (STICKER_SIZE, STICKER_SIZE)


def test_sticker_from_an_animated_gif():
    frames = [Image.new("RGB", (80, 80), color) for color in ((255, 0, 0), (0, 255, 0))]
    buffer = io.BytesIO()
    frames[0].save(
        buffer, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0
    )
    output, name = make_sticker(buffer.getvalue(), "anim.gif")
    assert name in ("sticker.gif", "sticker.png")
    assert len(output) <= STICKER_MAX_BYTES
    assert Image.open(io.BytesIO(output)).size == (STICKER_SIZE, STICKER_SIZE)


def test_too_large_is_rejected():
    with pytest.raises(ConvertError) as error:
        convert(b"x" * 2048, "big.png", "png", max_mb=0, timeout=30)
    assert error.value.code == "too_large"


def test_unknown_type_is_rejected():
    with pytest.raises(ConvertError) as error:
        convert(b"data", "notes.txt", "png", max_mb=25, timeout=30)
    assert error.value.code == "type"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_wav_to_mp3():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 8000)
    output, name = convert(buffer.getvalue(), "tone.wav", "mp3", max_mb=25, timeout=60)
    assert name == "tone.mp3"
    assert output[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_mp4_to_mp3(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"), "-y",
            "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(clip),
        ],
        capture_output=True,
        timeout=60,
    )
    output, name = convert(clip.read_bytes(), "clip.mp4", "mp3", max_mb=25, timeout=60)
    assert name == "clip.mp3"
    assert len(output) > 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_video_to_sticker(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i",
            "color=c=red:s=320x240:d=1", "-c:v", "libx264", str(clip),
        ],
        capture_output=True,
        timeout=60,
    )
    output, name = convert(
        clip.read_bytes(), "clip.mp4", "sticker", max_mb=25, timeout=60
    )
    assert name == "sticker.gif"
    assert len(output) <= STICKER_MAX_BYTES
    assert Image.open(io.BytesIO(output)).size == (STICKER_SIZE, STICKER_SIZE)
