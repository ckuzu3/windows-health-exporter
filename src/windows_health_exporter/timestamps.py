"""Parsing the timestamps Windows hands back.

Two formats arrive from the collectors and they do not agree with each other:

- `ToString('o')` writes an ISO 8601 value with a zone, such as
  `2026-10-23T05:59:40.5000000Z`.
- The Windows Update registry key writes local time with no zone at all, such as
  `2026-10-22 22:00:00`.

`datetime.fromisoformat` reads both since Python 3.11, including the seven-digit fraction
and the trailing `Z`, so there is no parsing trick here. What this module adds is the
decision about the second format, made once instead of in three collectors: a value
without a zone is read as local time, because that is what Windows wrote, and everything
leaves as UTC so an age never depends on where the client stands.
"""

from __future__ import annotations

from datetime import UTC, datetime


class TimestampError(ValueError):
    """Raised when a value cannot be read as a point in time."""


def parse_windows_timestamp(value: str) -> datetime:
    """Parse a Windows timestamp into an aware UTC datetime.

    Raises:
        TimestampError: if the value is empty or not a timestamp.
    """
    text = value.strip()
    if not text:
        raise TimestampError("An empty string is not a timestamp.")

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise TimestampError(f"'{value}' is not a timestamp this exporter understands.") from error

    if parsed.tzinfo is None:
        parsed = parsed.astimezone()

    return parsed.astimezone(UTC)


def to_unix_seconds(value: str) -> float:
    """Parse a Windows timestamp and return it as a Unix timestamp.

    Prometheus has no date type, so a point in time is exported as seconds since the
    epoch and turned back into a date at query time.
    """
    return parse_windows_timestamp(value).timestamp()
