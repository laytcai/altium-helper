from datetime import datetime, timedelta, timezone

import pytest

from altium_helper.timeparse import parse_when

NOW = datetime(2026, 9, 30, 15, 30, tzinfo=timezone(timedelta(hours=-4)))


@pytest.mark.parametrize(
    "text, expected",
    [
        ("now", NOW),
        ("today", NOW.replace(hour=0, minute=0)),
        ("yesterday", datetime(2026, 9, 29, tzinfo=NOW.tzinfo)),
        ("last week", datetime(2026, 9, 23, tzinfo=NOW.tzinfo)),
        ("3 days ago", NOW - timedelta(days=3)),
        ("2 hours ago", NOW - timedelta(hours=2)),
        ("2026-09-29T08:00:00Z", datetime(2026, 9, 29, 8, tzinfo=timezone.utc)),
    ],
)
def test_phrases_and_iso_times(text, expected):
    assert parse_when(text, now=NOW) == expected


def test_a_date_without_zone_is_local_time():
    assert parse_when("2026-09-29").tzinfo is not None


def test_nonsense_is_rejected():
    with pytest.raises(ValueError, match="Can't read the time"):
        parse_when("sometime")
