"""Tests for acquisition application orchestration."""

from collections.abc import Sequence

import pytest

from job_matcher.acquisition import acquire_and_persist
from job_matcher.models import AcquiredJob
from job_matcher.ports import PersistenceError, SourceAcquisitionError


class RecordingSource:
    def __init__(
        self,
        jobs: Sequence[AcquiredJob] = (),
        *,
        error: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.jobs = jobs
        self.error = error
        self.events = events if events is not None else []

    def fetch_jobs(self) -> Sequence[AcquiredJob]:
        self.events.append("fetch")
        if self.error is not None:
            raise self.error
        return self.jobs


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
    jobs = (object(), object())
    source = RecordingSource(jobs, events=events)  # type: ignore[arg-type]
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
