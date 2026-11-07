"""The independently proposed enforcement never defers a retained source."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from job_matcher.geographic_classification import LocationConfidence, classify_geography
from job_matcher.geographic_enforcement import (
    GeographicAction,
    apply_geographic_enforcement,
    geographic_enforcement,
)
from job_matcher.models import NormalizedJob, SourceJobKey
from job_matcher.screening import Evidence, ScreeningOutcome


def assess(location, description="Build services."):
    return classify_geography(
        NormalizedJob(
            SourceJobKey("greenhouse", "fixture", "1"),
            "Fixture",
            "Engineer",
            description,
            "https://example.com/1",
            datetime(2026, 11, 6, tzinfo=UTC),
            location,
        )
    )


@pytest.mark.parametrize(
    ("location", "description", "action"),
    [
        (
            "Costa Rica",
            "This role is based in our Costa Rica office in Heredia, with required office days every week.",
            "exclude",
        ),
        ("Bengaluru, India", "Build services.", "exclude"),
        ("Remote India only", "Build services.", "exclude"),
        ("Remote US", "Build services.", "pass"),
        (
            "Remote Worldwide",
            "This role is remote worldwide, including the US.",
            "pass",
        ),
        ("Remote US/Canada", "Build services.", "pass"),
        ("Remote North America", "Build services.", "pass"),
        (
            "Amsterdam",
            "This role is based out of our Amsterdam or San Francisco office.",
            "pass",
        ),
        (
            "Canada",
            "This role is open to multiple locations in North America (excluding CA, NY, & WA).",
            "pass",
        ),
        ("New Mexico, United States", "Build services.", "pass"),
        ("Turkey, Texas", "Build services.", "pass"),
        ("Lebanon, PA 17042", "Build services.", "pass"),
        (None, "Customers must reside in Germany.", "review_only"),
        (
            None,
            "You report to the Director of Product, who is based in Berlin.",
            "review_only",
        ),
        (None, "Our headquarters is in Germany.", "review_only"),
        ("India", "You will work US business hours.", "exclude"),
        (
            "Australia",
            "Eligible to obtain United States Secret security clearance.",
            "exclude",
        ),
        (
            "Greece",
            "This is a hybrid role at the intersection of engineering and data science.",
            "review_only",
        ),
        (
            "Ireland",
            "Candidates based in Europe are preferred, while remote candidates working European business hours will be considered.",
            "review_only",
        ),
        (
            "Remote - Mexico",
            "Location: Remote Mexico, Brazil, or other international locations.",
            "review_only",
        ),
        (
            "Remote Canada",
            "Candidates must reside in Canada. This role is open to applicants in the US.",
            "review_only",
        ),
        ("Canada recruiting market", "Build services.", "review_only"),
        ("Remote - EMEA/APAC timezones", "Build services.", "review_only"),
        ("Remote - US business hours", "Build services.", "review_only"),
        ("AMER - Remote; EMEA", "Build services.", "review_only"),
        ("Remote Americas", "Build services.", "review_only"),
        ("Remote EMEA", "Build services.", "exclude"),
        ("Remote EMEA timezones", "Candidates must reside in Germany.", "exclude"),
        ("Remote EMEA timezones", "This role is open to applicants in the US.", "pass"),
        (
            "Remote EMEA",
            "Remote-First Flexibility: You can work where you're most productive.",
            "review_only",
        ),
    ],
)
def test_enforcement_regressions(location, description, action):
    assessment = assess(location, description)
    enforcement = geographic_enforcement(assessment)
    assert enforcement.action == action
    assert enforcement.assessment is assessment
    result = apply_geographic_enforcement(ScreeningOutcome.KEEP, enforcement)
    assert result == ("exclude" if action == "exclude" else "keep")
    if action == "review_only":
        assert assessment.outcome == "defer"
        assert assessment.uncertainty_details


@pytest.mark.parametrize("existing", list(ScreeningOutcome))
@pytest.mark.parametrize("location", ["India", "United States", None])
def test_existing_outcome_is_preserved_unless_geography_excludes(existing, location):
    enforcement = geographic_enforcement(assess(location))
    expected = ScreeningOutcome.EXCLUDE if location == "India" else existing
    assert apply_geographic_enforcement(existing, enforcement) is expected


@pytest.mark.parametrize(
    "change",
    [
        {"confidence": LocationConfidence.MEDIUM},
        {"reason": "unvalidated_future_rule"},
        {"conflicts": ("conflicting_US_option",)},
        {"us_exceptions": (Evidence("description", "US work offered"),)},
        {"uncertainty_details": ("country_market_possible",)},
    ],
)
def test_weak_or_inconsistent_exclusion_is_review_only(change):
    enforcement = geographic_enforcement(replace(assess("India"), **change))
    assert enforcement.action is GeographicAction.REVIEW_ONLY
    assert (
        apply_geographic_enforcement(ScreeningOutcome.KEEP, enforcement)
        is ScreeningOutcome.KEEP
    )


def test_reject_invalid_existing_outcome():
    with pytest.raises(ValueError, match="ScreeningOutcome"):
        apply_geographic_enforcement("defer", geographic_enforcement(assess(None)))


@pytest.mark.parametrize(
    ("location", "description", "action"),
    [
        (
            "Remote Europe",
            "Location: We are happy to consider candidates in any location, but would have a preference for those based in or willing to relocate to Europe.",
            "pass",
        ),
        (
            "Remote Canada",
            "We consider candidates in any location within Canada.",
            "exclude",
        ),
        (
            "Remote",
            "Candidates residing in the Netherlands and Germany are excluded from undergoing background checks via Certn.co.",
            "review_only",
        ),
        (
            "New York",
            "Candidates residing in the Netherlands and Germany are excluded from undergoing background checks via Certn.co.",
            "pass",
        ),
        (
            "Remote Brazil",
            "Workplace requirement: Candidates residing in the Campinas Metropolitan Region must attend the company's offices in Campinas.",
            "review_only",
        ),
        ("Canada", "This role is open to candidates in North America.", "pass"),
        (
            "Remote Canada only",
            "This role is open to candidates in North America.",
            "review_only",
        ),
        (
            "Remote Canada only",
            "This role is open to applicants in the US.",
            "review_only",
        ),
    ],
)
def test_audited_grammar_and_option_scope(location, description, action):
    assessment = assess(location, description)
    assert geographic_enforcement(assessment).action == action


@pytest.mark.parametrize(
    ("location", "description", "action"),
    [
        ("India", "This role may be based in the US after a background check.", "pass"),
        (
            "Remote Germany",
            "Candidates must reside in Germany and complete a background check.",
            "exclude",
        ),
    ],
)
def test_background_check_does_not_erase_actual_location_permission_or_requirement(
    location, description, action
):
    assert geographic_enforcement(assess(location, description)).action == action


@pytest.mark.parametrize(
    ("location", "description", "action"),
    [
        (
            "Canada - Remote (ON, AB, BC, or NS Only)",
            "The role reports to the Senior Director, Enterprise Solutions, and is remote across the United States and Canada with a preference for proximity to Toronto.",
            "review_only",
        ),
        (
            "India",
            "Work arrangement: Remote opportunity advertised for candidates based in India. Important eligibility clarification: The original job description includes requirements for candidates to be legally authorized to work in the United States, as well as to reside within commuting distance of San Jose, California, or Albuquerque, New Mexico.",
            "review_only",
        ),
        (
            "India",
            "Candidates must reside within commuting distance of San Jose, California.",
            "pass",
        ),
        (
            None,
            "Our customers must reside within commuting distance of San Jose, California.",
            "review_only",
        ),
        (
            None,
            "You support clients who reside within commuting distance of San Jose, California.",
            "review_only",
        ),
        (
            None,
            "The role reports to a director who is remote across the United States.",
            "review_only",
        ),
        (
            None,
            "Candidates are not required to reside within commuting distance of San Jose, California.",
            "review_only",
        ),
    ],
)
def test_US_option_survives_manager_prelude_and_commuting_grammar(
    location, description, action
):
    assert geographic_enforcement(assess(location, description)).action == action
