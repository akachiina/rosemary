"""File conversion for ``/convert file``: images, video, audio and documents.

Images go through Pillow. Video and audio go through ``ffmpeg`` (the system
binary when present, the bundled ``imageio-ffmpeg`` otherwise). GIFs from
video use the two-pass palette (palettegen/paletteuse) so the colors and the
frame rate survive; a too-large result is retried at a lower frame rate and
size before giving up. PDFs go through PyMuPDF (image to PDF, first page to
PNG or text). The sticker target is a plain file: a 320x320 PNG/GIF under
512 KB the member can upload as a Discord sticker.

The functions are synchronous (subprocess/Pillow/PyMuPDF are blocking): the
panel runs them with :func:`asyncio.to_thread`.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

from rosemary.core.convert_registry import AUDIO, IMAGE, PDF, VIDEO, targets_for

#: The maximum sticker size Discord accepts.
STICKER_MAX_BYTES = 512 * 1024

#: The sticker edge Discord requires.
STICKER_SIZE = 320

_IMAGE_EXT = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".ico",
}
_VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
_AUDIO_EXT = {".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac", ".aac", ".opus"}
_PDF_EXT = {".pdf"}


class ConvertError(Exception):
    """A conversion failure with a stable code for the notice."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def kind_of(filename: str) -> str | None:
    """The source kind (``image``/``video``/``audio``/``pdf``) or ``None``."""
    suffix = Path(filename or "").suffix.lower()
    if suffix in _IMAGE_EXT:
        return IMAGE
    if suffix in _VIDEO_EXT:
        return VIDEO
    if suffix in _AUDIO_EXT:
        return AUDIO
    if suffix in _PDF_EXT:
        return PDF
    return None


def convert(
    data: bytes,
    filename: str,
    target: str,
    *,
    max_mb: int,
    timeout: int,
) -> tuple[bytes, str]:
    """Convert ``data`` to ``target``; returns ``(bytes, filename)``."""
    if len(data) > max_mb * 1024 * 1024:
        raise ConvertError("too_large")
    kind = kind_of(filename)
    if kind is None:
        raise ConvertError("type")
    if target not in targets_for(kind):
        raise ConvertError("type")
    if target == "sticker":
        if kind == VIDEO:
            return _video_sticker(data, filename, timeout)
        return make_sticker(data, filename)
    if kind == IMAGE:
        if target == "pdf":
            return _image_to_pdf(data, filename)
        return _convert_image(data, filename, target)
    if kind == PDF:
        if target == "txt":
            return _pdf_to_text(data, filename)
        return _pdf_to_image(data, filename)
    return _convert_media(data, filename, target, timeout)


def _stem(filename: str) -> str:
    return Path(filename or "file").stem or "file"


def _convert_image(data: bytes, filename: str, target: str) -> tuple[bytes, str]:
    image = Image.open(io.BytesIO(data))
    image.load()
    out = io.BytesIO()
    if target == "png":
        image.save(out, "PNG", optimize=True)
    elif target == "jpg":
        image.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
    elif target == "webp":
        image.save(out, "WEBP", quality=90, method=6)
    elif target == "bmp":
        image.convert("RGB").save(out, "BMP")
    elif target == "tiff":
        image.save(out, "TIFF")
    elif target == "ico":
        sizes = [(256, 256)] if max(image.size) > 256 else [image.size]
        image.save(out, "ICO", sizes=sizes)
    else:
        frames = [frame.convert("RGBA") for frame in ImageSequence.Iterator(image)]
        if len(frames) > 1:
            duration = int(image.info.get("duration") or 100)
            frames[0].save(
                out,
                "GIF",
                save_all=True,
                append_images=frames[1:],
                duration=duration,
                loop=0,
                optimize=True,
            )
        else:
            image.save(out, "GIF", optimize=True)
    return out.getvalue(), f"{_stem(filename)}.{target}"


def _image_to_pdf(data: bytes, filename: str) -> tuple[bytes, str]:
    """One image as a one-page PDF (PyMuPDF)."""
    import pymupdf

    try:
        image = pymupdf.open(stream=data, filetype=Path(filename).suffix.lstrip("."))
        rect = image[0].rect
        document = pymupdf.open()
        page = document.new_page(width=rect.width, height=rect.height)
        page.insert_image(rect, stream=data)
        return document.tobytes(), f"{_stem(filename)}.pdf"
    except (RuntimeError, ValueError) as exc:
        raise ConvertError("pdf") from exc


