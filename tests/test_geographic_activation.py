"""Exercise H-only geography in the shared production screen and cache fingerprint."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from job_matcher.catalog import LinkedSourceJob, LogicalJobId, SourceJobLink
from job_matcher.filter_policy import load_filter_policy
from job_matcher.filtering import DeterministicFilterEvaluator
from job_matcher.models import NormalizedJob, RawSourceRecord, SourceJobKey
from job_matcher.screening import ScreeningOutcome, aggregate_screening, screen_job
from job_matcher.screening_pipeline import screen_catalog
from job_matcher.search_index import implementation_signature

NOW = datetime(2026, 11, 6, tzinfo=UTC)
LOGICAL = LogicalJobId(UUID(int=17))


def source(
    number,
    location="Remote",
    description="Build backend services.",
    title="Engineer",
    country=None,
    workplace=None,
):
    key = SourceJobKey("lever", "activation-fixture", number)
    job = NormalizedJob(
        key,
        "Fixture",
        title,
        description,
        f"https://example.com/{number}",
        NOW,
        location,
    )
    return LinkedSourceJob(job, SourceJobLink(key, LOGICAL, NOW)), RawSourceRecord(
        key, NOW, json.dumps({"country": country, "workplaceType": workplace})
    )


@pytest.mark.parametrize(
    "location,description,action,expected",
    [
        ("Bengaluru, India", "Build services.", "exclude", ScreeningOutcome.EXCLUDE),
        ("Remote", "Build services.", "review_only", ScreeningOutcome.KEEP),
        ("Remote", "", "review_only", ScreeningOutcome.DEFER),
        ("Austin, TX", "Build services.", "pass", ScreeningOutcome.KEEP),
        ("Austin, TX", "", "pass", ScreeningOutcome.DEFER),
        (
            "Canada recruiting market",
            "Build services.",
            "review_only",
            ScreeningOutcome.KEEP,
        ),
        (
            "India",
            "Applicants must be authorized to work in the US on a permanent and ongoing basis.",
            "review_only",
            ScreeningOutcome.KEEP,
        ),
    ],
)
def test_production_enforcement_keeps_assessment_and_final_outcome_separate(
    location, description, action, expected
):
    decision = screen_job(*source("one", location, description))
    assert decision.outcome is expected
    assert decision.version == "5"
    assert any(
        e.field == "geo_h.action" and e.value == action for e in decision.evidence
    )
    assert any(r.startswith(f"geo_h:{action}:") for r in decision.reasons)


def test_H_pass_retires_legacy_geography_but_preserves_nongeographic_rejections():
    legacy = screen_job(
        *source(
            "legacy",
            "London",
            "This role offers relocation assistance to San Francisco.",
            country="GB",
            workplace="onsite",
        )
    )
    # Demo exclusions remain independent of geographic interpretation.
    demo = screen_job(
        *source("demo", "Austin, TX", "This is a test posting. Do not apply.")
    )
    assert legacy.outcome is ScreeningOutcome.KEEP
    assert any(e.field == "geo_h.action" and e.value == "pass" for e in legacy.evidence)
    assert demo.outcome is ScreeningOutcome.EXCLUDE
    evaluator = DeterministicFilterEvaluator(
        load_filter_policy("examples/filter_policy.example.toml")
    )
    result = screen_catalog(
        [source("filtered", "Austin, TX", title="Truck Driver")], evaluator
    )
    assert result.logical_outcomes[LOGICAL] is ScreeningOutcome.EXCLUDE


def test_sources_are_classified_independently_then_aggregated():
    foreign = screen_job(*source("foreign", "Bengaluru, India"))
    domestic = screen_job(*source("domestic", "Remote US", country="US"))
    assert foreign.outcome is ScreeningOutcome.EXCLUDE
    assert domestic.outcome is ScreeningOutcome.KEEP
    assert aggregate_screening([foreign, domestic])[LOGICAL] is ScreeningOutcome.KEEP
    assert (
        screen_job(*source("foreign", "Bengaluru, India")).outcome
        is ScreeningOutcome.EXCLUDE
    )


def test_Zoox_future_relocation_conflict_preserves_eligibility():
    # Location/requirements from the audited Zoox Staff Vehicle Attributes role.
    linked, raw = source(
        "zoox-relocation",
        "Banbury, UK",
        "This role is initially based in the UK, with a mandatory requirement to "
        "relocate to Foster City, California (San Francisco Bay Area) after 12 months. "
        "Continued employment in this position is contingent upon obtaining valid "
        "US work authorization and visa eligibility.",
        title="Staff Vehicle Attributes Engineer",
        country="GB",
        workplace="onsite",
    )
    decision = screen_job(linked, raw)
    assert decision.outcome is ScreeningOutcome.KEEP
    assert "geo_h:review_only:conflicting_location_evidence" in decision.reasons


@pytest.mark.parametrize("location", ["Austin, TX", "Remote"])
def test_H_PASS_or_REVIEW_cannot_rescue_occupational_exclusions(location):
    decision = screen_job(
        *source(
            "occupation",
            location,
            "Operate the cash register, stock store shelves, and assist shoppers.",
            title="Retail Sales Associate",
        )
    )
    assert decision.outcome is ScreeningOutcome.EXCLUDE
    assert any(not r.startswith("geo_h:") for r in decision.reasons)


@pytest.mark.parametrize(
    "name",
    ["geographic_classification.py", "geographic_enforcement.py", "geography_terms.py"],
)
def test_H_module_changes_invalidate_screening_fingerprint(monkeypatch, name):
    before = implementation_signature()
    original = Path.read_bytes

    def changed(path):
        contents = original(path)
        return (
            contents + b"\n# fingerprint regression\n"
            if path.name == name
            else contents
        )

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert implementation_signature() != before
