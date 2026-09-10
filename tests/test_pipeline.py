"""Tests for complete Phase 2 pipeline orchestration."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from job_matcher.catalog import LinkedSourceJob, LogicalJobId, SourceJobLink
from job_matcher.filter_policy import (
    CompanyFilterRules,
    FilterPolicy,
    TextFilterRules,
)
from job_matcher.filtering import DeterministicFilterEvaluator
from job_matcher.identity import IdentityResolver
from job_matcher.models import (
    AcquiredJob,
    NormalizedJob,
    RawSourceRecord,
    SourceJobKey,
    SourceSnapshot,
)
from job_matcher.pipeline import run_job_pipeline
from job_matcher.ports import PersistenceError, SourceAcquisitionError

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
LOGICAL_UUID = UUID("00000000-0000-4000-8000-000000000001")


def make_snapshot(source: str = "lever", *, jobs: int = 1) -> SourceSnapshot:
    acquired: list[AcquiredJob] = []
    for number in range(jobs):
        key = SourceJobKey(source, "example", f"job-{number}")
        acquired.append(
            AcquiredJob(
                normalized=NormalizedJob(
                    key=key,
                    company="Example Company",
                    title="Software Engineer",
                    location="Remote",
                    description="Build Python services.",
                    job_url=f"https://jobs.example/{number}",
                    retrieved_at=NOW,
                ),
                raw=RawSourceRecord(
                    key=key,
                    retrieved_at=NOW,
                    payload_json=f'{{"id":"job-{number}"}}',
                ),
            )
        )
    return SourceSnapshot(source, "example", NOW, tuple(acquired))


def make_policy(*, required_description: str = "python") -> FilterPolicy:
    return FilterPolicy(
        version=1,
        title=TextFilterRules(),
        location=TextFilterRules(),
        company=CompanyFilterRules(),
        description=TextFilterRules(include_any=(required_description,)),
        fingerprint="0" * 64,
    )


class RecordingSource:
    def __init__(
        self,
        snapshot: SourceSnapshot,
        events: list[str],
        *,
        error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.events = events
        self.error = error

    def fetch_snapshot(self) -> SourceSnapshot:
        self.events.append("fetch")
        if self.error is not None:
            raise self.error
        return self.snapshot


class MemoryPipelineRepository:
    def __init__(
        self,
        events: list[str],
        *,
        persist_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.persist_error = persist_error
        self.jobs: dict[SourceJobKey, NormalizedJob] = {}
        self.links: dict[SourceJobKey, SourceJobLink] = {}

    def reconcile_snapshot(self, snapshot: SourceSnapshot) -> None:
        self.events.append("persist")
        if self.persist_error is not None:
            raise self.persist_error
        self.jobs.update((job.normalized.key, job.normalized) for job in snapshot.jobs)

    def list_unlinked_jobs(self) -> tuple[NormalizedJob, ...]:
        self.events.append("list_unlinked")
        return tuple(job for key, job in self.jobs.items() if key not in self.links)

    def list_linked_jobs(self) -> tuple[LinkedSourceJob, ...]:
        self.events.append("list_linked")
        return tuple(
            LinkedSourceJob(self.jobs[key], link) for key, link in self.links.items()
        )

    def assign_logical_job(
        self,
        source_key: SourceJobKey,
        proposed_logical_job_id: LogicalJobId,
        linked_at: datetime,
    ) -> SourceJobLink:
        self.events.append("assign")
        return self.links.setdefault(
            source_key,
            SourceJobLink(source_key, proposed_logical_job_id, linked_at),
        )

    def get_link(self, source_key: SourceJobKey) -> SourceJobLink | None:
        return self.links.get(source_key)

    def get_logical_job_activity(self, logical_job_id: LogicalJobId) -> bool | None:
        return (
            any(link.logical_job_id == logical_job_id for link in self.links.values())
            or None
        )

    def list_active_linked_jobs(self) -> tuple[LinkedSourceJob, ...]:
        self.events.append("list_active")
        return tuple(
            LinkedSourceJob(self.jobs[key], link) for key, link in self.links.items()
        )


def run(
    source: RecordingSource,
    repository: MemoryPipelineRepository,
    policy: FilterPolicy | None = None,
):
    return run_job_pipeline(
        source,
        repository,
        IdentityResolver(
            repository,
            uuid_factory=lambda: LOGICAL_UUID,
            clock=lambda: NOW,
        ),
        DeterministicFilterEvaluator(policy or make_policy(), clock=lambda: NOW),
    )


@pytest.mark.parametrize("source_name", ["lever", "ashby", "greenhouse"])
def test_pipeline_runs_source_independent_stages_in_order(source_name: str) -> None:
    events: list[str] = []
    repository = MemoryPipelineRepository(events)
    source = RecordingSource(make_snapshot(source_name), events)

    result = run(source, repository)

    assert events == [
        "fetch",
        "persist",
        "list_unlinked",
        "list_linked",
        "assign",
        "list_active",
    ]
    assert result.acquired_count == 1
    assert result.resolved_count == 1
    assert result.evaluated_count == 1
    assert result.eligible_count == 1
    assert result.filter_decisions[0].source_key.source == source_name


def test_pipeline_successfully_handles_an_empty_snapshot() -> None:
    events: list[str] = []
    repository = MemoryPipelineRepository(events)
    source = RecordingSource(make_snapshot(jobs=0), events)

    result = run(source, repository)

    assert result.acquired_count == 0
    assert result.resolved_count == 0
    assert result.evaluated_count == 0
    assert result.eligible_count == 0


def test_pipeline_reports_catalog_counts_separately_from_snapshot_count() -> None:
    events: list[str] = []
    repository = MemoryPipelineRepository(events)
    first_source = RecordingSource(make_snapshot("ashby", jobs=1), events)
    run(first_source, repository)
    events.clear()
    second_source = RecordingSource(make_snapshot("greenhouse", jobs=1), events)

    result = run(second_source, repository)

    assert result.acquired_count == 1
    assert result.resolved_count == 1
    assert result.evaluated_count == 2
    assert result.eligible_count == 1


def test_source_failure_prevents_every_later_stage() -> None:
    events: list[str] = []
    repository = MemoryPipelineRepository(events)
    source = RecordingSource(
        make_snapshot(), events, error=SourceAcquisitionError("unavailable")
    )

    with pytest.raises(SourceAcquisitionError, match="unavailable"):
        run(source, repository)

    assert events == ["fetch"]


def test_persistence_failure_prevents_identity_and_filtering() -> None:
    events: list[str] = []
    repository = MemoryPipelineRepository(
        events, persist_error=PersistenceError("storage failed")
    )
    source = RecordingSource(make_snapshot(), events)

    with pytest.raises(PersistenceError, match="storage failed"):
        run(source, repository)

    assert events == ["fetch", "persist"]


def test_filter_failure_does_not_undo_persistence_or_identity() -> None:
    events: list[str] = []
    repository = MemoryPipelineRepository(events)
    source = RecordingSource(make_snapshot(), events)

    class FailingEvaluator:
        def evaluate(self, jobs: object) -> None:
            del jobs
            raise RuntimeError("filter failed")

    with pytest.raises(RuntimeError, match="filter failed"):
        run_job_pipeline(
            source,
            repository,
            IdentityResolver(
                repository,
                uuid_factory=lambda: LOGICAL_UUID,
                clock=lambda: NOW,
            ),
            FailingEvaluator(),  # type: ignore[arg-type]
        )

    assert len(repository.jobs) == 1
    assert len(repository.links) == 1
    assert events[-1] == "list_active"
