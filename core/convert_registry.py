"""The declarative catalogue of ``/convert file`` output formats.

One entry per format the picker offers. The send site groups them by
category (one select per category), the panel resolves the label/description
from the catalogs and the icon from the theme, and the engine dispatches on
the source kind. Keeping the list here (not in the panel) is what makes the
family modular: a new format is one entry plus its copy and its token.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The source kinds a file can be.
IMAGE = "image"
VIDEO = "video"
AUDIO = "audio"
PDF = "pdf"

#: The categories the picker groups the formats by.
CATEGORY_IMAGE = "image"
CATEGORY_VIDEO = "video"
CATEGORY_AUDIO = "audio"
CATEGORY_DOCUMENT = "document"
CATEGORY_STICKER = "sticker"

#: The category order the picker shows.
CATEGORY_ORDER = (
    CATEGORY_IMAGE,
    CATEGORY_VIDEO,
    CATEGORY_AUDIO,
    CATEGORY_DOCUMENT,
    CATEGORY_STICKER,
)


@dataclass(frozen=True)
class FormatOption:
    """One output format of the picker.

    ``value``
        The value sent to :func:`rosemary.core.convert.convert`.
    ``category``
        The picker category (one select per category).
    ``sources``
        The source kinds that offer this format.
    ``icon``
        The theme token of the option emoji (``convert_<id>``).
    ``fallback``
        The unicode emoji when the theme has no token.
    ``label_key`` / ``description_key``
        The catalog copy of the option.
    """

    value: str
    category: str
    sources: tuple[str, ...]
    icon: str
    fallback: str
    label_key: str
    description_key: str


def _option(
    value: str,
    category: str,
    sources: tuple[str, ...],
    fallback: str,
    *,
    name: str = "",
    icon: str = "",
) -> FormatOption:
    """Build one entry with the default label/description/icon keys."""
    label = name or value
    return FormatOption(
        value=value,
        category=category,
        sources=sources,
        icon=icon or f"convert_{label}",
        fallback=fallback,
        label_key=f"convert.formats.{label}.label",
        description_key=f"convert.formats.{label}.description",
    )


#: The picker's formats, in display order.
FORMATS: tuple[FormatOption, ...] = (
    # Images -----------------------------------------------------------------
    _option("png", CATEGORY_IMAGE, (IMAGE,), "🖼️"),
    _option("jpg", CATEGORY_IMAGE, (IMAGE,), "🖼️"),
    _option("webp", CATEGORY_IMAGE, (IMAGE,), "🖼️"),
    _option("gif", CATEGORY_IMAGE, (IMAGE,), "🎞️"),
    _option("bmp", CATEGORY_IMAGE, (IMAGE,), "🖼️"),
    _option("tiff", CATEGORY_IMAGE, (IMAGE,), "🖼️"),
    _option("ico", CATEGORY_IMAGE, (IMAGE,), "🔖"),
    # Documents --------------------------------------------------------------
    _option("pdf", CATEGORY_DOCUMENT, (IMAGE,), "📄"),
    _option("png", CATEGORY_DOCUMENT, (PDF,), "🖼️", name="pdf_page"),
    _option("txt", CATEGORY_DOCUMENT, (PDF,), "📝"),
    # Video ------------------------------------------------------------------
    _option("mp4", CATEGORY_VIDEO, (VIDEO,), "🎬"),
    _option("webm", CATEGORY_VIDEO, (VIDEO,), "🎬"),
    _option("mov", CATEGORY_VIDEO, (VIDEO,), "🎬"),
    _option("mkv", CATEGORY_VIDEO, (VIDEO,), "🎬"),
    _option("gif", CATEGORY_VIDEO, (VIDEO,), "🎞️", name="gif_video"),
    # Audio ------------------------------------------------------------------
    _option("mp3", CATEGORY_AUDIO, (VIDEO, AUDIO), "🎵"),
    _option("ogg", CATEGORY_AUDIO, (VIDEO, AUDIO), "🎵"),
    _option("wav", CATEGORY_AUDIO, (VIDEO, AUDIO), "🎧"),
    _option("m4a", CATEGORY_AUDIO, (VIDEO, AUDIO), "🎧"),
    _option("flac", CATEGORY_AUDIO, (VIDEO, AUDIO), "🎧"),
    _option("aac", CATEGORY_AUDIO, (AUDIO,), "🎧"),
    _option("opus", CATEGORY_AUDIO, (AUDIO,), "🎧"),
    # Sticker ----------------------------------------------------------------
    _option("sticker", CATEGORY_STICKER, (IMAGE, VIDEO), "🏷️"),
)


def options_for(kind: str) -> list[FormatOption]:
    """The formats offered for one source kind (the registry order)."""
    return [option for option in FORMATS if kind in option.sources]


def targets_for(kind: str) -> tuple[str, ...]:
    """The output values offered for one source kind (deduplicated)."""
    seen: list[str] = []
    for option in options_for(kind):
        if option.value not in seen:
            seen.append(option.value)
    return tuple(seen)


def category_label_key(category: str) -> str:
    """The catalog key of a category's select placeholder."""
    return f"convert.categories.{category}"


def group_by_category(kind: str) -> dict[str, list[FormatOption]]:
    """The formats of one source kind grouped by category (display order)."""
    grouped: dict[str, list[FormatOption]] = {}
    for option in options_for(kind):
        if option.category not in CATEGORY_ORDER:
            continue
        grouped.setdefault(option.category, []).append(option)
    return grouped


__all__ = [
    "CATEGORY_AUDIO",
    "CATEGORY_DOCUMENT",
    "CATEGORY_IMAGE",
    "CATEGORY_ORDER",
    "CATEGORY_STICKER",
    "CATEGORY_VIDEO",
    "FORMATS",
    "FormatOption",
    "category_label_key",
    "group_by_category",
    "options_for",
    "targets_for",
]
