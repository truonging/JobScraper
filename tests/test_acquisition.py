"""Tests for acquisition application orchestration."""

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from job_matcher.acquisition import acquire_and_persist
from job_matcher.models import (
    AcquiredJob,
    NormalizedJob,
    RawSourceRecord,
    SourceJobKey,
    SourceSnapshot,
)
from job_matcher.ports import PersistenceError, SourceAcquisitionError

RETRIEVED_AT = datetime(2026, 9, 8, 12, 30, tzinfo=UTC)


def acquired_job(source_job_id: str) -> AcquiredJob:
    key = SourceJobKey("test", "example", source_job_id)
    return AcquiredJob(
        normalized=NormalizedJob(
            key=key,
            company="Example Company",
            title="Software Engineer",
            description="Build software.",
            job_url=f"https://jobs.example/{source_job_id}",
            retrieved_at=RETRIEVED_AT,
        ),
        raw=RawSourceRecord(
            key=key,
            retrieved_at=RETRIEVED_AT,
            payload_json=f'{{"id":"{source_job_id}"}}',
        ),
    )


class RecordingSource:
    def __init__(
        self,
        jobs: Sequence[AcquiredJob] = (),
        *,
        error: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.snapshot = SourceSnapshot(
            source="test",
            source_scope="example",
            retrieved_at=RETRIEVED_AT,
            jobs=tuple(jobs),
        )
        self.error = error
        self.events = events if events is not None else []

    def fetch_snapshot(self) -> SourceSnapshot:
        self.events.append("fetch")
        if self.error is not None:
            raise self.error
        return self.snapshot


class RecordingRepository:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.error = error
        self.events = events if events is not None else []
        self.batches: list[Sequence[AcquiredJob]] = []

    def upsert_batch(self, jobs: Sequence[AcquiredJob]) -> None:
        self.events.append("persist")
        self.batches.append(jobs)
        if self.error is not None:
            raise self.error

    def get(self, key: object) -> AcquiredJob | None:
        del key
        return None


def test_acquire_and_persist_stores_empty_successful_batch() -> None:
    source = RecordingSource()
    repository = RecordingRepository()

    count = acquire_and_persist(source, repository)

    assert count == 0
    assert repository.batches == [()]


def test_acquire_and_persist_fetches_before_storing_complete_batch() -> None:
    events: list[str] = []
    jobs = (acquired_job("job-1"), acquired_job("job-2"))
    source = RecordingSource(jobs, events=events)
    repository = RecordingRepository(events=events)

    count = acquire_and_persist(source, repository)

    assert count == 2
    assert events == ["fetch", "persist"]
    assert repository.batches == [jobs]


def test_acquisition_failure_prevents_persistence() -> None:
    source = RecordingSource(error=SourceAcquisitionError("source failed"))
    repository = RecordingRepository()

    with pytest.raises(SourceAcquisitionError, match="source failed"):
        acquire_and_persist(source, repository)

    assert repository.batches == []


def test_persistence_failure_remains_visible() -> None:
    source = RecordingSource()
    repository = RecordingRepository(error=PersistenceError("storage failed"))

    with pytest.raises(PersistenceError, match="storage failed"):
        acquire_and_persist(source, repository)
