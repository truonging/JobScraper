"""PST timestamp presentation, including date rollover and 12-hour boundaries."""

import pytest

from job_matcher.dashboard_display import format_pst_timestamp


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-04T07:42:16.209540+00:00", "2026-11-03 11:42 PM"),
        ("2026-11-04T08:00:00Z", "2026-11-04 12:00 AM"),
        ("2026-11-04T20:05:00+00:00", "2026-11-04 12:05 PM"),
        ("2026-07-04T08:05:00+00:00", "2026-07-04 12:05 AM"),
        (None, "Unavailable"),
        ("invalid", "Unavailable"),
        ("2026-11-04", "Unavailable"),
    ],
)
def test_pst_timestamp(value, expected):
    assert format_pst_timestamp(value) == expected


def test_posted_date_uses_utc():
    from job_matcher.dashboard_display import format_posted_date

    assert format_posted_date("2026-01-01T23:30:00-08:00") == "2026-01-02"
    assert format_posted_date(None) == "Unknown"
    assert format_posted_date("bad") == "Unknown"
    assert format_posted_date("2026-01-01") == "Unknown"
