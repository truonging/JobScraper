"""Work authorization is uncertainty evidence, not a US work-location option."""

import json
from datetime import UTC, datetime

import pytest

from job_matcher.geographic_classification import classify_geography
from job_matcher.geographic_enforcement import (
    GeographicAction,
    apply_geographic_enforcement,
    geographic_enforcement,
)
from job_matcher.models import NormalizedJob, RawSourceRecord, SourceJobKey
from job_matcher.screening import ScreeningOutcome

NOW = datetime(2026, 11, 6, tzinfo=UTC)
REQUIREMENT = "Applicants must be authorized to work in the US on a permanent and ongoing basis without the need for current or future employer-sponsored work authorization."


def assessment(location, description):
    return classify_geography(
        NormalizedJob(
            SourceJobKey("greenhouse", "authorization-fixture", "1"),
            "Fixture",
            "Engineer",
            description,
            "https://example.com/1",
            NOW,
            location,
        )
    )


def test_real_beghou_source_requirement_preserves_KEEP_without_claiming_US_option():
    # Original role requirement and metadata from the immutable catalog snapshot.
    key = SourceJobKey(
        "lever", "beghouconsulting", "c8ef70e8-11cd-470a-85fe-84aa8fada3b5"
    )
    job = NormalizedJob(
        key,
        "Beghou Consulting",
        "Senior Consultant, PSS/ PSP",
        "We are currently unable to consider candidates who require, or will require in the future, sponsorship for work authorization. "
        + REQUIREMENT,
        "https://jobs.lever.co/beghouconsulting/c8ef70e8-11cd-470a-85fe-84aa8fada3b5",
        NOW,
        "Bangalore",
    )
    raw = RawSourceRecord(
        key,
        NOW,
        json.dumps(
            {
                "country": "IN",
                "workplaceType": "hybrid",
                "categories": {"allLocations": ["Bangalore"]},
            }
        ),
    )
    result = classify_geography(job, raw)
    action = geographic_enforcement(result)
    assert result.outcome is ScreeningOutcome.DEFER
    assert result.reason == "conflicting_location_evidence"
    assert "US_work_authorization_vs_foreign_work_location" in result.conflicts
    assert not result.us_exceptions
    assert any(REQUIREMENT in e.value for e in result.evidence)
    assert action.action is GeographicAction.REVIEW_ONLY
    assert (
        apply_geographic_enforcement(ScreeningOutcome.KEEP, action)
        is ScreeningOutcome.KEEP
    )
    assert (
        apply_geographic_enforcement(ScreeningOutcome.EXCLUDE, action)
        is ScreeningOutcome.EXCLUDE
    )


@pytest.mark.parametrize(
    "requirement",
    [
        REQUIREMENT,
        "Candidates must have permanent US work authorization.",
        "This role requires US work authorization.",
        "The position requires United States employment eligibility.",
        "Applicants must be legally eligible for employment in the United States.",
        "Must be authorized to work in the United States.",
        "Applicants must currently be authorized to work in the USA.",
        "You must be legally authorised to work in the United States.",
        "Applicants must be authorized to work for any employer in the U.S.",
        "Skills and Experience: ● Scrum Master certification preferred ● Must be authorized to work in the U.S.",
        "Continued employment in this position is contingent upon obtaining valid US work authorization and visa eligibility.",
        "In compliance with federal law, all persons hired will be required to verify identity and eligibility to work in the United States and to complete the required employment eligibility verification form upon hire.",
    ],
)
def test_foreign_worksite_and_employee_US_authorization_is_review_only(requirement):
    result = assessment("Bengaluru, India", requirement)
    assert geographic_enforcement(result).action is GeographicAction.REVIEW_ONLY
    assert not result.us_exceptions


@pytest.mark.parametrize(
    "description",
    [
        "You support US customers.",
        "This role works US business hours.",
        "Our headquarters are in the US.",
        "Candidates must obtain a United States Secret security clearance.",
        "Our customers must be authorized to work in the US.",
        "Your manager must be authorized to work in the US.",
        "Applicants are not required to be authorized to work in the US.",
        "For US-based roles: Applicants must be authorized to work in the US.",
        "US Applicants: You must be authorized to work in the US.",
        "If you apply for a US position, you must be authorized to work in the US.",
        "We provide the government with Form I-9 information to confirm authorization to work in the U.S.",
        "Export-controlled technology may require authorization, and government contracts require U.S. Persons.",
    ],
)
def test_incidental_or_conditional_US_language_does_not_undermine_foreign_worksite(
    description,
):
    assert (
        geographic_enforcement(assessment("Bengaluru, India", description)).action
        is GeographicAction.EXCLUDE
    )


@pytest.mark.parametrize(
    "location,restriction",
    [
        ("India", "Candidates must reside in India."),
        ("India", "This role is onsite in Bengaluru, India."),
        ("Berlin", "This role has a hybrid office schedule in Berlin."),
        (
            "Costa Rica",
            "This role is based in our Costa Rica office in Heredia, with required office days every week.",
        ),
        ("Remote India only", "Build services."),
        ("Remote Canada", "This role is remote within Canada."),
        ("Remote EMEA only", "Build services."),
        ("India", "This role is not available in the US."),
    ],
)
def test_US_authorization_does_not_override_definitive_foreign_requirement(
    location, restriction
):
    assert (
        geographic_enforcement(
            assessment(location, restriction + "\n" + REQUIREMENT)
        ).action
        is GeographicAction.EXCLUDE
    )


@pytest.mark.parametrize(
    "description,expected",
    [
        ("This role may be based in the US.", GeographicAction.PASS),
        (
            "Candidates must reside in India. This role is open to applicants in the US.",
            GeographicAction.REVIEW_ONLY,
        ),
    ],
)
def test_actual_US_work_option_keeps_existing_precedence(description, expected):
    assert (
        geographic_enforcement(
            assessment("India", description + "\n" + REQUIREMENT)
        ).action
        is expected
    )


def test_authorization_without_work_location_does_not_establish_US_eligibility():
    result = assessment(None, "Applicants must be authorized to work in the US.")
    assert not result.us_exceptions
    assert geographic_enforcement(result).action is GeographicAction.REVIEW_ONLY


def test_future_US_relocation_authorization_is_a_conflict_with_foreign_field():
    result = assessment(
        "Banbury, UK",
        "This role is initially based in the UK, with a mandatory requirement to relocate to Foster City, California (San Francisco Bay Area) after 12 months. "
        "Continued employment in this position is contingent upon obtaining valid US work authorization and visa eligibility.",
    )
    assert geographic_enforcement(result).action is GeographicAction.REVIEW_ONLY
