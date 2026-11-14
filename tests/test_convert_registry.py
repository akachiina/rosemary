"""The declarative catalogue of ``/convert file`` formats.

The registry is the single source of truth the picker and the engine read:
every option must have copy in both catalogs, an icon token (or a fallback),
and the source kinds must resolve to at least one target.
"""

from __future__ import annotations

from tests.catalog import flatten, load_catalog

from rosemary.core.convert_registry import (
    AUDIO,
    CATEGORY_ORDER,
    FORMATS,
    IMAGE,
    PDF,
    VIDEO,
    group_by_category,
    options_for,
    targets_for,
)
from rosemary.ui.theme import load_theme


def test_every_kind_has_formats():
    for kind in (IMAGE, VIDEO, AUDIO, PDF):
        assert targets_for(kind), kind
        assert options_for(kind), kind


def test_targets_cover_the_common_formats():
    assert "png" in targets_for(IMAGE)
    assert "sticker" in targets_for(IMAGE)
    assert "pdf" in targets_for(IMAGE)
    assert "gif" in targets_for(VIDEO)
    assert "mp4" in targets_for(VIDEO)
    assert "mp3" in targets_for(VIDEO)
    assert "flac" in targets_for(VIDEO)
    assert "flac" in targets_for(AUDIO)
    assert "aac" in targets_for(AUDIO)
    assert targets_for(PDF) == ("png", "txt")


def test_groups_keep_the_category_order():
    grouped = group_by_category(IMAGE)
    assert list(grouped) == [c for c in CATEGORY_ORDER if c in grouped]
    assert grouped["image"] and grouped["document"] and grouped["sticker"]


def test_every_option_has_a_label_and_description_in_both_catalogs():
    catalogs = {lang: flatten(load_catalog(lang)) for lang in ("pt-BR", "en-US")}
    missing = []
    for option in FORMATS:
        for lang, flat in catalogs.items():
            for key in (option.label_key, option.description_key):
                if key not in flat:
                    missing.append(f"{lang}:{key}")
    assert missing == []


def test_every_icon_token_exists_in_the_default_theme():
    theme = load_theme()
    missing = [option.icon for option in FORMATS if not theme.emoji(option.icon)]
    assert missing == []
