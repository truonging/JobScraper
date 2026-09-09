"""Tests for the Phase 1 acquisition contracts."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from job_matcher.models import (
    AcquiredJob,
    ContractValidationError,
    NormalizedJob,
    RawSourceRecord,
    SourceJobKey,
)

RETRIEVED_AT = datetime(2026, 9, 8, 12, 30, tzinfo=UTC)


def make_key(**changes: str) -> SourceJobKey:
    values = {
        "source": "lever",
        "source_scope": "example",
        "source_job_id": "job-123",
    }
    values.update(changes)
    return SourceJobKey(**values)


def make_normalized(**changes: object) -> NormalizedJob:
    values = {
        "key": make_key(),
        "company": "Example Company",
        "title": "Software Engineer",
        "description": "Build reliable software.",
        "location": "Remote",
        "job_url": "https://jobs.example.com/job-123",
        "apply_url": "https://jobs.example.com/job-123/apply",
        "posted_at": datetime(2026, 9, 1, 8, 0, tzinfo=timezone(timedelta(hours=-7))),
        "retrieved_at": RETRIEVED_AT,
    }
    values.update(changes)
    return NormalizedJob(**values)  # type: ignore[arg-type]


def make_raw(**changes: object) -> RawSourceRecord:
    values = {
        "key": make_key(),
        "retrieved_at": RETRIEVED_AT,
        "payload_json": '{"id":"job-123","text":"Software Engineer"}',
    }
    values.update(changes)
    return RawSourceRecord(**values)  # type: ignore[arg-type]


def test_acquired_job_accepts_complete_matching_records() -> None:
    acquired = AcquiredJob(normalized=make_normalized(), raw=make_raw())

    assert acquired.normalized.key.source_job_id == "job-123"
    assert acquired.raw.payload_json.startswith("{")


def test_normalized_job_accepts_optional_values_as_none() -> None:
    job = make_normalized(location=None, apply_url=None, posted_at=None)

    assert job.location is None
    assert job.apply_url is None
    assert job.posted_at is None


@pytest.mark.parametrize("field", ["source", "source_scope", "source_job_id"])
def test_source_job_key_rejects_blank_fields(field: str) -> None:
    with pytest.raises(ContractValidationError, match=field):
        make_key(**{field: "  "})


@pytest.mark.parametrize("field", ["company", "title", "description", "location"])
def test_normalized_job_rejects_blank_text(field: str) -> None:
    with pytest.raises(ContractValidationError, match=field):
        make_normalized(**{field: "  "})


@pytest.mark.parametrize("field", ["job_url", "apply_url"])
@pytest.mark.parametrize("value", ["ftp://example.com/job", "not-a-url", ""])
def test_normalized_job_rejects_invalid_web_urls(field: str, value: str) -> None:
    with pytest.raises(ContractValidationError, match=field):
        make_normalized(**{field: value})


def test_normalized_job_rejects_naive_retrieved_at() -> None:
    with pytest.raises(ContractValidationError, match="timezone-aware"):
        make_normalized(retrieved_at=datetime(2026, 9, 8, 12, 30))


def test_normalized_job_rejects_non_utc_retrieved_at() -> None:
    non_utc = datetime(2026, 9, 8, 12, 30, tzinfo=timezone(timedelta(hours=-7)))

    with pytest.raises(ContractValidationError, match="datetime.UTC"):
        make_normalized(retrieved_at=non_utc)


def test_normalized_job_rejects_naive_posted_at() -> None:
    with pytest.raises(ContractValidationError, match="timezone-aware"):
        make_normalized(posted_at=datetime(2026, 9, 1, 8, 0))


@pytest.mark.parametrize("payload_json", ["", "not json", "[]", '"value"', "{}"])
def test_raw_source_record_rejects_invalid_payload(payload_json: str) -> None:
    with pytest.raises(ContractValidationError, match="payload_json"):
        make_raw(payload_json=payload_json)


def test_acquired_job_rejects_mismatched_keys() -> None:
    raw = make_raw(key=make_key(source_job_id="another-job"))

    with pytest.raises(ContractValidationError, match="same source job key"):
        AcquiredJob(normalized=make_normalized(), raw=raw)


def test_acquired_job_rejects_mismatched_retrieval_times() -> None:
    raw = make_raw(retrieved_at=datetime(2026, 9, 8, 12, 31, tzinfo=UTC))

    with pytest.raises(ContractValidationError, match="same retrieved_at"):
        AcquiredJob(normalized=make_normalized(), raw=raw)


def test_contracts_are_frozen() -> None:
    key = make_key()

    with pytest.raises(FrozenInstanceError):
        key.source_job_id = "replacement"  # type: ignore[misc]
