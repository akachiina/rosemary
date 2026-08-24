"""Entrypoint for ``python -m rosemary``."""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from rosemary.bot import main  # noqa: E402

if __name__ == "__main__":
    main()
