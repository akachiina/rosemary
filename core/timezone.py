"""Guild timezone resolution for the ``general.timezone`` setting."""

from __future__ import annotations

import re
from datetime import UTC, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def resolve_timezone(value: str | None) -> tzinfo:
    """Parse a user-supplied timezone string into a ``tzinfo``.

    Accepts IANA names (``America/Sao_Paulo``, ``UTC``, ``GMT``) and plain
    offsets such as ``UTC-3``, ``GMT+2``, ``UTC+05:30``, ``-03:00`` or
    ``+0530``. Any unparsable or empty value falls back to UTC.
    """
    if not value:
        return UTC
    value = value.strip()
    if not value:
        return UTC
    if value.upper() in ("UTC", "GMT", "Z", "UTC+0", "GMT+0", "UTC+00:00", "GMT+00:00"):
        return UTC
    try:
        return ZoneInfo(value)
    except (KeyError, ValueError):
        pass
    match = _OFFSET_RE.match(value)
    if match:
        sign, hours, minutes = match.groups()
        offset = timedelta(hours=int(hours), minutes=int(minutes or 0))
        if sign == "-":
            offset = -offset
        return timezone(offset)
    return UTC
