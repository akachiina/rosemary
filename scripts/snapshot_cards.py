"""Write the golden card fixtures.

Run from inside ``rosemary/``::

    ../.venv/bin/python scripts/snapshot_cards.py

Every registered card is rendered with its sample variables and written to
``tests/golden/cards/<key>.json``. Re-run it after a deliberate change to what
a member receives and review the diff: the golden test
(``tests/test_golden_cards.py``) compares the current render against these
files, so an accidental change never reaches production unnoticed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The repo root (for ``import rosemary``) and the tests dir (for the shared
# golden helpers).
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from golden_cards import GOLDEN_DIR, render_all  # noqa: E402


async def main() -> int:
    snapshots = await render_all()
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for key, snapshot in snapshots.items():
        path = GOLDEN_DIR / f"{key}.json"
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(f"wrote {len(snapshots)} fixtures to {GOLDEN_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
