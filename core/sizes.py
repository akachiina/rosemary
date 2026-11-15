"""Human-readable file sizes for the copy the member reads.

A size is a machine value until the copy makes it readable: ``1.2 MB`` is what
the member reads, ``1234567`` is what the storage holds. Every send site that
mentions a byte count (the converter, the image upload limits) goes through
:func:`human_size`, so the format stays the same everywhere.
"""

from __future__ import annotations


def human_size(size: int) -> str:
    """A short human-readable byte count (``1.2 MB``)."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


__all__ = ["human_size"]
