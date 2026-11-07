"""Tests for the IDE-runnable multi-company Phase 2 validation runner."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_matcher import validation
from job_matcher.catalog import SourceJobStatus
from job_matcher.filter_policy import (
    CompanyFilterRules,
    FilterPolicy,
    TextFilterRules,
)
from job_matcher.filtering import DeterministicFilterEvaluator
from job_matcher.models import (
    AcquiredJob,
    NormalizedJob,
    RawSourceRecord,
    SourceJobKey,
    SourceSnapshot,
)
from job_matcher.ports import SourceAcquisitionError
from job_matcher.sqlite_repository import SQLiteJobRepository
from scripts import run_phase2_validation as runner

T0 = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 9, 13, 0, tzinfo=UTC)


def make_job(
    site: str,
    source_job_id: str,
    *,
    title: str = "Python Engineer",
    retrieved_at: datetime = T0,
) -> AcquiredJob:
    key = SourceJobKey("lever", site, source_job_id)
    return AcquiredJob(
        normalized=NormalizedJob(
            key=key,
            company=f"{site.title()} Company",
            title=title,
            description="Build Python services.",
            location="Remote",
            job_url=f"https://jobs.lever.co/{site}/{source_job_id}",
            retrieved_at=retrieved_at,
        ),
        raw=RawSourceRecord(
            key=key,
            retrieved_at=retrieved_at,
            payload_json=json.dumps({"id": source_job_id, "text": title}),
        ),
    )


def make_snapshot(
    site: str,
    *jobs: AcquiredJob,
    retrieved_at: datetime = T1,
) -> SourceSnapshot:
    return SourceSnapshot("lever", site, retrieved_at, jobs)


def make_policy() -> FilterPolicy:
    return FilterPolicy(
        version=1,
        title=TextFilterRules(exclude_any=("nurse",)),
        location=TextFilterRules(),
        company=CompanyFilterRules(),
        description=TextFilterRules(),
        fingerprint="0" * 64,
    )


class FakeSource:
    def __init__(
        self,
        snapshot: SourceSnapshot | None = None,
        *,
        error: SourceAcquisitionError | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.error = error

    def fetch_snapshot(self) -> SourceSnapshot:
        if self.error is not None:
            raise self.error
        assert self.snapshot is not None
        return self.snapshot


def test_registry_loader_reports_invalid_and_duplicate_entries(tmp_path: Path) -> None:
    registry_path = tmp_path / "lever.json"
    registry_path.write_text(
        json.dumps(
            [
                {"site": " valid-site ", "company": " Valid Company "},
                {"site": "missing-company", "company": None},
                {"site": "valid-site", "company": "Duplicate Company"},
                "not-an-object",
            ]
        ),
        encoding="utf-8",
    )

    registry = runner.load_verified_companies(registry_path)

    assert registry.total_entries == 4
    assert registry.companies == (runner.LeverCompany("valid-site", "Valid Company"),)
    assert [issue.message for issue in registry.issues] == [
        "company must be non-blank",
        "site is duplicated",
        "entry must be a JSON object",
    ]


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("{", "valid UTF-8 JSON"),
        ("{}", "must contain a JSON array"),
    ],
)
def test_registry_loader_rejects_malformed_documents(
    tmp_path: Path, contents: str, message: str
) -> None:
    registry_path = tmp_path / "lever.json"
    registry_path.write_text(contents, encoding="utf-8")

    with pytest.raises(runner.ValidationConfigurationError, match=message):
        runner.load_verified_companies(registry_path)


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(None, 3), (2, 2), (0, 0)],
)
def test_company_limit_is_applied_after_registry_validation(
    limit: int | None, expected: int
) -> None:
    companies = tuple(
        runner.LeverCompany(f"site-{number}", f"Company {number}")
        for number in range(3)
    )

    assert len(runner.select_companies(companies, limit)) == expected


def test_company_limit_rejects_invalid_configuration() -> None:
    with pytest.raises(runner.ValidationConfigurationError, match="COMPANY_LIMIT"):
        runner.select_companies((), -1)


def test_runner_continues_after_source_failure_and_processes_catalog_once(
    tmp_path: Path,
) -> None:
    repository = SQLiteJobRepository(tmp_path / "validation.sqlite3")
    repository.initialize_schema()
    failed_existing = make_job("failed", "old-job")
    empty_existing = make_job("empty", "closed-job")
    repository.reconcile_snapshot(
        make_snapshot("failed", failed_existing, retrieved_at=T0)
    )
    repository.reconcile_snapshot(
        make_snapshot("empty", empty_existing, retrieved_at=T0)
    )

    companies = (
        runner.LeverCompany("live", "Live Company"),
        runner.LeverCompany("failed", "Failed Company"),
        runner.LeverCompany("empty", "Empty Company"),
    )
    sources = {
        "live": FakeSource(
            make_snapshot(
                "live",
                make_job("live", "new-job", title="Nurse", retrieved_at=T1),
            )
        ),
        "failed": FakeSource(error=SourceAcquisitionError("unavailable")),
        "empty": FakeSource(make_snapshot("empty")),
    }
    delays: list[float] = []
    progress: list[str] = []

    result = runner.run_validation(
        companies,
        repository,
        DeterministicFilterEvaluator(make_policy(), clock=lambda: T1),
        source_factory=lambda company: sources[company.site],
        delay_seconds=0.5,
        sleeper=delays.append,
        write_line=progress.append,
    )

    assert result.attempted_count == 3
    assert result.successful_count == 2
    assert result.failed_count == 1
    assert result.total_source_postings_fetched == 1
    assert result.failures[0].company.site == "failed"
    assert delays == [0.5, 0.5]
    assert any("failed: FAILED - unavailable" in line for line in progress)

    failed_lifecycle = repository.get_lifecycle(failed_existing.normalized.key)
    assert failed_lifecycle is not None
    assert failed_lifecycle.status is SourceJobStatus.ACTIVE
    assert failed_lifecycle.last_seen_at == T0
    empty_lifecycle = repository.get_lifecycle(empty_existing.normalized.key)
    assert empty_lifecycle is not None
    assert empty_lifecycle.status is SourceJobStatus.INACTIVE
    assert empty_lifecycle.inactive_at == T1

    assert result.catalog.resolved_count == 3
    assert result.catalog.evaluated_count == 2
    assert result.catalog.eligible_count == 1
    assert len(result.catalog.active_linked_jobs) == 2
    assert len(result.all_linked_jobs) == 3

    registry = runner.RegistryLoadResult(
        total_entries=4,
        companies=companies,
        issues=(runner.RegistryIssue(3, "invalid", "company must be non-blank"),),
    )
    summary: list[str] = []
    runner.print_summary(
        registry,
        result,
        sample_limit=1,
        top_reason_limit=1,
        write_line=summary.append,
    )
    rendered = "\n".join(summary)
    assert "Companies successfully fetched: 2" in rendered
    assert "Companies failed: 1" in rendered
    assert "Active linked source postings: 2" in rendered
    assert "Inactive linked source postings: 1" in rendered
    assert "Eligible logical jobs: 1" in rendered
    assert "Rejected logical jobs: 1" in rendered
    assert "Eligible logical-job percentage: 50.0%" in rendered
    assert "Rejected logical-job percentage: 50.0%" in rendered
    assert "title/exclude_match ('nurse'): 1" in rendered
    assert "Eligible job sample (up to 1)" in rendered
    assert "Rejected job sample (up to 1)" in rendered
    assert "Source postings fetched this run: 1" in rendered
    assert "Current-run acquisition counts" in rendered
    assert "Entire-database catalog counts (including previous runs)" in rendered
    assert (
        "Failed or unattempted scopes can retain previous active postings" in rendered
    )
    assert "job samples use the entire active catalog" in rendered


def test_summary_samples_from_all_jobs_in_each_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SQLiteJobRepository(tmp_path / "validation.sqlite3")
    repository.initialize_schema()
    jobs = (
        make_job("sample", "eligible-first", title="Backend Engineer", retrieved_at=T1),
        make_job("sample", "eligible-last", title="Platform Engineer", retrieved_at=T1),
        make_job("sample", "rejected-first", title="Nurse One", retrieved_at=T1),
        make_job("sample", "rejected-last", title="Nurse Two", retrieved_at=T1),
    )
    result = runner.run_validation(
        (runner.LeverCompany("sample", "Sample Company"),),
        repository,
        DeterministicFilterEvaluator(make_policy(), clock=lambda: T1),
        source_factory=lambda _company: FakeSource(make_snapshot("sample", *jobs)),
        delay_seconds=0,
        write_line=lambda _line: None,
    )
    sampled_population_sizes: list[int] = []

    def choose_last(population: list[object], count: int) -> list[object]:
        sampled_population_sizes.append(len(population))
        return population[-count:]

    monkeypatch.setattr(runner.random, "sample", choose_last)
    summary: list[str] = []

    runner.print_summary(
        runner.RegistryLoadResult(
            total_entries=1,
            companies=(runner.LeverCompany("sample", "Sample Company"),),
            issues=(),
        ),
        result,
        sample_limit=1,
        top_reason_limit=0,
        write_line=summary.append,
    )

    rendered = "\n".join(summary)
    assert sampled_population_sizes == [2, 2]
    assert "Platform Engineer" in rendered
    assert "Nurse Two" in rendered
    assert "Backend Engineer" not in rendered
    assert "Nurse One" not in rendered


@pytest.mark.parametrize("database_existed", [False, True])
def test_main_reports_database_existence_before_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database_existed: bool,
) -> None:
    database_path = tmp_path / "validation.sqlite3"
    if database_existed:
        repository = SQLiteJobRepository(database_path)
        repository.initialize_schema()
        repository.reconcile_snapshot(
            make_snapshot(
                "unattempted", make_job("unattempted", "old"), retrieved_at=T0
            )
        )
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps([{"site": "live", "company": "Live Company"}]), encoding="utf-8"
    )
    monkeypatch.setattr(runner, "DATABASE_PATH", database_path)
    monkeypatch.setattr(runner, "VERIFIED_LEVER_REGISTRY_PATH", registry_path)
    greenhouse_registry = tmp_path / "greenhouse.json"
    greenhouse_registry.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        runner, "VERIFIED_GREENHOUSE_REGISTRY_PATH", greenhouse_registry
    )
    monkeypatch.setattr(runner, "load_filter_policy", lambda _path: make_policy())
    monkeypatch.setattr(
        runner, "_source_factory", lambda _company: FakeSource(make_snapshot("live"))
    )

    assert runner.main() == 0

    output = capsys.readouterr().out
    assert (
        f"Database existed before run: {'yes' if database_existed else 'no'}" in output
    )
    assert "Source postings fetched this run: 0" in output
    assert f"Active linked source postings: {int(database_existed)}" in output
    assert "Entire-database catalog counts (including previous runs)" in output


def test_source_factory_passes_configured_retry_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_source(**kwargs: object) -> FakeSource:
        captured.update(kwargs)
        return FakeSource(make_snapshot("example"))

    monkeypatch.setattr(runner, "LeverJobSource", fake_source)
    monkeypatch.setattr(runner, "MAX_ATTEMPTS", 2)
    monkeypatch.setattr(runner, "BACKOFF_SECONDS", 0.5)
    monkeypatch.setattr(runner, "MAX_RETRY_DELAY_SECONDS", 4.0)

    runner._source_factory(runner.LeverCompany("example", "Example Company"))

    assert captured["timeout_seconds"] == runner.TIMEOUT_SECONDS == 20.0
    assert captured["max_attempts"] == 2
    assert captured["backoff_seconds"] == 0.5
    assert captured["max_retry_delay_seconds"] == 4.0


def greenhouse_snapshot(site: str, *, retrieved_at: datetime = T1) -> SourceSnapshot:
    job = make_job(site, "job-1", retrieved_at=retrieved_at)
    key = replace(job.normalized.key, source="greenhouse")
    job = AcquiredJob(
        replace(
            job.normalized, key=key, job_url=f"https://example.com/greenhouse/{site}"
        ),
        replace(job.raw, key=key),
    )
    return SourceSnapshot("greenhouse", site, retrieved_at, (job,))


@pytest.mark.parametrize("failed_source", [None, "lever", "greenhouse"])
def test_combined_batches_are_ordered_and_process_once_after_both(
    tmp_path, monkeypatch, failed_source
):
    repo = SQLiteJobRepository(tmp_path / "combined.sqlite3")
    repo.initialize_schema()
    # An old Greenhouse scope must survive a failed Greenhouse acquisition and
    # must never be deactivated by the successful Lever snapshot of that name.
    old = greenhouse_snapshot("shared", retrieved_at=T0)
    repo.reconcile_snapshot(old)
    events = []
    actual_process = validation.process_persisted_catalog

    def process(*args):
        events.append("process")
        return actual_process(*args)

    monkeypatch.setattr(validation, "process_persisted_catalog", process)

    def factory(source):
        class Source:
            def fetch_snapshot(self):
                events.append(source)
                if source == failed_source:
                    raise SourceAcquisitionError("temporary failure")
                return (
                    make_snapshot(
                        "shared", make_job("shared", "job-1", retrieved_at=T1)
                    )
                    if source == "lever"
                    else greenhouse_snapshot("shared")
                )

        return lambda _: Source()

    company = runner.LeverCompany("shared", "Shared Company")
    result = validation.run_combined_validation(
        (company,),
        (company,),
        repo,
        DeterministicFilterEvaluator(make_policy(), clock=lambda: T1),
        lever_source_factory=factory("lever"),
        greenhouse_source_factory=factory("greenhouse"),
        delay_seconds=0,
        write_line=lambda _: None,
    )
    assert events == ["lever", "greenhouse", "process"]
    assert result.combined.attempted_count == 2
    assert result.combined.failed_count == int(failed_source is not None)
    assert result.lever.successful_count == int(failed_source != "lever")
    assert result.greenhouse.successful_count == int(failed_source != "greenhouse")
    assert (
        repo.get_lifecycle(old.jobs[0].normalized.key).status is SourceJobStatus.ACTIVE
    )
    if failed_source == "greenhouse":
        assert repo.get_lifecycle(old.jobs[0].normalized.key).last_seen_at == T0
    sources = {
        j.normalized.key.source for j in result.combined.catalog.active_linked_jobs
    }
    assert sources == (
        {"greenhouse"} if failed_source == "lever" else {"lever", "greenhouse"}
    )


def test_main_reuses_lever_catalog_and_processes_entire_greenhouse_registry(
    tmp_path, monkeypatch, capsys
):
    lever_registry = tmp_path / "lever.json"
    greenhouse_registry = tmp_path / "greenhouse.json"
    lever_registry.write_text(
        json.dumps(
            [
                {"site": "lever-one", "company": "Lever One"},
                {"site": "unattempted", "company": "Unattempted"},
            ]
        )
    )
    greenhouse_registry.write_text(
        json.dumps([{"site": f"gh-{i}", "company": f"GH {i}"} for i in range(7)])
    )
    database = tmp_path / "phase2_validation.sqlite3"
    repo = SQLiteJobRepository(database)
    repo.initialize_schema()
    old_job = make_job("unattempted", "old", retrieved_at=T0)
    repo.reconcile_snapshot(make_snapshot("unattempted", old_job, retrieved_at=T0))
    validation.IdentityResolver(repo).resolve_unlinked()
    old_link = repo.get_link(old_job.normalized.key)
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(runner, "DATABASE_PATH", database)
    monkeypatch.setattr(runner, "VERIFIED_LEVER_REGISTRY_PATH", lever_registry)
    monkeypatch.setattr(
        runner, "VERIFIED_GREENHOUSE_REGISTRY_PATH", greenhouse_registry
    )
    monkeypatch.setattr(runner, "COMPANY_LIMIT", 1)
    monkeypatch.setattr(runner, "GREENHOUSE_COMPANY_LIMIT", None)
    monkeypatch.setattr(runner, "DELAY_SECONDS", 0)
    monkeypatch.setattr(runner, "load_filter_policy", lambda _: make_policy())
    events = []

    def lever_factory(company):
        events.append("lever")
        return FakeSource(
            make_snapshot(
                company.site, make_job(company.site, "job-1", retrieved_at=T1)
            )
        )

    def greenhouse_factory(company):
        events.append(company.site)
        return FakeSource(greenhouse_snapshot(company.site))

    monkeypatch.setattr(runner, "_source_factory", lever_factory)
    monkeypatch.setattr(runner, "_greenhouse_source_factory", greenhouse_factory)
    assert runner.main() == 0
    assert events == ["lever", *[f"gh-{i}" for i in range(7)]]
    output = capsys.readouterr().out
    report = json.loads(output[output.index("{") :])
    assert report["acquisition"]["lever"]["attempted"] == 1
    assert report["acquisition"]["greenhouse"]["attempted"] == 7
    assert report["global"]["active_source_postings"] == 9
    assert report["by_source"]["lever"]["active_source_postings"] == 2
    assert report["by_source"]["greenhouse"]["active_source_postings"] == 7
    assert repo.get(old_job.normalized.key) == old_job
    assert repo.get_link(old_job.normalized.key) == old_link
    assert repo.get_lifecycle(old_job.normalized.key).status is SourceJobStatus.ACTIVE
    assert repo.get_lifecycle(old_job.normalized.key).last_seen_at == T0


def test_greenhouse_factory_uses_shared_request_settings(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        runner, "GreenhouseJobSource", lambda **kwargs: captured.update(kwargs)
    )
    runner._greenhouse_source_factory(runner.LeverCompany("example", "Example"))
    assert captured["timeout_seconds"] == runner.TIMEOUT_SECONDS
    assert captured["max_attempts"] == runner.MAX_ATTEMPTS
    assert captured["max_retry_delay_seconds"] == runner.MAX_RETRY_DELAY_SECONDS


def test_combined_default_reuses_catalog_with_one_lever_and_full_greenhouse():
    assert runner.COMPANY_LIMIT == 1
    assert runner.GREENHOUSE_COMPANY_LIMIT is None
    assert runner.DATABASE_PATH.name == "phase2_validation.sqlite3"
