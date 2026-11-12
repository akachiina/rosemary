"""The tests never touch the network.

The cards draw their pictures (the author strip): the avatar of the member
comes from the Discord CDN. The fixtures must not depend on the internet
(and a host whose DNS never answers would make the suite crawl), so the
download answers with a fixed picture.

The policy lives here, in one place: the session installs it for every test
(:mod:`tests.conftest`) and the golden harness asks for it explicitly when
it renders outside pytest (``scripts/snapshot_cards.py``).
"""

from __future__ import annotations

import base64
from contextlib import contextmanager

#: The picture the tests draw (a fixed 1x1 PNG).
FIXED_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@contextmanager
def offline_pictures():
    """The pictures of the cards come from a fixed file, never from the web."""
    from rosemary.core.render import identity

    original = identity.download_bytes

    def fake_download(url: str, *, timeout: float, max_bytes: int) -> bytes:
        return FIXED_PNG

    identity.download_bytes = fake_download
    try:
        yield
    finally:
        identity.download_bytes = original
