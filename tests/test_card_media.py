"""Card media: ``{media/<name>}`` resolved into attachment URLs + files.

The pass runs before the card is compiled, so the ``attachment://`` URL
survives; the send site attaches the returned ``discord.File`` list.
"""

from __future__ import annotations

from rosemary.core.card_media import resolve_card_media
from rosemary.core.design.ir import (
    Action,
    Author,
    Card,
    Gallery,
    Section,
    Text,
    Thumbnail,
)
from rosemary.core.theme_assets import ThemeAssets


def _assets(tmp_path):
    media = tmp_path / "assets" / "media"
    media.mkdir(parents=True)
    (media / "logo.png").write_bytes(b"logo")
    (media / "banner.png").write_bytes(b"banner")
    return ThemeAssets(
        media={"logo": media / "logo.png", "banner": media / "banner.png"}
    )


def test_resolves_every_url_slot_and_collects_files(tmp_path):
    card = Card(
        key="x",
        form="card",
        image="{media/banner}",
        thumbnail="{media/logo}",
        author=Author(name="a", icon_url="{media/logo}"),
        actions=(Action(label="go", url="{media/banner}"),),
        blocks=(
            Gallery(urls=("{media/banner}", "https://x/y.png")),
            Section(children=(Text("hi"),), accessory=Thumbnail(url="{media/logo}")),
        ),
    )
    resolved, files = resolve_card_media(card, _assets(tmp_path))
    assert resolved.image == "attachment://banner.png"
    assert resolved.thumbnail == "attachment://logo.png"
    assert resolved.author.icon_url == "attachment://logo.png"
    assert resolved.actions[0].url == "attachment://banner.png"
    assert resolved.blocks[0].urls == (
        "attachment://banner.png",
        "https://x/y.png",
    )
    assert resolved.blocks[1].accessory.url == "attachment://logo.png"
    assert sorted(f.filename for f in files) == ["banner.png", "logo.png"]


def test_unknown_media_ref_drops_and_attaches_nothing(tmp_path):
    card = Card(key="x", image="{media/missing}")
    resolved, files = resolve_card_media(card, _assets(tmp_path))
    assert resolved.image == ""
    assert files == []


def test_two_files_with_the_same_name_get_unique_filenames(tmp_path):
    media = tmp_path / "assets" / "media"
    (media / "sub").mkdir(parents=True)
    (media / "a.png").write_bytes(b"a")
    (media / "sub" / "a.png").write_bytes(b"b")
    assets = ThemeAssets(
        media={"a": media / "a.png", "sub/a": media / "sub" / "a.png"}
    )
    card = Card(key="x", image="{media/a}", blocks=(Gallery(urls=("{media/sub/a}",)),))
    resolved, files = resolve_card_media(card, assets)
    assert resolved.image == "attachment://a.png"
    assert resolved.blocks[0].urls[0] == "attachment://a_2.png"
    assert sorted(f.filename for f in files) == ["a.png", "a_2.png"]
