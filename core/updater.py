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


async def origin_slug(repo: Path) -> str:
    """The ``owner/repo`` slug of ``origin`` (empty when unknown).

    Handles both HTTPS (``https://github.com/owner/repo.git``) and SSH
    (``git@github.com:owner/repo.git``) remotes.
    """
    code, out, _ = await _git(repo, "config", "--get", "remote.origin.url")
    if code != 0 or not out:
        return ""
    cleaned = out.strip().removesuffix(".git")
    if cleaned.startswith("git@"):
        cleaned = cleaned.split(":", 1)[-1]
    elif "github.com/" in cleaned:
        cleaned = cleaned.split("github.com/", 1)[-1]
    else:
        return ""
    return cleaned.strip("/") if cleaned.count("/") == 1 else ""


async def build_info(repo: Path) -> tuple[str, str, bool]:
    """``(commit, tag, dirty)`` for the ``/about`` build line."""
    commit = (await _git(repo, "rev-parse", "--short", "HEAD"))[1]
    tag = (await _git(repo, "describe", "--tags", "--exact-match"))[1]
    _code, out, _err = await _git(repo, "status", "--porcelain")
    return commit, tag, bool(out)


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


# ---------------------------------------------------------------------------
# Release tarball (the stable channel, without git)
# ---------------------------------------------------------------------------

#: GitHub API root (public repositories need no token; 60 requests/hour).
_GITHUB_API = "https://api.github.com"

#: The files the release tarball must never overwrite (host data and config).
_KEEP = {"data", "data_backups", ".env", "host.yaml", "venv", ".venv", ".git", "__pycache__"}


async def latest_release_tag(repo: Path) -> str | None:
    """The newest release tag from GitHub, falling back to the git tags."""
    import aiohttp

    slug = await origin_slug(repo)
    if slug:
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
                f"{_GITHUB_API}/repos/{slug}/releases/latest",
                headers={"User-Agent": "Rosemary"},
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    tag = str(data.get("tag_name") or "").strip()
                    if tag:
                        return tag
        except (TimeoutError, aiohttp.ClientError, ValueError):
            pass
    return latest_stable(await fetch_tags(repo))


async def apply_release(repo: Path, tag: str) -> bool:
    """Download the release tarball and copy it over the repo."""
    import tarfile
    import tempfile

    import aiohttp

    slug = await origin_slug(repo)
    if not slug:
        return False
    url = f"https://github.com/{slug}/archive/refs/tags/{tag}.tar.gz"
    tmp = Path(tempfile.mkdtemp(prefix="rosemary-release-"))
    try:
        archive = tmp / "release.tar.gz"
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
            url, headers={"User-Agent": "Rosemary"}
        ) as response:
            if response.status != 200:
                return False
            archive.write_bytes(await response.read())
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(tmp / "src", filter="data")
        roots = list((tmp / "src").iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            return False
        _copy_tree(roots[0], repo)
        return True
    except (TimeoutError, OSError, aiohttp.ClientError, tarfile.TarError):
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _copy_tree(source: Path, target: Path) -> None:
    """Copy the release over the repo, keeping the host-owned paths."""
    for entry in source.iterdir():
        if entry.name in _KEEP:
            continue
        destination = target / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, destination)
