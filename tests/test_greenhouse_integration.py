"""Greenhouse screening, source isolation, reporting, and repeat-run safety."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from job_matcher.acquisition import acquire_and_persist
from job_matcher.catalog import (
    LinkedSourceJob,
    LogicalJobId,
    SourceJobLink,
    SourceJobStatus,
)
from job_matcher.filter_policy import load_filter_policy
from job_matcher.filtering import DeterministicFilterEvaluator
from job_matcher.identity import IdentityResolver
from job_matcher.models import AcquiredJob, SourceSnapshot
from job_matcher.pipeline import process_persisted_catalog
from job_matcher.ports import SourceAcquisitionError
from job_matcher.screening import Geography, ScreeningOutcome, screen_job
from job_matcher.screening_pipeline import screen_catalog
from job_matcher.screening_sqlite import read_active_screening_inputs
from job_matcher.sources import greenhouse as gh
from job_matcher.sqlite_repository import SQLiteJobRepository
from job_matcher.validation_reporting import catalog_report
from scripts import run_greenhouse_validation as runner
from scripts import run_phase2_validation as lever_runner
from tests.test_greenhouse_source import NOW, posting, source


def evaluator():
    return DeterministicFilterEvaluator(
        load_filter_policy("examples/filter_policy.example.toml")
    )


def test_lever_runner_registry_configuration_is_restored():
    assert lever_runner.VERIFIED_LEVER_REGISTRY_PATH.name == "lever_verified.json"


def screen(monkeypatch, location, description, offices=()):
    p = posting(location={"name": location}, content=description, offices=list(offices))
    acquired = source(monkeypatch, {"jobs": [p]}).fetch_snapshot().jobs[0]
    from uuid import UUID

    linked = LinkedSourceJob(
        acquired.normalized,
        SourceJobLink(acquired.normalized.key, LogicalJobId(UUID(int=1)), NOW),
    )
    return screen_job(linked, acquired.raw)


@pytest.mark.parametrize(
    "location,description,expected",
    [
        ("Canada", "You must reside in Canada.", ScreeningOutcome.EXCLUDE),
        (
            "Remote",
            "You must be legally authorized to work in Canada.",
            ScreeningOutcome.KEEP,
        ),
        ("United Kingdom", "Hybrid office role.", ScreeningOutcome.EXCLUDE),
        ("Georgia", "Build APIs.", ScreeningOutcome.KEEP),
        ("Baja California", "Build APIs.", ScreeningOutcome.KEEP),
        ("Remote", "Work from anywhere in the world.", ScreeningOutcome.KEEP),
        ("Canada", "Remote role open to US residents.", ScreeningOutcome.KEEP),
        ("United States", "Build APIs.", ScreeningOutcome.KEEP),
        ("London", "Build APIs.", ScreeningOutcome.KEEP),
        (
            "US / Canada",
            "Candidates must reside in Canada or US.",
            ScreeningOutcome.KEEP,
        ),
    ],
)
def test_geography_and_offices_never_override_restrictions(
    monkeypatch, location, description, expected
):
    decision = screen(
        monkeypatch,
        location,
        description,
        [{"name": "US Headquarters", "location": "San Francisco, CA, United States"}],
    )
    assert decision.outcome is expected
    # Employer office associations must not become work-location options.
    assert decision == replace(
        screen(monkeypatch, location, description), evaluated_at=decision.evaluated_at
    )


def test_offices_alone_do_not_confirm_us_eligibility(monkeypatch):
    decision = screen(
        monkeypatch,
        "Remote",
        "Build APIs.",
        [{"name": "US HQ", "location": "United States"}],
    )
    assert decision.geography is Geography.UNCERTAIN


def test_conflicting_job_location_and_restriction_is_retained(monkeypatch):
    decision = screen(monkeypatch, "United States", "Candidates must reside in Canada.")
    assert decision.geography is Geography.UNCERTAIN


def test_missing_content_defers_and_occupations_still_apply(monkeypatch):
    assert screen(monkeypatch, "US", "").outcome is ScreeningOutcome.DEFER
    raw = posting(title="Cookie Crew", content="<p>Bake cookies.</p>")
    job = source(monkeypatch, {"jobs": [raw]}).fetch_snapshot().jobs[0]
    from uuid import UUID

    assert (
        screen_job(
            LinkedSourceJob(
                job.normalized,
                SourceJobLink(job.normalized.key, LogicalJobId(UUID(int=1)), NOW),
            ),
            job.raw,
        ).outcome
        is ScreeningOutcome.EXCLUDE
    )


def test_repeat_failure_empty_and_cross_source_scope_isolation(monkeypatch, tmp_path):
    repo = SQLiteJobRepository(tmp_path / "catalog.sqlite3")
    repo.initialize_schema()
    first = source(monkeypatch, {"jobs": [posting()]}).fetch_snapshot()
    ghjob = first.jobs[0]
    leverkey = replace(ghjob.normalized.key, source="lever")
    lever = AcquiredJob(
        replace(ghjob.normalized, key=leverkey), replace(ghjob.raw, key=leverkey)
    )
    repo.reconcile_snapshot(SourceSnapshot("lever", "example", NOW, (lever,)))
    acquire_and_persist(source(monkeypatch, {"jobs": [posting()]}), repo)
    IdentityResolver(repo).resolve_unlinked()
    link = repo.get_link(ghjob.normalized.key)
    repeated = gh.GreenhouseJobSource(
        "example", "Example", clock=lambda: NOW + timedelta(seconds=1)
    )
    acquire_and_persist(repeated, repo)
    assert repo.get_link(ghjob.normalized.key) == link
    assert len(repo.list_linked_jobs()) == 2
    assert repo.get(leverkey) == lever
    bad = source(monkeypatch, {"jobs": [posting(), posting(id=456, title=None)]})
    with pytest.raises(SourceAcquisitionError):
        acquire_and_persist(bad, repo)
    assert repo.get_lifecycle(ghjob.normalized.key).status is SourceJobStatus.ACTIVE
    empty = source(monkeypatch, {"jobs": []})
    empty.clock = lambda: NOW + timedelta(seconds=2)
    acquire_and_persist(empty, repo)
    assert repo.get_lifecycle(ghjob.normalized.key).status is SourceJobStatus.INACTIVE
    assert repo.get_lifecycle(leverkey).status is SourceJobStatus.ACTIVE
    assert repo.get(leverkey) == lever
    reactivate = source(monkeypatch, {"jobs": [posting()]})
    reactivate.clock = lambda: NOW + timedelta(seconds=3)
    acquire_and_persist(reactivate, repo)
    assert repo.get_lifecycle(ghjob.normalized.key).status is SourceJobStatus.ACTIVE
    assert repo.get_link(ghjob.normalized.key) == link


def test_report_separates_source_contribution_links_and_candidates(
    monkeypatch, tmp_path
):
    repo = SQLiteJobRepository(tmp_path / "catalog.sqlite3")
    repo.initialize_schema()
    a = source(monkeypatch, {"jobs": [posting()]}).fetch_snapshot().jobs[0]
    leverkey = replace(a.normalized.key, source="lever")
    lever = AcquiredJob(
        replace(a.normalized, key=leverkey), replace(a.raw, key=leverkey)
    )
    repo.reconcile_snapshot(SourceSnapshot("lever", "example", NOW, (lever,)))
    IdentityResolver(repo).resolve_unlinked()
    snap = source(
        monkeypatch,
        {
            "jobs": [
                posting(),
                posting(id=456, absolute_url="https://example.com/other"),
            ]
        },
    ).fetch_snapshot()
    repo.reconcile_snapshot(snap)
    cat = process_persisted_catalog(repo, IdentityResolver(repo), evaluator())
    screened = screen_catalog(
        read_active_screening_inputs(tmp_path / "catalog.sqlite3"), evaluator()
    )
    report = catalog_report(repo, cat, screened, source="greenhouse")
    assert report["global"]["active_source_postings"] == 3
    assert report["global"]["active_unique_logical_jobs"] == 2
    assert report["by_source"]["greenhouse"]["independent_logical_outcomes"] == {
        "keep": 2
    }
    stats = report["selected_source_identity"]
    assert stats["new_unique_logical_jobs_this_run"] == 1
    assert stats["confirmed_cross_source_links_this_run"] == 1
    assert stats["shared_active_logical_jobs_with_other_sources"] == 1
    assert stats["exclusive_active_logical_jobs"] == 1
    assert stats["possible_cross_source_exact_content_groups"] == 1
    assert stats["possible_cross_source_candidate_evidence_this_run"] == 1
    assert stats["additional_ready_logical_jobs_vs_catalog_without_source"] == 1


def test_identical_source_id_and_scope_do_not_merge_without_url_evidence(
    monkeypatch, tmp_path
):
    repo = SQLiteJobRepository(tmp_path / "catalog.sqlite3")
    repo.initialize_schema()
    a = source(monkeypatch, {"jobs": [posting()]}).fetch_snapshot().jobs[0]
    leverkey = replace(a.normalized.key, source="lever")
    lever = AcquiredJob(
        replace(a.normalized, key=leverkey, job_url="https://example.com/lever"),
        replace(a.raw, key=leverkey),
    )
    repo.reconcile_snapshot(SourceSnapshot("lever", "example", NOW, (lever,)))
    repo.reconcile_snapshot(SourceSnapshot("greenhouse", "example", NOW, (a,)))
    IdentityResolver(repo).resolve_unlinked()
    assert (
        repo.get_link(leverkey).logical_job_id
        != repo.get_link(a.normalized.key).logical_job_id
    )
    assert repo.get(leverkey) == lever
    assert repo.get(a.normalized.key) == a


def test_independent_source_rejection_does_not_hide_global_keep(monkeypatch, tmp_path):
    path = tmp_path / "catalog.sqlite3"
    repo = SQLiteJobRepository(path)
    repo.initialize_schema()
    a = (
        source(monkeypatch, {"jobs": [posting(title="Registered Nurse")]})
        .fetch_snapshot()
        .jobs[0]
    )
    leverkey = replace(a.normalized.key, source="lever")
    lever = AcquiredJob(
        replace(a.normalized, key=leverkey, title="Software Engineer"),
        replace(a.raw, key=leverkey),
    )
    repo.reconcile_snapshot(SourceSnapshot("lever", "example", NOW, (lever,)))
    IdentityResolver(repo).resolve_unlinked()
    repo.reconcile_snapshot(SourceSnapshot("greenhouse", "example", NOW, (a,)))
    cat = process_persisted_catalog(repo, IdentityResolver(repo), evaluator())
    report = catalog_report(
        repo,
        cat,
        screen_catalog(read_active_screening_inputs(path), evaluator()),
        source="greenhouse",
    )
    ghstats = report["by_source"]["greenhouse"]
    assert ghstats["independent_logical_outcomes"] == {"exclude": 1}
    assert ghstats["global_logical_outcomes_of_represented_jobs"] == {"keep": 1}
    assert (
        report["selected_source_identity"]["confirmed_cross_source_links_this_run"] == 1
    )
    assert (
        report["selected_source_identity"][
            "additional_ready_logical_jobs_vs_catalog_without_source"
        ]
        == 0
    )


def test_runner_fresh_and_repeat(monkeypatch, tmp_path, capsys):
    registry = tmp_path / "registry.json"
    registry.write_text('[{"site":"example","company":"Example"}]')
    monkeypatch.setattr(gh, "_request_json", lambda *_: {"jobs": [posting()]})
    args = [
        "--database",
        str(tmp_path / "small.sqlite3"),
        "--registry",
        str(registry),
        "--sites",
        "example",
    ]
    assert runner.main(args) == 0
    output = capsys.readouterr().out
    report = json.loads(output[output.index("{") :])
    assert report["selected_source_identity"]["new_unique_logical_jobs_this_run"] == 1
    assert runner.main(args) == 1
    capsys.readouterr()
    assert runner.main(args + ["--resume"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output[output.index("{") :])
    assert report["selected_source_identity"]["new_unique_logical_jobs_this_run"] == 0
    assert report["global"]["active_unique_logical_jobs"] == 1


def test_runner_rejects_protected_database_and_full_selection(tmp_path, capsys):
    assert (
        runner.main(
            ["--database", str(runner.PROJECT_ROOT / "phase2_validation.sqlite3")]
        )
        == 1
    )
    assert "protected" in capsys.readouterr().out
    path = tmp_path / "never.sqlite3"
    assert (
        runner.main(
            ["--database", str(path), "--sites", *["s" + str(i) for i in range(6)]]
        )
        == 1
    )
    assert not path.exists()