def _pdf_to_image(data: bytes, filename: str) -> tuple[bytes, str]:
    """The first page of a PDF as a PNG (PyMuPDF)."""
    import pymupdf

    try:
        document = pymupdf.open(stream=data, filetype="pdf")
        page = document.load_page(0)
        pixmap = page.get_pixmap(dpi=150, colorspace=pymupdf.csRGB)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        out = io.BytesIO()
        image.save(out, "PNG", optimize=True)
        return out.getvalue(), f"{_stem(filename)}.png"
    except (RuntimeError, ValueError) as exc:
        raise ConvertError("pdf") from exc


def _pdf_to_text(data: bytes, filename: str) -> tuple[bytes, str]:
    """The text of a PDF (PyMuPDF)."""
    import pymupdf

    try:
        document = pymupdf.open(stream=data, filetype="pdf")
        text = "\n".join(page.get_text() for page in document)
        return text.encode("utf-8"), f"{_stem(filename)}.txt"
    except (RuntimeError, ValueError) as exc:
        raise ConvertError("pdf") from exc


def make_sticker(data: bytes, filename: str) -> tuple[bytes, str]:
    """A 320x320 sticker file under 512 KB (PNG for static, GIF for animated)."""
    image = Image.open(io.BytesIO(data))
    image.load()
    animated = getattr(image, "is_animated", False) and getattr(image, "n_frames", 1) > 1
    if animated:
        frames = []
        durations = []
        for frame in ImageSequence.Iterator(image):
            frames.append(_sticker_frame(frame))
            durations.append(int(frame.info.get("duration") or 100))
        out = io.BytesIO()
        frames[0].save(
            out,
            "GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0,
            optimize=True,
            disposal=2,
        )
        if out.tell() <= STICKER_MAX_BYTES:
            return out.getvalue(), "sticker.gif"
        # Too big: fewer colors and every other frame, then try again.
        for colors in (128, 64, 32):
            reduced = [
                frame.convert("P", palette=Image.ADAPTIVE, colors=colors)
                for frame in frames[::2]
            ]
            out = io.BytesIO()
            reduced[0].save(
                out,
                "GIF",
                save_all=True,
                append_images=reduced[1:],
                duration=[d * 2 for d in durations[::2]],
                loop=0,
                optimize=True,
                disposal=2,
            )
            if out.tell() <= STICKER_MAX_BYTES:
                return out.getvalue(), "sticker.gif"
        return out.getvalue(), "sticker.gif"
    out = io.BytesIO()
    _sticker_frame(image).save(out, "PNG", optimize=True)
    if out.tell() <= STICKER_MAX_BYTES:
        return out.getvalue(), "sticker.png"
    quantized = _sticker_frame(image).convert("P", palette=Image.ADAPTIVE, colors=128)
    out = io.BytesIO()
    quantized.save(out, "PNG", optimize=True)
    return out.getvalue(), "sticker.png"


def _video_sticker(
    data: bytes, filename: str, timeout: int
) -> tuple[bytes, str]:
    """A 320x320 GIF sticker under 512 KB from a video (ffmpeg).

    A short clip (3s, 12 fps) is scaled and padded to 320x320 with the
    two-pass palette. A too-large result is retried shorter and slower; if it
    still does not fit, the first frame becomes a static PNG sticker.
    """
    exe = _ffmpeg()
    with tempfile.TemporaryDirectory(prefix="rosemary-sticker-") as tmp:
        root = Path(tmp)
        source = root / (Path(filename).name or "video")
        source.write_bytes(data)
        for seconds, fps in ((3, 12), (2, 8)):
            out = root / "sticker.gif"
            _run(
                [
                    exe, "-y", "-i", str(source), "-t", str(seconds),
                    "-vf",
                    f"fps={fps},scale=320:320:force_original_aspect_ratio=decrease,"
                    "pad=320:320:(ow-iw)/2:(oh-ih)/2,split [a][b];"
                    "[a]palettegen=stats_mode=diff [p];"
                    "[b][p]paletteuse=dither=bayer:bayer_scale=5",
                    "-loop", "0",
                    str(out),
                ],
                timeout,
            )
            if out.stat().st_size <= STICKER_MAX_BYTES:
                return out.read_bytes(), "sticker.gif"
        # Static fallback: the first frame as a 320x320 PNG.
        frame = root / "frame.png"
        _run(
            [
                exe, "-y", "-i", str(source), "-frames:v", "1",
                "-vf",
                "scale=320:320:force_original_aspect_ratio=decrease,"
                "pad=320:320:(ow-iw)/2:(oh-ih)/2",
                str(frame),
            ],
            timeout,
        )
        return make_sticker(frame.read_bytes(), "frame.png")


