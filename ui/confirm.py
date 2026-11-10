"""Backward-compatible import of the kit's confirmation dialog.

The dialog moved to :mod:`rosemary.ui.kit` (``Confirm``): one lifecycle, one
plain-text layer. This module keeps the old names importable so the cogs and
the tests that predate the kit need no churn.
"""

from __future__ import annotations

from rosemary.ui.kit import Confirm as ConfirmView
from rosemary.ui.kit import ConfirmResult

__all__ = ["ConfirmResult", "ConfirmView"]
