"""Timestamps: INTEGER microseconds since the Unix epoch (UTC) <-> RFC 3339 strings."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from .errors import invalid

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ONE_MICROSECOND = timedelta(microseconds=1)
MICROS_PER_DAY = 86_400 * 1_000_000

_RFC3339 = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?([Zz]|[+-]\d{2}:\d{2})?"
)


def parse_rfc3339(text: str) -> int:
    """Parse RFC 3339 into UTC microseconds. Offsets convert; no offset or >6 fraction digits is invalid."""
    if not isinstance(text, str):
        raise invalid("A timestamp must be an RFC 3339 string.")
    match = _RFC3339.fullmatch(text)
    if match is None:
        raise invalid("A timestamp must be RFC 3339, e.g. 2026-11-25T09:30:00.000000Z.")
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    if offset is None:
        raise invalid("A timestamp needs an offset or Z.")
    if fraction is not None and len(fraction) > 6:
        raise invalid("A timestamp may have at most six fractional digits.")
    micros = int((fraction or "").ljust(6, "0"))
    if offset in ("Z", "z"):
        tz = timezone.utc
    else:
        sign = 1 if offset[0] == "+" else -1
        hours, minutes = int(offset[1:3]), int(offset[4:6])
        if hours > 23 or minutes > 59:
            raise invalid("A timestamp offset is out of range.")
        tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), micros, tzinfo=tz)
    except ValueError:
        raise invalid("A timestamp is out of range.") from None
    return (moment - EPOCH) // ONE_MICROSECOND


def format_rfc3339(micros: int) -> str:
    """Format UTC microseconds as RFC 3339 with exactly six fractional digits and Z."""
    moment = EPOCH + timedelta(microseconds=micros)
    return (f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T"
            f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}.{moment.microsecond:06d}Z")


def coerce(value) -> int | None:
    """Accept None, integer microseconds, or an RFC 3339 string."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise invalid("A timestamp must be an RFC 3339 string.")
    if isinstance(value, int):
        return value
    return parse_rfc3339(value)
