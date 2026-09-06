"""Self-update helpers: git inspection, data backup and restart arguments.

Two update channels: ``git`` follows ``origin/<branch>`` commit by commit,
``stable`` follows the latest ``vX.Y.Z`` release tag. Kept free of discord
imports so the pure parts stay unit-testable; the cog wires them to slash
commands. Updates apply to this repository (``rosemary/`` is its own git
repo) and only run on explicit admin confirmation with a clean tree and a
fresh backup.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

#: Release tags look like ``v1.2.3`` (semver, leading ``v``).
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def parse_tag(tag: str) -> tuple[int, int, int] | None:
    """Parse a ``vX.Y.Z`` tag into a sortable version tuple (or ``None``)."""
    match = _TAG_RE.match(tag.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def latest_stable(tags: list[str]) -> str | None:
    """Newest release tag by semver order; malformed tags are ignored."""
    best: str | None = None
    best_version: tuple[int, int, int] = (-1, -1, -1)
    for tag in tags:
        version = parse_tag(tag)
        if version is not None and version > best_version:
            best, best_version = tag, version
    return best


def is_newer(current: str, latest: str) -> bool:
    """Whether release tag ``latest`` is newer than version ``current``."""
    current_parsed = parse_tag(current if current.startswith("v") else f"v{current}")
    latest_parsed = parse_tag(latest)
    if current_parsed is None or latest_parsed is None:
        return False
    return latest_parsed > current_parsed


def describe_target(
    *,
    channel: str,
    current_version: str,
    latest_tag: str | None,
    behind: int,
    branch: str,
) -> tuple[str | None, str]:
    """Decide the update target without touching the network or disk.

    Returns ``(target_ref, label)`` where ``target_ref`` is ``None`` when
    already up to date. ``latest_tag``/``behind`` come from the fetch step;
    this pure split keeps the decision unit-testable.
    """
    if channel == "stable":
        if latest_tag is None or not is_newer(current_version, latest_tag):
            return None, ""
        return latest_tag, latest_tag
    if behind == 0:
        return None, ""
    return f"origin/{branch}", f"{behind} commits"


def should_notify_failure(last: str | None, current: str | None) -> tuple[bool, str | None]:
    """State-change throttle for repeated auto-check failures.

    Returns ``(notify, stored)``: failures notify only when their signature
    differs from the last one; success (``current=None``) clears the state.
    """
    if current is None:
        return False, None
    if current == last:
        return False, last
    return True, current


async def _git(repo: Path, *args: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    return process.returncode or 0, out.decode().strip(), err.decode().strip()


async def fetch(repo: Path, branch: str) -> bool:
    code, _, _ = await _git(repo, "fetch", "origin", branch)
    return code == 0


async def fetch_tags(repo: Path) -> list[str]:
    """Fetch tags from origin and return all tag names (empty on failure)."""
    code, _, _ = await _git(repo, "fetch", "--tags", "origin")
    if code != 0:
        return []
    code, out, _ = await _git(repo, "tag", "--list")
    if code != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]


async def is_clean(repo: Path) -> bool:
    code, out, _ = await _git(repo, "status", "--porcelain")
    return code == 0 and out == ""


async def compare(repo: Path, ref: str) -> tuple[int, int]:
    """Return ``(behind, ahead)`` commit counts vs ``ref`` (branch or tag)."""
    target = ref if "/" in ref or ref.startswith("v") else f"origin/{ref}"
    code, out, _ = await _git(
        repo, "rev-list", "--left-right", "--count", f"HEAD...{target}"
    )
    if code != 0:
        return 0, 0
    try:
        ahead_raw, behind_raw = out.split()
        return int(behind_raw), int(ahead_raw)
    except ValueError:
        return 0, 0


async def reset_hard(repo: Path, ref: str) -> bool:
    """Reset the tree to ``ref`` (``origin/<branch>`` or a release tag)."""
    target = ref if "/" in ref or ref.startswith("v") else f"origin/{ref}"
    code, _, _ = await _git(repo, "reset", "--hard", target)
    return code == 0


def backup_data(data_dir: Path, backup_root: Path) -> Path:
    """Copy ``data_dir`` to a timestamped folder; returns the backup path."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = backup_root / f"data-{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)
    counter = 1
    while target.exists():
        counter += 1
        target = backup_root / f"data-{stamp}-{counter}"
    shutil.copytree(data_dir, target)
    return target


def prune_backups(backup_root: Path, keep: int = 5) -> None:
    backups = sorted(
        (path for path in backup_root.glob("data-*") if path.is_dir()),
        key=lambda path: path.name,
    )
    for stale in backups[:-max(keep, 1)]:
        shutil.rmtree(stale, ignore_errors=True)
