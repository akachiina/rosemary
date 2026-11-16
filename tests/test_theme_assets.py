"""Theme assets: the per-theme ``assets/`` folder (media + emojis).

The module is Discord-free: it indexes the files, validates them and resolves
the ``{media/<name>}`` placeholders to ``file://`` URLs. The emoji upload and
the card attachment live in their own layers (tested elsewhere).
"""

from __future__ import annotations

from rosemary.core.theme_assets import (
    ThemeAssets,
    emoji_asset_filename,
    emoji_asset_key,
    ensure_assets_layout,
    media_urls,
    resolve_media_refs,
    scan_theme_assets,
    validate_theme_assets,
)


def _package(root, *, media=None, emojis=None):
    """A theme package with the given asset files (name -> bytes)."""
    for name, data in (media or {}).items():
        path = root / "assets" / "media" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for name, data in (emojis or {}).items():
        path = root / "assets" / "emojis" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def test_scan_indexes_media_and_emojis_by_key(tmp_path):
    _package(
        tmp_path,
        media={"logo.png": b"x", "bg/padrao.png": b"y"},
        emojis={"miao.png": b"z"},
    )
    assets = scan_theme_assets(tmp_path)
    assert set(assets.media) == {"logo", "bg/padrao"}
    assert set(assets.emojis) == {"miao"}
    assert assets.media_path("logo").name == "logo.png"
    assert assets.media_path("bg/padrao.png").name == "padrao.png"
    assert assets.media_path("missing") is None


def test_scan_skips_unknown_types(tmp_path):
    _package(tmp_path, media={"notes.txt": b"x", "ok.png": b"y"})
    assets = scan_theme_assets(tmp_path)
    assert set(assets.media) == {"ok"}


def test_media_urls_are_file_uris(tmp_path):
    _package(tmp_path, media={"logo.png": b"x"})
    urls = media_urls(scan_theme_assets(tmp_path))
    assert urls["logo"].startswith("file://")
    assert urls["logo"].endswith("/logo.png")


def test_resolve_media_refs_replaces_and_drops_unknown():
    urls = {"logo": "file:///tmp/logo.png", "bg/padrao": "file:///tmp/bg.png"}
    text = '<img src="{media/logo}"><div style="background:{media/bg/padrao}">'
    out = resolve_media_refs(text, urls)
    assert "file:///tmp/logo.png" in out
    assert "file:///tmp/bg.png" in out
    assert "{media/" not in out
    # An unknown reference disappears (never the raw placeholder).
    assert resolve_media_refs("x {media/nope} y", urls) == "x  y"


def test_validate_reports_bad_types_and_sizes(tmp_path):
    _package(tmp_path, media={"ok.png": b"x", "bad.exe": b"y"})
    problems = validate_theme_assets(tmp_path)
    assert any("bad.exe" in problem for problem in problems)


def test_ensure_assets_layout_creates_the_folders(tmp_path):
    ensure_assets_layout(tmp_path)
    assert (tmp_path / "assets" / "media" / ".gitkeep").is_file()
    assert (tmp_path / "assets" / "emojis" / ".gitkeep").is_file()


def test_emoji_key_and_filename_are_namespaced():
    assert emoji_asset_key("fancy", "miao.png") == "fancy/miao"
    name = emoji_asset_filename("fancy", "miao.png")
    assert name.startswith("t_fancy_")


def test_theme_assets_is_empty_by_default():
    assert ThemeAssets().is_empty()
    assert not ThemeAssets().media_url("logo")
