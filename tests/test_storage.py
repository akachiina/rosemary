"""Tests for ``rosemary.core.storage`` (GuildStorage)."""

from __future__ import annotations

from rosemary.core.storage import GuildStorage


async def test_defaults_when_missing(tmp_path):
    store = GuildStorage(tmp_path)
    assert await store.get(123) == {"language": "en-US"}
    # Reading must not create the file.
    assert not (tmp_path / "123" / "settings.json").exists()


async def test_set_and_reload(tmp_path):
    store = GuildStorage(tmp_path)
    await store.set(123, "language", "pt-BR")
    path = tmp_path / "123" / "settings.json"
    assert path.exists()
    reloaded = GuildStorage(tmp_path)
    assert (await reloaded.get(123))["language"] == "pt-BR"


async def test_per_guild_isolation(tmp_path):
    store = GuildStorage(tmp_path)
    await store.set(111, "language", "pt-BR")
    assert (await store.get(111))["language"] == "pt-BR"
    assert (await store.get(222))["language"] == "en-US"


async def test_corrupt_file_returns_defaults(tmp_path):
    guild_dir = tmp_path / "333"
    guild_dir.mkdir(parents=True)
    (guild_dir / "settings.json").write_text("{not json", encoding="utf-8")
    store = GuildStorage(tmp_path)
    assert await store.get(333) == {"language": "en-US"}


async def test_set_all_replaces_document(tmp_path):
    store = GuildStorage(tmp_path)
    await store.set(444, "language", "pt-BR")
    await store.set_all(444, {"leaderboard": {"1": 3}})
    reloaded = GuildStorage(tmp_path, use_defaults=False)
    assert await reloaded.get(444) == {"leaderboard": {"1": 3}}
    with_defaults = GuildStorage(tmp_path)
    assert await with_defaults.get(444) == {"leaderboard": {"1": 3}, "language": "en-US"}
