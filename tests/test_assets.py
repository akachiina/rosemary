"""Asset registry (core/assets.py): the scan, the upload and the references.

The registry is the bridge between the images shipped in ``assets/`` and
Discord: every file under ``assets/emojis/`` becomes an application emoji
(uploaded once, at startup) and every reference (``{emojis/op_emoji}``)
resolves to the real emoji mention.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from rosemary.core.assets import AssetRegistry, emoji_name, resolve_refs

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _write(root, relative: str, data: bytes = PNG) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _emoji(name: str, emoji_id: int = 42):
    emoji = MagicMock()
    emoji.name = name
    emoji.mention = f"<:{name}:{emoji_id}>"
    return emoji


def test_scan_indexes_the_emojis_folder(tmp_path):
    _write(tmp_path, "emojis/op_emoji.png")
    _write(tmp_path, "emojis/myserver/custom.png")
    _write(tmp_path, "frames/ignored.png")
    registry = AssetRegistry(tmp_path)
    registry.scan()
    assert registry.keys() == ["emojis/myserver/custom", "emojis/op_emoji"]
    assert registry.mention("emojis/op_emoji") == ""  # not uploaded yet


def test_scan_skips_unsupported_types(tmp_path):
    _write(tmp_path, "emojis/notes.txt", b"hello")
    _write(tmp_path, "emojis/op_emoji.png")
    registry = AssetRegistry(tmp_path)
    registry.scan()
    assert registry.keys() == ["emojis/op_emoji"]


def test_emoji_name_follows_discords_rules():
    assert emoji_name("op_emoji") == "op_emoji"
    assert emoji_name("op-emoji") == "op_emoji"
    assert emoji_name("op emoji!") == "op_emoji_"
    assert len(emoji_name("a")) == 2  # at least two characters


def test_colliding_names_get_a_suffix(tmp_path):
    _write(tmp_path, "emojis/op-emoji.png")
    _write(tmp_path, "emojis/op_emoji.png")
    registry = AssetRegistry(tmp_path)
    registry.scan()
    names = sorted(asset.name for asset in registry._assets.values())
    assert names == ["op_emoji", "op_emoji_2"]


async def test_sync_uploads_only_the_missing_emoji(tmp_path):
    _write(tmp_path, "emojis/op_emoji.png")
    _write(tmp_path, "emojis/other.png")
    registry = AssetRegistry(tmp_path)
    registry.scan()
    bot = MagicMock()
    bot.fetch_emojis = AsyncMock(return_value=[_emoji("op_emoji")])
    created = AsyncMock(return_value=_emoji("other", 99))
    bot.create_emoji = created
    assert await registry.sync(bot) == 1
    assert created.await_args.kwargs["name"] == "other"
    assert created.await_args.kwargs["image"] == PNG
    # The emoji Discord already had is adopted, not uploaded again.
    assert registry.mention("emojis/op_emoji") == "<:op_emoji:42>"
    assert registry.mention("emojis/other") == "<:other:99>"


async def test_sync_skips_a_file_over_the_size_limit(tmp_path):
    _write(tmp_path, "emojis/huge.png", b"x" * (256 * 1024 + 1))
    registry = AssetRegistry(tmp_path)
    registry.scan()
    bot = MagicMock()
    bot.fetch_emojis = AsyncMock(return_value=[])
    bot.create_emoji = AsyncMock()
    assert await registry.sync(bot) == 0
    bot.create_emoji.assert_not_called()


def test_resolve_replaces_the_reference_and_keeps_the_rest():
    registry = AssetRegistry()
    registry._mentions["emojis/op_emoji"] = "<:op_emoji:42>"
    assert registry.resolve("**Anonymous** {emojis/op_emoji}") == (
        "**Anonymous** <:op_emoji:42>"
    )
    # The extension is optional.
    assert registry.resolve("{emojis/op_emoji.png}") == "<:op_emoji:42>"


def test_unknown_reference_resolves_to_nothing():
    registry = AssetRegistry()
    assert registry.resolve("hello {emojis/nope}") == "hello "
    assert registry.resolve("{emojis/nope}") == ""


def test_plain_placeholders_are_not_asset_references():
    registry = AssetRegistry()
    assert registry.resolve("{user} {server}") == "{user} {server}"


def test_add_bytes_registers_a_runtime_asset():
    registry = AssetRegistry()
    registry.add_bytes("emojis/myserver/custom", PNG, scope="guild:myserver")
    assert registry.keys() == ["emojis/myserver/custom"]
    assert registry.mention("emojis/myserver/custom") == ""


def test_module_resolver_uses_the_process_registry(monkeypatch):
    from rosemary.core import assets

    monkeypatch.setitem(assets.REGISTRY._mentions, "emojis/op_emoji", "<:op_emoji:7>")
    assert resolve_refs("{emojis/op_emoji}") == "<:op_emoji:7>"


def test_safe_format_resolves_the_asset_reference(monkeypatch):
    from rosemary.core import assets
    from rosemary.core.cards import safe_format

    monkeypatch.setitem(assets.REGISTRY._mentions, "emojis/op_emoji", "<:op_emoji:7>")
    assert safe_format("**{alias_name}**{emojis/op_emoji}", {"alias_name": "K7Q"}) == (
        "**K7Q**<:op_emoji:7>"
    )
    # An unknown reference never leaks its raw text.
    assert safe_format("**{alias_name}**{emojis/nope}", {"alias_name": "K7Q"}) == (
        "**K7Q**"
    )


def test_shipped_assets_are_referenceable():
    """The shipped ``assets/emojis/`` folder carries the referenced file."""
    from rosemary.core.assets import ASSETS_DIR

    assert (ASSETS_DIR / "emojis" / "op_emoji.png").is_file()
