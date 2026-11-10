"""Write the golden notice fixture.

Run from inside ``rosemary/``::

    ../.venv/bin/python scripts/snapshot_notices.py

Every representative notice is rendered with the real theme and catalog and
written to ``tests/golden/notices.json``. Re-run it after a deliberate change
to what a member reads and review the diff: the golden test
(``tests/test_golden_notices.py``) compares the current render against the
fixture, so a notice refactor never changes the line unnoticed.
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

from golden_notices import GOLDEN_PATH, render_all  # noqa: E402


async def main() -> int:
    snapshots = await render_all()
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(
        json.dumps(snapshots, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {sum(len(lines) for lines in snapshots.values())} notices to {GOLDEN_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
