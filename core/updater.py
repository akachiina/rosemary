"""Self-update helpers: git inspection, data backup and restart arguments.

Kept free of discord imports so the pure parts stay unit-testable; the cog
wires them to slash commands. Updates apply to this repository (``rosemary/``
is its own git repo) and only run on explicit admin confirmation with a
clean tree and a fresh backup.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path


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


async def is_clean(repo: Path) -> bool:
    code, out, _ = await _git(repo, "status", "--porcelain")
    return code == 0 and out == ""


async def compare(repo: Path, branch: str) -> tuple[int, int]:
    """Return ``(behind, ahead)`` commit counts vs ``origin/<branch>``."""
    code, out, _ = await _git(
        repo, "rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"
    )
    if code != 0:
        return 0, 0
    try:
        ahead_raw, behind_raw = out.split()
        return int(behind_raw), int(ahead_raw)
    except ValueError:
        return 0, 0


async def reset_hard(repo: Path, branch: str) -> bool:
    code, _, _ = await _git(repo, "reset", "--hard", f"origin/{branch}")
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
