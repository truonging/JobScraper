"""Conservative screening and representation aggregation regressions."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from job_matcher.catalog import LinkedSourceJob, LogicalJobId, SourceJobLink
from job_matcher.filter_policy import FilterOutcome, load_filter_policy
from job_matcher.filtering import DeterministicFilterEvaluator
from job_matcher.models import (
    AcquiredJob,
    ContractValidationError,
    NormalizedJob,
    RawSourceRecord,
    SourceJobKey,
    SourceSnapshot,
)
from job_matcher.ports import PersistenceError
from job_matcher.screening import (
    Geography,
    ScreeningOutcome,
    aggregate_screening,
    screen_job,
)
from job_matcher.screening_pipeline import screen_catalog
from job_matcher.screening_sqlite import read_active_screening_inputs
from job_matcher.sqlite_repository import SQLiteJobRepository

NOW = datetime(2026, 11, 3, tzinfo=UTC)
ID = LogicalJobId(UUID(int=1))


def example(
    *,
    location="Remote",
    description="Build and support systems.",
    title="Software Engineer",
    country=None,
    workplace="remote",
    alternates=(),
    source="lever",
    number="1",
):
    key = SourceJobKey(source, "synthetic", number)
    job = NormalizedJob(
        key,
        "Synthetic",
        title,
        description,
        f"https://example.com/{number}",
        NOW,
        location,
    )
    linked = LinkedSourceJob(job, SourceJobLink(key, ID, NOW))
    raw = RawSourceRecord(
        key,
        NOW,
        json.dumps(
            {
                "country": country,
                "workplaceType": workplace,
                "categories": {"allLocations": list(alternates)},
            }
        ),
    )
    return linked, raw


@pytest.mark.parametrize("workplace", ["onsite", "hybrid", "remote"])
def test_us_workplaces_survive(workplace):
    decision = screen_job(
        *example(location="Austin, TX", country="US", workplace=workplace)
    )
    assert decision.outcome is ScreeningOutcome.KEEP
    assert decision.geography in {Geography.US, Geography.US_REMOTE}


@pytest.mark.parametrize(
    "location,country,alternates",
    [
        ("Toronto, Ontario", "CA", ("Remote - US",)),
        ("London", "GB", ("Boston, Massachusetts",)),
        ("Baja California, Mexico", "MX", ("San Diego, CA",)),
        ("Tbilisi, Georgia", "GE", ("Atlanta, GA",)),
    ],
)
def test_us_alternate_prevents_foreign_rejection(location, country, alternates):
    assert (
        screen_job(
            *example(location=location, country=country, alternates=alternates)
        ).outcome
        is ScreeningOutcome.KEEP
    )


@pytest.mark.parametrize(
    "location,country",
    [("Bengaluru", "IN"), ("Baja California, Mexico", "MX"), ("Berlin, Germany", "DE")],
)
@pytest.mark.parametrize("workplace", ["onsite", "hybrid"])
def test_foreign_onsite_and_hybrid(location, country, workplace):
    decision = screen_job(
        *example(location=location, country=country, workplace=workplace)
    )
    assert decision.geography is Geography.FOREIGN_ONLY
    assert decision.outcome is ScreeningOutcome.EXCLUDE


@pytest.mark.parametrize(
    "location,country,description",
    [
        ("Georgia", None, "Build systems."),
        ("Remote", None, "Build systems."),
        ("Berlin", "DE", "Remote work is available."),
        ("India", "IN", "Build systems."),
        ("Brazil", "BR", "Candidates must be born in Latin America."),
        ("India", "IN", "You do not have to reside in India."),
    ],
)
def test_uncertain_remote_retained(location, country, description):
    decision = screen_job(
        *example(location=location, country=country, description=description)
    )
    assert decision.geography is Geography.UNCERTAIN
    assert decision.outcome is ScreeningOutcome.KEEP


def test_explicit_us_remote_description_retained():
    decision = screen_job(
        *example(
            country="CA",
            description="Remote candidates may be located in the United States.",
        )
    )
    assert decision.outcome is ScreeningOutcome.KEEP
    assert any("United States" in item.value for item in decision.evidence)


@pytest.mark.parametrize("workplace", ["onsite", "hybrid"])
@pytest.mark.parametrize(
    "location,country", [("Baja California", "MX"), ("Tbilisi, Georgia", "GE")]
)
def test_H_uncertainty_is_not_overridden_by_legacy_country_and_workplace(
    location, country, workplace
):
    decision = screen_job(
        *example(location=location, country=country, workplace=workplace)
    )
    assert decision.outcome is ScreeningOutcome.KEEP
    assert decision.geography is Geography.UNCERTAIN
    assert any(r.startswith("geo_h:review_only:") for r in decision.reasons)


def test_worldwide_remote_is_H_US_compatible():
    decision = screen_job(
        *example(
            location="Canada",
            country="CA",
            description="Work from anywhere in the world.",
        )
    )
    assert decision.outcome is ScreeningOutcome.KEEP
    assert decision.geography is Geography.US_REMOTE


@pytest.mark.parametrize("country_name", ["US", "U.S.", "United States"])
def test_us_residents_eligibility_prevents_foreign_exclusion(country_name):
    decision = screen_job(
        *example(
            location="Canada",
            country="CA",
            workplace="hybrid",
            description=f"This job is open to {country_name} residents. Remote work available.",
        )
    )
    assert decision.outcome is ScreeningOutcome.KEEP
    assert decision.geography is Geography.US


@pytest.mark.parametrize("country", [None, "IN"])
def test_explicit_residence_restriction(country):
    decision = screen_job(
        *example(
            country=country,
            description="Candidates must be currently residing in India.",
        )
    )
    assert decision.outcome is ScreeningOutcome.EXCLUDE
    assert "geo_h:exclude:explicit_foreign_employee_location" in decision.reasons
    assert any(
        e.field.startswith("geo_h.foreign_restriction.")
        and "residing in India" in e.value
        for e in decision.evidence
    )


@pytest.mark.parametrize(
    "description",
    [
        "These jobs are testing jobs of Lever's testing environment - please do not apply for this job.",
        "This posting is a demo posting. Please don't apply.",
        "This job is part of a testing environment. Please do not apply.",
        "This is a test posting. Do not apply.",
    ],
)
def test_explicit_demo_disclaimer(description):
    decision = screen_job(*example(description=description))
    assert decision.outcome is ScreeningOutcome.EXCLUDE
    assert "explicit_testing_do_not_apply" in decision.reasons


@pytest.mark.parametrize(
    "title,description",
    [
        (
            "QA Test Engineer",
            "Maintain our testing environment. Applicants should apply.",
        ),
        ("Demo Engineer", "Demonstrate software to customers."),
        (
            "Software Engineer",
            "Do not apply production changes directly. Use the testing environment.",
        ),
    ],
)
def test_testing_work_is_not_testing_disclaimer(title, description):
    assert (
        screen_job(*example(title=title, description=description)).outcome
        is ScreeningOutcome.KEEP
    )


@pytest.mark.parametrize(
    "description", ["", " \n\t ", "TBD", "[Insert General Job Description Here]"]
)
def test_missing_or_placeholder_description_deferred(description):
    decision = screen_job(*example(description=description))
    assert decision.outcome is ScreeningOutcome.DEFER
    assert "insufficient_description" in decision.reasons


def test_short_but_substantive_description_is_kept():
    assert (
        screen_job(*example(description="Maintain Linux servers.")).outcome
        is ScreeningOutcome.KEEP
    )


def test_foreign_exclusion_takes_precedence_over_missing_description():
    assert (
        screen_job(
            *example(location="India", country="IN", workplace="onsite", description="")
        ).outcome
        is ScreeningOutcome.EXCLUDE
    )


def test_raw_evidence_must_match_snapshot():
    linked, raw = example()
    with pytest.raises(ContractValidationError):
        screen_job(linked, replace(raw, key=replace(raw.key, source_job_id="other")))
    with pytest.raises(ContractValidationError):
        screen_job(linked, replace(raw, retrieved_at=datetime(2026, 1, 1, tzinfo=UTC)))


def test_non_lever_raw_metadata_is_not_interpreted_as_lever():
    decision = screen_job(
        *example(source="other", location="Georgia", country="IN", workplace="onsite")
    )
    assert decision.geography is Geography.UNCERTAIN


def test_aggregation_keep_then_defer_then_exclude():
    keep = screen_job(*example())
    defer = screen_job(*example(description=""))
    excluded = screen_job(*example(location="India", country="IN", workplace="onsite"))
    assert aggregate_screening([excluded, defer])[ID] is ScreeningOutcome.DEFER
    assert aggregate_screening([keep, excluded, defer])[ID] is ScreeningOutcome.KEEP
    assert aggregate_screening([defer, excluded, keep])[ID] is ScreeningOutcome.KEEP


def test_filter_and_screen_must_pass_on_same_representation():
    evaluator = DeterministicFilterEvaluator(
        load_filter_policy("examples/filter_policy.example.toml")
    )
    excluded_us = example(
        title="Truck Driver", country="US", location="Boston, MA", number="1"
    )
    foreign_technical = example(
        country="IN", location="India", workplace="onsite", number="2"
    )
    result = screen_catalog([excluded_us, foreign_technical], evaluator)
    assert result.logical_outcomes[ID] is ScreeningOutcome.EXCLUDE
    result = screen_catalog(
        [excluded_us, foreign_technical, example(number="3")], evaluator
    )
    assert result.logical_outcomes[ID] is ScreeningOutcome.KEEP


def test_it_dispatcher_narrow_exception_preserves_other_exclusions():
    evaluator = DeterministicFilterEvaluator(
        load_filter_policy("examples/filter_policy.example.toml")
    )
    for title in ["IT Dispatcher", "  it   dispatcher "]:
        assert (
            evaluator.evaluate((example(title=title)[0],))[0].outcome
            is FilterOutcome.ELIGIBLE
        )
    for title in [
        "Dispatcher",
        "Truck Dispatcher",
        "Residential Dispatcher",
        "IT Dispatcher / Truck Driver",
    ]:
        assert (
            evaluator.evaluate((example(title=title)[0],))[0].outcome
            is FilterOutcome.REJECTED
        )
    linked, _ = example(
        title="IT Dispatcher", description="Active security clearance required"
    )
    assert evaluator.evaluate((linked,))[0].outcome is FilterOutcome.REJECTED


def test_sqlite_projection_reads_active_only_and_does_not_write(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    repository = SQLiteJobRepository(path)
    repository.initialize_schema()
    linked, raw = example()
    repository.reconcile_snapshot(
        SourceSnapshot(
            "lever", "synthetic", NOW, (AcquiredJob(linked.normalized, raw),)
        )
    )
    repository.assign_logical_job(linked.normalized.key, ID, NOW)
    before = hashlib.sha256(path.read_bytes()).digest()
    inputs = list(read_active_screening_inputs(path))
    assert inputs == [(linked, raw)]
    assert hashlib.sha256(path.read_bytes()).digest() == before
    later = datetime(2026, 11, 4, tzinfo=UTC)
    repository.reconcile_snapshot(SourceSnapshot("lever", "synthetic", later, ()))
    assert list(read_active_screening_inputs(path)) == []


def test_missing_catalog_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(PersistenceError):
        list(read_active_screening_inputs(path))
    assert not path.exists()


def test_join_us_is_not_us_eligibility():
    decision = screen_job(
        *example(
            location="India",
            country="IN",
            workplace="onsite",
            description="An office-based role. Join us.",
        )
    )
    assert decision.outcome is ScreeningOutcome.EXCLUDE
