"""Updater helpers: rev-list parsing and data backup rotation."""

from __future__ import annotations

from rosemary.core.updater import (
    backup_data,
    describe_target,
    is_newer,
    latest_stable,
    parse_tag,
    prune_backups,
    should_notify_failure,
)


def test_parse_tag():
    assert parse_tag("v1.2.3") == (1, 2, 3)
    assert parse_tag("  v0.1.0  ") == (0, 1, 0)
    assert parse_tag("1.2.3") is None
    assert parse_tag("v1.2") is None
    assert parse_tag("main") is None
    assert parse_tag("") is None


def test_latest_stable_semver_order():
    tags = ["v1.9.0", "v1.10.0", "v2.0.0", "main", "not-a-version", "v0.9.9"]
    assert latest_stable(tags) == "v2.0.0"
    assert latest_stable(["main", "broken"]) is None
    assert latest_stable([]) is None


def test_is_newer():
    assert is_newer("0.1.0", "v0.2.0") is True
    assert is_newer("1.2.0", "v1.2.0") is False
    assert is_newer("2.0.0", "v1.9.9") is False
    assert is_newer("v1.10.0", "v1.9.0") is False
    assert is_newer("bogus", "v1.0.0") is False


def test_describe_target():
    stable = {"channel": "stable", "branch": "main"}
    assert describe_target(
        current_version="0.1.0", latest_tag="v0.2.0", behind=0, **stable
    ) == ("v0.2.0", "v0.2.0")
    assert describe_target(
        current_version="0.2.0", latest_tag="v0.2.0", behind=0, **stable
    ) == (None, "")
    assert describe_target(
        current_version="0.1.0", latest_tag=None, behind=0, **stable
    ) == (None, "")
    assert describe_target(
        channel="git", current_version="0.1.0", latest_tag=None, behind=3, branch="main"
    ) == ("origin/main", "3 commits")
    assert describe_target(
        channel="git", current_version="0.1.0", latest_tag=None, behind=0, branch="main"
    ) == (None, "")


def test_should_notify_failure_throttle():
    assert should_notify_failure(None, "dirty") == (True, "dirty")
    assert should_notify_failure("dirty", "dirty") == (False, "dirty")
    assert should_notify_failure("dirty", "fetch") == (True, "fetch")
    assert should_notify_failure("fetch", None) == (False, None)


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
