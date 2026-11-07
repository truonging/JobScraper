"""Positive duties, ambiguous occupations, and technical counterexamples."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from job_matcher.catalog import LinkedSourceJob, LogicalJobId, SourceJobLink
from job_matcher.filter_policy import load_filter_policy
from job_matcher.filtering import DeterministicFilterEvaluator
from job_matcher.models import NormalizedJob, SourceJobKey
from job_matcher.occupational_screening import occupational_matches
from job_matcher.screening import ScreeningOutcome, screen_job
from job_matcher.screening_pipeline import occupational_summary, screen_catalog


def linked(title, description, number="1"):
    key = SourceJobKey("synthetic", "example", number)
    now = datetime(2026, 11, 3, tzinfo=UTC)
    job = NormalizedJob(
        key, "Example", title, description, f"https://example.com/{number}", now, "US"
    )
    return LinkedSourceJob(job, SourceJobLink(key, LogicalJobId(UUID(int=1)), now))


@pytest.mark.parametrize(
    "category,title,description",
    [
        (
            "paid_survey_participation",
            "Work at Home Data Entry Agent",
            "Earn at home by taking polls. Use our software and help companies collect data.",
        ),
        (
            "paid_survey_participation",
            "Customer Service Representative",
            "Complete paid questionnaires online. We use modern technology.",
        ),
        (
            "retail_store_operations",
            "Seasonal Key Holder",
            "Open and close the store. Work the register using POS software.",
        ),
        (
            "retail_store_operations",
            "Cashier",
            "Process retail transactions and help customers with purchases. Record sales data.",
        ),
        (
            "retail_store_operations",
            "Merchandising Specialist",
            "Replenish merchandise and stock shelves. Use inventory technology.",
        ),
        (
            "licensed_clinical_care",
            "Clinical Psychologist",
            "Provide psychological assessments and treat patients. Enter patient data into our software.",
        ),
        (
            "licensed_clinical_care",
            "Physician",
            "Provide bedside wound care. Use cutting-edge clinical technology.",
        ),
        (
            "licensed_clinical_care",
            "Psychiatrist",
            "Diagnose patients and prescribe medications.",
        ),
        (
            "food_service_operations",
            "Cookie Crew",
            "Bake cookies and prepare dough. Use our scheduling software.",
        ),
        (
            "food_service_operations",
            "Kitchen Assistant",
            "Assist with cooking classes. Record customer data.",
        ),
        (
            "food_service_operations",
            "Shift Lead",
            "Supervise restaurant staff and prepare food. Use automated ordering technology.",
        ),
    ],
)
def test_positive_occupations_with_generic_technology_mentions(
    category, title, description
):
    decision = screen_job(linked(title, description))
    assert decision.outcome is ScreeningOutcome.EXCLUDE
    assert category in decision.reasons
    assert any(
        e.field == "normalized.description" and e.value in description
        for e in decision.evidence
    )


@pytest.mark.parametrize(
    "title,description",
    [
        (
            "Survey Platform Software Engineer",
            "Build APIs for customers who complete paid surveys.",
        ),
        (
            "Survey Data Analyst",
            "Analyze survey responses; participants complete paid questionnaires.",
        ),
        (
            "Technical Support Specialist",
            "Help users who participate in paid surveys troubleshoot software.",
        ),
        (
            "Survey Statistician",
            "Design survey sampling methods; consumers complete paid questionnaires.",
        ),
        (
            "Retail POS Software Engineer",
            "Develop POS software. Cashiers work the register.",
        ),
        (
            "Retail IT Operations Specialist",
            "Configure network infrastructure while store staff process retail transactions.",
        ),
        (
            "Store Technical Support Specialist",
            "Provide technical support to cashiers who work the register.",
        ),
        (
            "Health Informatics Specialist",
            "Improve workflows for clinicians who treat patients.",
        ),
        (
            "Clinical Data Engineer",
            "Build data pipelines for doctors who provide patient care.",
        ),
        (
            "Healthcare Software Developer",
            "Develop software for teams that prescribe medications.",
        ),
        (
            "Clinical QA Test Engineer",
            "Develop automated tests for applications used to conduct psychological testing.",
        ),
        (
            "Restaurant Automation Engineer",
            "Maintain automated systems used to prepare food.",
        ),
        ("Bakery IT Support Specialist", "Support employees who bake cookies."),
        (
            "Food Technical Operations Engineer",
            "Support software where kitchen staff prepare meals.",
        ),
        (
            "Store Operations Specialist",
            "Build POS integrations for cashiers. Work the register as needed.",
        ),
    ],
)
def test_technical_counterexamples_remain_eligible(title, description):
    assert screen_job(linked(title, description)).outcome is ScreeningOutcome.KEEP


@pytest.mark.parametrize(
    "title,description",
    [
        ("Data Entry Agent", "Maintain records and answer customer questions."),
        (
            "Customer Service Representative",
            "Use our survey platform to resolve account issues.",
        ),
        ("Assistant Manager", "Coordinate operations. We are a retail company."),
        ("Clinical Practitioner", "Join our team and use our latest technology."),
        ("Shift Lead", "Coordinate team schedules and use the office kitchen."),
        ("Kitchen Assistant", "Provide administrative support for a food company."),
        ("Customer Service Representative", "You do not complete paid surveys."),
        ("Cashier", "You never process retail transactions."),
        ("Psychologist", "You do not treat patients."),
        ("Cookie Crew", "You never bake cookies."),
        ("Clinical Testing Psychologist", ""),
    ],
)
def test_ambiguous_or_negated_duties_do_not_exclude(title, description):
    assert not occupational_matches(title, description)
    assert screen_job(linked(title, description)).outcome is (
        ScreeningOutcome.KEEP if description else ScreeningOutcome.DEFER
    )


def test_clinical_testing_is_not_software_testing():
    decision = screen_job(
        linked(
            "Clinical Testing Psychologist",
            "Conduct psychological testing and evaluate patients.",
        )
    )
    assert "licensed_clinical_care" in decision.reasons


def test_categories_are_independent_and_keep_both_evidence_clauses():
    decision = screen_job(
        linked("Retail Shift Lead / Cookie Crew", "Work the register. Bake cookies.")
    )
    assert "retail_store_operations" in decision.reasons
    assert "food_service_operations" in decision.reasons
    assert {"Work the register", "Bake cookies"} <= {e.value for e in decision.evidence}
    evaluator = DeterministicFilterEvaluator(
        load_filter_policy("examples/filter_policy.example.toml")
    )
    result = screen_catalog(
        [
            (
                linked(
                    "Retail Shift Lead / Cookie Crew",
                    "Work the register. Bake cookies.",
                ),
                None,
            )
        ],
        evaluator,
    )
    summary = occupational_summary(result)
    assert (
        summary["pairwise_overlap_among_filter_eligible"][
            "retail_store_operations + food_service_operations"
        ]
        == 1
    )
    categories = summary["categories_in_funnel_order"]
    assert categories["retail_store_operations"]["incremental_exclusions_in_order"] == 1
    assert (
        categories["food_service_operations"][
            "incremental_exclusions_after_existing_screen"
        ]
        == 1
    )
    assert categories["food_service_operations"]["incremental_exclusions_in_order"] == 0


def test_plausible_representation_survives_occupational_exclusion():
    excluded = linked("Shift Lead", "Prepare food and supervise restaurant staff.")
    kept = linked("Automation Engineer", "Build automated systems.", number="2")
    evaluator = DeterministicFilterEvaluator(
        load_filter_policy("examples/filter_policy.example.toml")
    )
    result = screen_catalog([(excluded, None), (kept, None)], evaluator)
    assert result.logical_outcomes[kept.link.logical_job_id] is ScreeningOutcome.KEEP
    summary = occupational_summary(result)["categories_in_funnel_order"][
        "food_service_operations"
    ]
    assert summary["standalone_filter_eligible_logical_matches"] == 1
    assert summary["incremental_exclusions_after_existing_screen"] == 0
    deferred = replace(kept, normalized=replace(kept.normalized, description=""))
    result = screen_catalog([(excluded, None), (deferred, None)], evaluator)
    assert result.logical_outcomes[kept.link.logical_job_id] is ScreeningOutcome.DEFER


def test_travel_booking_is_outside_this_slice():
    assert (
        screen_job(
            linked(
                "Travel Reservations Assistant", "Book hotels and manage reservations."
            )
        ).outcome
        is ScreeningOutcome.KEEP
    )


@pytest.mark.parametrize(
    "description",
    [
        "To apply, complete online questionnaires. Earn extra income serving customers.",
        "Participate in surveys to help improve our workplace. Assist our customers.",
    ],
)
def test_application_or_employee_questionnaires_are_not_paid_survey_work(description):
    assert (
        screen_job(linked("Customer Service Representative", description)).outcome
        is ScreeningOutcome.KEEP
    )


@pytest.mark.parametrize(
    "title,description",
    [
        (
            "Customer Service Representative",
            "Provide technical support for survey software. Customers complete paid surveys.",
        ),
        (
            "Survey Specialist",
            "Analyze survey responses using SQL. Respondents complete paid questionnaires.",
        ),
        (
            "Retail Operations Associate",
            "Provide technical support for POS systems. Store associates work the register.",
        ),
    ],
)
def test_specific_technical_duties_protect_generic_titles(title, description):
    assert screen_job(linked(title, description)).outcome is ScreeningOutcome.KEEP


@pytest.mark.parametrize(
    "category,title,description",
    [
        (
            "paid_survey_participation",
            "Customer Service Rep",
            "We are looking for individuals to participate in paid surveys - Apply ASAP! Earn money taking surveys.",
        ),
        (
            "retail_store_operations",
            "Full-Time Assistant Manager",
            "Support your Store Manager in achieving the sales plan. Run sales reports and use data to hit sales targets.",
        ),
        (
            "licensed_clinical_care",
            "Clinical Psychologist",
            "Fully licensed and credentialed. Experienced with caring for adult and child populations. Latest digital technology.",
        ),
        (
            "licensed_clinical_care",
            "Nurse Care Manager",
            "Completes medication reconciliation for members with complex medical needs.",
        ),
        (
            "food_service_operations",
            "Culinary Lead",
            "Ensure an outstanding cooking class experience by following provided recipes.",
        ),
    ],
)
def test_catalog_duty_wording_regressions(category, title, description):
    assert category in screen_job(linked(title, description)).reasons


def test_clinical_talent_network_is_not_a_technical_network_job():
    decision = screen_job(
        linked(
            "Join Our Talent Network - Outpatient Psychiatrist",
            "Provide psychiatric evaluations and prescribe medications.",
        )
    )
    assert "licensed_clinical_care" in decision.reasons
