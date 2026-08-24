"""Resolution of the ``general.timezone`` setting string."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rosemary.core.timezone import resolve_timezone


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "garbage", "Not/AZone", "America/Atlantis"],
)
def test_resolve_timezone_falls_back_to_utc(value):
    assert resolve_timezone(value) is UTC


@pytest.mark.parametrize(
    "value",
    ["UTC", "utc", "GMT", "Z"],
)
def test_resolve_timezone_utc_names(value):
    assert resolve_timezone(value) is UTC


@pytest.mark.parametrize(
    ("value", "expected_offset"),
    [
        ("UTC-3", timedelta(hours=-3)),
        ("utc-3", timedelta(hours=-3)),
        ("GMT+2", timedelta(hours=2)),
        ("UTC+05:30", timedelta(hours=5, minutes=30)),
        ("UTC+0530", timedelta(hours=5, minutes=30)),
        ("-03:00", timedelta(hours=-3)),
        ("+02:00", timedelta(hours=2)),
        ("-0430", timedelta(hours=-4, minutes=-30)),
    ],
)
def test_resolve_timezone_offsets(value, expected_offset):
    assert resolve_timezone(value).utcoffset(datetime.now(UTC)) == expected_offset


def test_resolve_timezone_iana_name():
    tz = resolve_timezone("America/Sao_Paulo")
    assert tz.utcoffset(datetime(2026, 8, 1, tzinfo=UTC)) == timedelta(hours=-3)
