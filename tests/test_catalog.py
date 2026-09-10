"""Tests for Phase 2 catalog contracts."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from job_matcher.catalog import (
    LogicalJobId,
    SourceJobLifecycle,
    SourceJobLink,
    SourceJobStatus,
)
from job_matcher.models import ContractValidationError, SourceJobKey

FIRST_SEEN = datetime(2026, 9, 1, tzinfo=UTC)
LAST_SEEN = datetime(2026, 9, 8, tzinfo=UTC)
INACTIVE_AT = datetime(2026, 9, 9, tzinfo=UTC)
KEY = SourceJobKey("lever", "example", "job-123")


def test_active_lifecycle_has_no_inactive_time() -> None:
    lifecycle = SourceJobLifecycle(KEY, SourceJobStatus.ACTIVE, FIRST_SEEN, LAST_SEEN)

    assert lifecycle.inactive_at is None


def test_inactive_lifecycle_records_when_absence_was_observed() -> None:
    lifecycle = SourceJobLifecycle(
        KEY,
        SourceJobStatus.INACTIVE,
        FIRST_SEEN,
        LAST_SEEN,
        INACTIVE_AT,
    )

    assert lifecycle.inactive_at == INACTIVE_AT


@pytest.mark.parametrize(
    ("status", "inactive_at", "message"),
    [
        (SourceJobStatus.ACTIVE, INACTIVE_AT, "must not have inactive_at"),
        (SourceJobStatus.INACTIVE, None, "must have inactive_at"),
        (
            SourceJobStatus.INACTIVE,
            datetime(2026, 9, 7, tzinfo=UTC),
            "must not precede last_seen_at",
        ),
    ],
)
def test_lifecycle_rejects_inconsistent_status_and_times(
    status: SourceJobStatus, inactive_at: datetime | None, message: str
) -> None:
    with pytest.raises(ContractValidationError, match=message):
        SourceJobLifecycle(KEY, status, FIRST_SEEN, LAST_SEEN, inactive_at)


def test_lifecycle_rejects_first_seen_after_last_seen() -> None:
    with pytest.raises(ContractValidationError, match="first_seen_at"):
        SourceJobLifecycle(KEY, SourceJobStatus.ACTIVE, INACTIVE_AT, LAST_SEEN)


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 9, 1),
        datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=-7))),
    ],
)
def test_lifecycle_requires_canonical_utc(timestamp: datetime) -> None:
    with pytest.raises(ContractValidationError):
        SourceJobLifecycle(KEY, SourceJobStatus.ACTIVE, timestamp, LAST_SEEN)


def test_inactive_lifecycle_requires_canonical_utc_inactive_time() -> None:
    non_utc = datetime(2026, 9, 9, tzinfo=timezone(timedelta(hours=-7)))

    with pytest.raises(ContractValidationError, match="datetime.UTC"):
        SourceJobLifecycle(
            KEY,
            SourceJobStatus.INACTIVE,
            FIRST_SEEN,
            LAST_SEEN,
            non_utc,
        )


def test_logical_job_id_wraps_uuid_and_is_frozen() -> None:
    logical_id = LogicalJobId(uuid4())

    with pytest.raises(FrozenInstanceError):
        logical_id.value = uuid4()  # type: ignore[misc]


def test_logical_job_id_rejects_non_uuid() -> None:
    with pytest.raises(ContractValidationError, match="UUID"):
        LogicalJobId("not-a-uuid")  # type: ignore[arg-type]


def test_source_job_link_records_stable_ids_and_utc_link_time() -> None:
    logical_id = LogicalJobId(UUID("12345678-1234-5678-1234-567812345678"))

    link = SourceJobLink(KEY, logical_id, LAST_SEEN)

    assert link.source_key == KEY
    assert link.logical_job_id == logical_id


def test_source_job_link_rejects_non_utc_time() -> None:
    with pytest.raises(ContractValidationError, match="datetime.UTC"):
        SourceJobLink(
            KEY,
            LogicalJobId(uuid4()),
            datetime(2026, 9, 8, tzinfo=timezone(timedelta(hours=-7))),
        )
