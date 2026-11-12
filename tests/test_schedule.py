from datetime import datetime

import pytest

from scriptrunner.schedule import Cron, parse_duration


@pytest.mark.parametrize(
    "value, seconds",
    [(30, 30), (1.5, 1.5), ("45", 45), ("30s", 30), ("5m", 300), ("1h", 3600), ("2d", 172800)],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "abc", "5w", 0, -1, True, None])
def test_parse_duration_rejects(value):
    with pytest.raises(ValueError):
        parse_duration(value)


@pytest.mark.parametrize(
    "expr, after, expected",
    [
        ("* * * * *", "2026-01-01 10:00:30", "2026-01-01 10:01"),
        ("*/15 * * * *", "2026-01-01 10:15:00", "2026-01-01 10:30"),
        ("30 6 * * *", "2026-01-01 07:00:00", "2026-01-02 06:30"),
        ("0 9 * * 1-5", "2026-01-02 09:00:00", "2026-01-05 09:00"),  # Fri -> Mon
        ("0 0 * * 7", "2026-01-01 00:00:00", "2026-01-04 00:00"),  # 7 is Sunday
        ("0 0 29 2 *", "2026-01-01 00:00:00", "2028-02-29 00:00"),
        ("0 0 13 * 5", "2026-02-01 00:00:00", "2026-02-06 00:00"),  # 13th OR Friday
        ("5,10 8-9 * 3 *", "2026-03-01 09:10:00", "2026-03-02 08:05"),
    ],
)
def test_cron_next_after(expr, after, expected):
    got = Cron(expr).next_after(datetime.fromisoformat(after))
    assert got == datetime.fromisoformat(expected)


@pytest.mark.parametrize("expr", ["* * * *", "60 * * * *", "* 24 * * *", "a * * * *", "*/0 * * * *", "5-1 * * * *"])
def test_cron_rejects(expr):
    with pytest.raises(ValueError):
        Cron(expr)


def test_cron_never_matching():
    with pytest.raises(ValueError):
        Cron("0 0 31 2 *").next_after(datetime(2026, 1, 1))
