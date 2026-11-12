"""Pytest bootstrap: make the repo root importable so ``import rosemary`` works.

The session also installs the offline policy of the pictures: the cards draw
their author strip, and the tests must never reach the internet (the CDN of
the avatar, or a host whose DNS never answers).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from offline import offline_pictures  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _offline_pictures():
    """The pictures of the cards are fixed (no network in the tests)."""
    with offline_pictures():
        yield
