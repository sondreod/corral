"""Durations and a small five-field cron parser (local time)."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")


def parse_duration(value) -> float:
    """Seconds from a number or a string such as ``"30s"``, ``"5m"``, ``"1h"``."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str) and (m := _DURATION_RE.match(value)):
        seconds = float(m.group(1)) * _UNITS[m.group(2) or "s"]
    else:
        raise ValueError(f"invalid duration {value!r} (use e.g. 30, \"30s\", \"5m\", \"1h\")")
    if not math.isfinite(seconds):
        raise ValueError("duration must be a finite number")
    if seconds <= 0:
        raise ValueError("duration must be positive")
    return seconds


# (name, lowest, highest)
_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
)


def _parse_field(text: str, name: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        rng, _, step_text = part.partition("/")
        try:
            step = int(step_text) if step_text else 1
            if rng == "*":
                start, end = lo, hi
            elif "-" in rng:
                a, b = rng.split("-", 1)
                start, end = int(a), int(b)
            else:
                start = int(rng)
                end = hi if step_text else start
        except ValueError:
            raise ValueError(f"invalid cron {name} field {text!r}") from None
        if step < 1 or not (lo <= start <= end <= hi):
            raise ValueError(f"cron {name} field {text!r} out of range {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return values


class Cron:
    """``minute hour day-of-month month day-of-week``.

    Supports ``*``, lists, ranges and steps. Day of week is 0-7 where both 0
    and 7 are Sunday. As in classic cron, when both day fields are restricted
    a day matching either one qualifies.
    """

    def __init__(self, expression: str):
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError(f"cron expression {expression!r} must have 5 fields")
        self.expression = " ".join(fields)
        sets = [_parse_field(f, *spec) for f, spec in zip(fields, _FIELDS)]
        self.minutes, self.hours, self.days, self.months, dow = sets
        self.weekdays = {d % 7 for d in dow}
        self._dom_any = fields[2].startswith("*")
        self._dow_any = fields[4].startswith("*")

    def _day_matches(self, dt: datetime) -> bool:
        if dt.month not in self.months:
            return False
        dom = dt.day in self.days
        dow = dt.isoweekday() % 7 in self.weekdays
        if self._dom_any or self._dow_any:
            return dom and dow
        return dom or dow

    def next_after(self, after: datetime) -> datetime:
        """First matching minute strictly after ``after``."""
        dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = dt + timedelta(days=366 * 5)
        while dt < limit:
            if not self._day_matches(dt):
                dt = dt.replace(hour=0, minute=0) + timedelta(days=1)
            elif dt.hour not in self.hours:
                dt = dt.replace(minute=0) + timedelta(hours=1)
            elif dt.minute not in self.minutes:
                dt += timedelta(minutes=1)
            else:
                return dt
        raise ValueError(f"cron expression {self.expression!r} never matches")
