"""Parsing the two timestamp formats the collectors receive."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from windows_health_exporter.timestamps import (
    TimestampError,
    parse_windows_timestamp,
    to_unix_seconds,
)


def test_the_seven_digit_fraction_powershell_writes() -> None:
    """ToString('o') writes seven fractional digits, which is more than a datetime holds."""
    parsed = parse_windows_timestamp("2026-10-23T05:59:40.5000000Z")

    assert parsed == datetime(2026, 10, 23, 5, 59, 40, 500000, tzinfo=UTC)


def test_a_trailing_z_is_understood_as_utc() -> None:
    assert parse_windows_timestamp("2026-10-23T05:59:40Z").tzinfo is UTC


def test_an_offset_is_converted_rather_than_relabelled() -> None:
    parsed = parse_windows_timestamp("2026-10-23T07:59:40+02:00")

    assert parsed == datetime(2026, 10, 23, 5, 59, 40, tzinfo=UTC)


def test_the_registry_format_without_a_zone_is_read_as_local_time() -> None:
    """The Windows Update key stores local time with no zone, so that is the assumption."""
    naive = "2026-10-22 22:00:00"
    expected = datetime(2026, 10, 22, 22, 0, 0).astimezone().astimezone(UTC)

    assert parse_windows_timestamp(naive) == expected


def test_a_parsed_value_always_leaves_as_utc() -> None:
    """An age must not depend on the time zone the client happens to stand in."""
    for value in ("2026-10-23T05:59:40Z", "2026-10-23T07:59:40+02:00", "2026-10-22 22:00:00"):
        assert parse_windows_timestamp(value).tzinfo is UTC


@pytest.mark.parametrize("value", ["", "   ", "never", "2026-13-45T99:99:99Z", "KB5121794"])
def test_something_that_is_not_a_timestamp_is_rejected(value: str) -> None:
    with pytest.raises(TimestampError):
        parse_windows_timestamp(value)


def test_the_error_names_the_offending_value() -> None:
    with pytest.raises(TimestampError, match="KB5121794"):
        parse_windows_timestamp("KB5121794")


def test_to_unix_seconds_returns_a_number_prometheus_can_hold() -> None:
    seconds = to_unix_seconds("2026-10-23T05:59:40Z")

    assert seconds == pytest.approx(1792735180.0)
    assert isinstance(seconds, float)