def _sticker_frame(frame: Image.Image) -> Image.Image:
    """One 320x320 RGBA frame with the picture contained (no crop)."""
    source = frame.convert("RGBA")
    contained = ImageOps.contain(source, (STICKER_SIZE, STICKER_SIZE))
    canvas = Image.new("RGBA", (STICKER_SIZE, STICKER_SIZE), (0, 0, 0, 0))
    offset = (
        (STICKER_SIZE - contained.width) // 2,
        (STICKER_SIZE - contained.height) // 2,
    )
    canvas.alpha_composite(contained, offset)
    return canvas


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - depends on the host
        raise ConvertError("ffmpeg") from exc


def _run(args: list[str], timeout: int) -> None:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConvertError("timeout") from exc
    if result.returncode != 0:
        raise ConvertError("ffmpeg")


def _convert_media(
    data: bytes, filename: str, target: str, timeout: int
) -> tuple[bytes, str]:
    exe = _ffmpeg()
    with tempfile.TemporaryDirectory(prefix="rosemary-convert-") as tmp:
        root = Path(tmp)
        source = root / (Path(filename).name or "input")
        source.write_bytes(data)
        if target == "gif":
            return _video_to_gif(exe, source, root, timeout)
        out = root / f"output.{target}"
        args = {
            "mp4": [
                "-c:v", "libx264", "-crf", "23", "-preset", "medium",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            ],
            "webm": [
                "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
                "-deadline", "good", "-cpu-used", "2",
                "-c:a", "libopus", "-b:a", "96k",
            ],
            "mov": ["-c", "copy"],
            "mkv": ["-c", "copy"],
            "mp3": ["-vn", "-c:a", "libmp3lame", "-q:a", "2"],
            "ogg": ["-vn", "-c:a", "libvorbis", "-q:a", "5"],
            "wav": ["-vn", "-c:a", "pcm_s16le"],
            "m4a": ["-vn", "-c:a", "aac", "-b:a", "192k"],
            "flac": ["-vn", "-c:a", "flac"],
            "aac": ["-vn", "-c:a", "aac", "-b:a", "192k"],
            "opus": ["-vn", "-c:a", "libopus", "-b:a", "96k"],
        }[target]
        _run([exe, "-y", "-i", str(source), *args, str(out)], timeout)
        return out.read_bytes(), f"{_stem(filename)}.{target}"


def _video_to_gif(
    exe: str, source: Path, root: Path, timeout: int
) -> tuple[bytes, str]:
    palette = root / "palette.png"
    for fps, width in ((15, 480), (10, 320)):
        _run(
            [
                exe, "-y", "-i", str(source),
                "-vf", f"fps={fps},scale={width}:-1:flags=lanczos,palettegen=stats_mode=diff",
                str(palette),
            ],
            timeout,
        )
        out = root / "output.gif"
        _run(
            [
                exe, "-y", "-i", str(source), "-i", str(palette),
                "-lavfi",
                f"fps={fps},scale={width}:-1:flags=lanczos[x];[x][1:v]"
                "paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
                str(out),
            ],
            timeout,
        )
        if out.stat().st_size <= 10 * 1024 * 1024:
            return out.read_bytes(), f"{source.stem}.gif"
    return out.read_bytes(), f"{source.stem}.gif"


__all__ = [
    "STICKER_MAX_BYTES",
    "STICKER_SIZE",
    "ConvertError",
    "convert",
    "kind_of",
    "make_sticker",
    "targets_for",
]
