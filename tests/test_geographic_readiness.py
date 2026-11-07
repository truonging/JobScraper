"""Travel scope and source-local aggregation remain safe at integration time."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from job_matcher.catalog import LogicalJobId
from job_matcher.geographic_classification import classify_geography
from job_matcher.geographic_enforcement import (
    GeographicAction,
    apply_geographic_enforcement,
    geographic_enforcement,
)
from job_matcher.models import NormalizedJob, SourceJobKey
from job_matcher.screening import (
    Geography,
    ScreeningDecision,
    ScreeningOutcome,
    aggregate_screening,
)

NOW = datetime(2026, 11, 6, tzinfo=UTC)


def posting(number, location=None, description="Build services."):
    return NormalizedJob(
        SourceJobKey("greenhouse", "readiness", number),
        "Fixture",
        "Engineer",
        description,
        f"https://example.com/{number}",
        NOW,
        location,
    )


def effective(job, existing=ScreeningOutcome.KEEP):
    enforcement = geographic_enforcement(classify_geography(job))
    return ScreeningDecision(
        LogicalJobId(UUID(int=1)),
        job.key,
        NOW,
        NOW,
        Geography.UNCERTAIN,
        apply_geographic_enforcement(existing, enforcement),
        (enforcement.reason,),
        (),
    )


@pytest.mark.parametrize(
    "description",
    [
        "This role requires travel to Germany for customer meetings.",
        "You will work with clients onsite in London during quarterly business trips.",
        "You must be willing to travel to our India office twice a year.",
    ],
)
def test_foreign_travel_is_not_foreign_residence(description):
    assessment = classify_geography(posting("travel", description=description))
    enforcement = geographic_enforcement(assessment)
    assert enforcement.action is GeographicAction.REVIEW_ONLY
    assert (
        apply_geographic_enforcement(ScreeningOutcome.KEEP, enforcement)
        is ScreeningOutcome.KEEP
    )


def test_enforce_each_source_before_aggregating_US_and_foreign_options():
    foreign = posting("foreign", "Bengaluru, India")
    domestic = posting("domestic", "United States")
    foreign_decision, domestic_decision = effective(foreign), effective(domestic)
    assert foreign_decision.outcome is ScreeningOutcome.EXCLUDE
    assert domestic_decision.outcome is ScreeningOutcome.KEEP
    assert aggregate_screening((foreign_decision, domestic_decision)) == {
        foreign_decision.logical_job_id: ScreeningOutcome.KEEP
    }
    # US evidence from another representation must not rescue this source.
    assert effective(foreign).outcome is ScreeningOutcome.EXCLUDE


def test_existing_defer_and_unrelated_exclusion_survive_geographic_pass_or_review():
    foreign = effective(posting("foreign", "India"))
    uncertain = effective(posting("unknown", description=""), ScreeningOutcome.DEFER)
    rejected = effective(posting("rejected", "United States"), ScreeningOutcome.EXCLUDE)
    assert uncertain.outcome is ScreeningOutcome.DEFER
    assert rejected.outcome is ScreeningOutcome.EXCLUDE
    assert aggregate_screening((foreign, uncertain, rejected)) == {
        foreign.logical_job_id: ScreeningOutcome.DEFER
    }


@pytest.mark.parametrize(
    "location,description",
    [
        (
            "New Delhi",
            "Location: Remote in India\nRate: 4 USD/hour (rates vary per country. If you are based elsewhere, the rates will be different)",
        ),
        (
            "Paris",
            "Location: Remote in France\nRate: 16.50 USD per hour (rates vary per country; if you are based elsewhere, the rates will be different)",
        ),
        ("Kuala Lumpur", "Location: Remote in Malaysia\nRate: Rates vary per country"),
    ],
)
def test_country_rate_options_make_unbounded_remote_label_review_only(
    location, description
):
    assessment = classify_geography(posting("rates", location, description))
    action = geographic_enforcement(assessment)
    assert assessment.reason == "remote_policy_or_role_ambiguity"
    assert action.action is GeographicAction.REVIEW_ONLY
    assert (
        apply_geographic_enforcement(ScreeningOutcome.KEEP, action)
        is ScreeningOutcome.KEEP
    )


@pytest.mark.parametrize(
    "location,requirement",
    [
        ("India", "Candidates must reside in India."),
        ("India", "Remote only in India."),
        ("Remote - India only", "Location: Remote in India"),
        ("India", "This role is based in our India office."),
    ],
)
def test_country_rate_text_does_not_override_mandatory_foreign_restriction(
    location, requirement
):
    assessment = classify_geography(
        posting("restricted", location, requirement + "\nRate: Rates vary per country")
    )
    assert geographic_enforcement(assessment).action is GeographicAction.EXCLUDE


@pytest.mark.parametrize(
    "location",
    [
        "Jamaica, New York 11412",
        "Jamaica, NewYork  11420",
        "Lebanon, Pennsylvania 17042",
        "Turkey, Texas 79261",
        "Mexico, Missouri 65265-1234",
    ],
)
def test_US_state_and_zip_disambiguate_country_namesake(location):
    assessment = classify_geography(posting("zip", location))
    assert assessment.outcome is ScreeningOutcome.KEEP
    assert geographic_enforcement(assessment).action is GeographicAction.PASS


def test_foreign_address_is_not_US_based_only_on_california_name():
    assessment = classify_geography(posting("baja", "Tijuana, Baja California, Mexico"))
    assert geographic_enforcement(assessment).action is GeographicAction.EXCLUDE


@pytest.mark.parametrize("mandatory", [False, True])
def test_outside_primary_location_application_is_uncertain_unless_country_mandatory(
    mandatory,
):
    description = (
        "If you are applying for this role outside of the primary location and you are selected for an interview, "
        "the Talent Acquisition Partner can share more information with you.\n"
        "If you're within a comfortable travel distance to one of our offices, you'll occasionally join us in person."
    )
    if mandatory:
        description += "\nNOTE: Candidates must reside in Canada."
    action = geographic_enforcement(
        classify_geography(posting("primary", "Canada", description))
    )
    assert action.action is (
        GeographicAction.EXCLUDE if mandatory else GeographicAction.REVIEW_ONLY
    )
