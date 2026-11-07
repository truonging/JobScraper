"""Structured metadata parsing, source provenance and live adapter parity."""

import json
from datetime import UTC, datetime

import pytest

from job_matcher.job_metadata import extract_metadata
from job_matcher.sources.greenhouse import GreenhouseJobSource
from job_matcher.sources.lever import LeverJobSource

NOW = datetime(2026, 11, 6, tzinfo=UTC)


@pytest.mark.parametrize(
    "value,expected",
    [
        (" Full-TIME ", "full_time"),
        ("Part time", "part_time"),
        ("Intern", "internship"),
        ("Internship", "internship"),
        ("CONTRACT", "contract"),
        ("Seasonal", "temporary"),
        ("Full Time/Part Time", None),
        ("Remote", None),
        (None, None),
        (5, None),
    ],
)
def test_employment(value, expected):
    assert (
        extract_metadata("lever", {"categories": {"commitment": value}}, NOW)[
            "employment_type"
        ]
        == expected
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        (" Remote ", "remote"),
        ("HYBRID", "hybrid"),
        ("On Site", "onsite"),
        ("on-site", "onsite"),
        ("unspecified", "unknown"),
        ("Mars", None),
        (None, None),
    ],
)
def test_workplace(value, expected):
    assert (
        extract_metadata("lever", {"workplaceType": value}, NOW)["workplace_type"]
        == expected
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        0,
        True,
        "1700000000000",
        1700000000,
        -1,
        float("nan"),
        float("inf"),
        999999999999999999999,
        {},
        [],
    ],
)
def test_invalid_milliseconds(value):
    assert extract_metadata("lever", {"createdAt": value}, NOW)["posted_at"] is None


def test_valid_milliseconds_and_future():
    expected = datetime(2026, 1, 1, 0, 0, 0, 123000, tzinfo=UTC)
    assert (
        extract_metadata("lever", {"createdAt": int(expected.timestamp() * 1000)}, NOW)[
            "posted_at"
        ]
        == expected
    )
    assert (
        extract_metadata("lever", {"createdAt": 2000000000000}, NOW)["posted_at"]
        is None
    )


@pytest.mark.parametrize(
    "value", [None, "bad", "2026-01-01", "", 123, "2099-01-01T00:00:00Z"]
)
def test_greenhouse_no_update_fallback(value):
    result = extract_metadata(
        "greenhouse",
        {
            "first_published": value,
            "updated_at": "2026-01-01T00:00:00Z",
            "employment": "employment_required",
            "location": {"name": "Remote"},
        },
        NOW,
    )
    assert all(v is None for v in result.values())


def test_greenhouse_structured_values_and_conflict():
    payload = {
        "metadata": [
            {"name": "Employment Type", "value": "Full-time"},
            {"name": "Workplace Type", "value": "Hybrid"},
        ],
        "first_published": "2026-01-01T02:00:00+02:00",
    }
    result = extract_metadata("greenhouse", payload, NOW)
    assert result == dict(
        employment_type="full_time",
        workplace_type="hybrid",
        posted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    payload["metadata"].append({"name": "Workplace", "value": "Remote"})
    assert extract_metadata("greenhouse", payload, NOW)["workplace_type"] is None


@pytest.mark.parametrize("provider", ["lever", "greenhouse"])
def test_live_adapter_uses_shared_extraction(provider):
    if provider == "lever":
        payload = dict(
            id="1",
            text="Engineer",
            hostedUrl="https://example.com/1",
            categories={"commitment": "Full-time"},
            workplaceType="remote",
            createdAt=1767225600000,
        )
        job = LeverJobSource("example", "Example")._normalize(
            payload, source_job_id="1", retrieved_at=NOW
        )
    else:
        payload = dict(
            id=1,
            title="Engineer",
            absolute_url="https://example.com/1",
            first_published="2026-01-01T00:00:00Z",
            metadata=[{"name": "Workplace Type", "value": "Remote"}],
        )
        job = GreenhouseJobSource("example", "Example")._normalize(payload, "1", NOW)
    for field, value in extract_metadata(provider, payload, NOW).items():
        assert getattr(job.normalized, field) == value
    assert json.loads(job.raw.payload_json) == payload
