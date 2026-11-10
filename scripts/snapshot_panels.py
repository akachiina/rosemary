"""Write the golden panel fixtures.

Run from inside ``rosemary/``::

    ../.venv/bin/python scripts/snapshot_panels.py

Every representative panel is prepared with the real theme and catalog and
written to ``tests/golden/panels/<name>.json``. Re-run it after a deliberate
change to what a member sees or clicks and review the diff: the golden test
(``tests/test_golden_panels.py``) compares the current render against these
files, so a kit/runtime refactor never changes a panel unnoticed.
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

from golden_panels import GOLDEN_DIR, render_all  # noqa: E402


async def main() -> int:
    snapshots = await render_all()
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for name, snapshot in snapshots.items():
        path = GOLDEN_DIR / f"{name}.json"
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(f"wrote {len(snapshots)} fixtures to {GOLDEN_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
