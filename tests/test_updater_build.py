"""The build info the /about card shows (commit, tag, dirty tree)."""

from __future__ import annotations

from pathlib import Path

from rosemary.core.updater import build_info, is_newer, origin_slug, parse_tag

REPO = Path(__file__).resolve().parents[1]


async def test_origin_slug_is_the_github_repo():
    assert await origin_slug(REPO) == "akachiina/rosemary"


async def test_build_info_has_a_commit():
    commit, tag, dirty = await build_info(REPO)
    assert commit
    assert isinstance(tag, str)
    assert isinstance(dirty, bool)


def test_is_newer_compares_semver():
    assert is_newer("0.1.0", "v0.2.0") is True
    assert is_newer("0.2.0", "v0.2.0") is False
    assert is_newer("0.2.0", "v0.1.9") is False
    assert parse_tag("v1.2.3") == (1, 2, 3)
    assert parse_tag("nope") is None
