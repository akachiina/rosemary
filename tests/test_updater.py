"""Updater helpers: rev-list parsing and data backup rotation."""

from __future__ import annotations

from rosemary.core.updater import backup_data, prune_backups


async def test_backup_and_prune(tmp_path):
    data = tmp_path / "data"
    (data / "1").mkdir(parents=True)
    (data / "1" / "settings.json").write_text("{}")
    root = tmp_path / "backups"

    first = backup_data(data, root)
    assert (first / "1" / "settings.json").exists()

    second = backup_data(data, root)
    assert second != first
    assert len(list(root.glob("data-*"))) == 2

    prune_backups(root, keep=1)
    assert len(list(root.glob("data-*"))) == 1
