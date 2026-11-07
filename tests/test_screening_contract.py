"""Shared effective decisions and lifecycle-neutral aggregation contracts."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from itertools import product
from uuid import UUID

import pytest

from job_matcher.catalog import LogicalJobId
from job_matcher.filter_policy import (
    FilterDecision,
    FilterField,
    FilterOutcome,
    FilterReason,
    FilterReasonKind,
)
from job_matcher.models import SourceJobKey
from job_matcher.screening import (
    Evidence,
    Geography,
    ScreeningDecision,
    ScreeningOutcome,
    aggregate_outcomes,
    aggregate_screening,
    combine_decisions,
    combine_outcomes,
    outcome_priority,
)

NOW = datetime(2026, 11, 6, tzinfo=UTC)
ID = LogicalJobId(UUID(int=1))
KEY = SourceJobKey("lever", "contract", "1")


def decision(outcome):
    return ScreeningDecision(
        ID,
        KEY,
        NOW,
        NOW,
        Geography.UNCERTAIN,
        outcome,
        ("insufficient_description", "geo_h:review_only:uncertain"),
        (Evidence("normalized.description", ""),),
    )


@pytest.mark.parametrize(
    "filtered,screened", list(product(FilterOutcome, ScreeningOutcome))
)
def test_every_combination_preserves_separate_facts(filtered, screened):
    expected = (
        ScreeningOutcome.EXCLUDE if filtered is FilterOutcome.REJECTED else screened
    )
    filter_decision = FilterDecision(
        ID,
        KEY,
        NOW,
        1,
        "a" * 64,
        filtered,
        (FilterReason(FilterField.TITLE, FilterReasonKind.EXCLUDE_MATCH, "Blocked"),)
        if filtered is FilterOutcome.REJECTED
        else (),
    )
    screened_decision = decision(screened)
    original_filter, original_screen = (
        asdict(filter_decision),
        asdict(screened_decision),
    )
    assert combine_outcomes(filtered, screened) is expected
    combined = combine_decisions(filter_decision, screened_decision)
    assert combined == replace(screened_decision, outcome=expected)
    assert combined.source_key == KEY and combined.logical_job_id == ID
    assert combined.reasons is screened_decision.reasons
    assert combined.evidence is screened_decision.evidence
    assert combined.version == "5"
    assert asdict(filter_decision) == original_filter
    assert asdict(screened_decision) == original_screen


@pytest.mark.parametrize(
    "filtered,screened",
    [
        (None, ScreeningOutcome.KEEP),
        ("eligible", ScreeningOutcome.KEEP),
        ("unknown", ScreeningOutcome.EXCLUDE),
        (FilterOutcome.ELIGIBLE, None),
        (FilterOutcome.REJECTED, "keep"),
        (FilterOutcome.ELIGIBLE, "unknown"),
    ],
)
def test_invalid_combination_statuses_fail(filtered, screened):
    with pytest.raises(ValueError, match="Expected"):
        combine_outcomes(filtered, screened)


@pytest.mark.parametrize(
    "outcomes",
    [group for size in range(4) for group in product(ScreeningOutcome, repeat=size)],
)
def test_aggregation_all_groups_and_orders(outcomes):
    # Independent baseline rule, deliberately not derived from the production rank.
    expected = (
        ScreeningOutcome.KEEP
        if ScreeningOutcome.KEEP in outcomes
        else ScreeningOutcome.DEFER
        if ScreeningOutcome.DEFER in outcomes
        else ScreeningOutcome.EXCLUDE
    )
    assert aggregate_outcomes(iter(outcomes)) is expected
    grouped = aggregate_screening(decision(outcome) for outcome in outcomes)
    assert grouped == ({ID: expected} if outcomes else {})


@pytest.mark.parametrize("bad", [None, "unknown", "KEEP", 9])
def test_invalid_aggregation_retains_lookup_failure(bad):
    with pytest.raises(KeyError):
        aggregate_outcomes([ScreeningOutcome.KEEP, bad])
    with pytest.raises(KeyError):
        aggregate_screening([decision(bad)])


def test_priority_and_string_compatibility():
    assert [
        outcome_priority(s)
        for s in (
            ScreeningOutcome.EXCLUDE,
            ScreeningOutcome.DEFER,
            ScreeningOutcome.KEEP,
        )
    ] == [0, 1, 2]
    # StrEnum lookup compatibility existed in aggregate_screening's original map.
    assert aggregate_outcomes(["exclude", "keep"]) == "keep"


def test_lifecycle_scope_is_explicit_and_groups_remain_separate():
    active = decision(ScreeningOutcome.EXCLUDE)
    inactive = replace(
        decision(ScreeningOutcome.KEEP), source_key=replace(KEY, source_job_id="old")
    )
    other = replace(
        decision(ScreeningOutcome.DEFER), logical_job_id=LogicalJobId(UUID(int=2))
    )
    assert aggregate_screening([active, other]) == {
        ID: ScreeningOutcome.EXCLUDE,
        other.logical_job_id: ScreeningOutcome.DEFER,
    }
    # The function has no lifecycle input and must not guess which records to omit.
    assert aggregate_screening([inactive])[ID] is ScreeningOutcome.KEEP
    assert aggregate_screening([active, inactive])[ID] is ScreeningOutcome.KEEP


def test_occupational_mask_keeps_original_decision_and_provenance():
    from job_matcher.screening_pipeline import ScreenedCatalog, occupational_summary

    screened = replace(
        decision(ScreeningOutcome.EXCLUDE), reasons=("food_service_operations",)
    )
    filtered = FilterDecision(ID, KEY, NOW, 1, "a" * 64, FilterOutcome.ELIGIBLE)
    catalog = ScreenedCatalog((filtered,), (screened,), {ID: ScreeningOutcome.EXCLUDE})
    original = asdict(screened)
    report = occupational_summary(catalog)
    assert report["baseline_outcomes_among_filter_eligible"] == {
        ScreeningOutcome.KEEP: 1
    }
    assert (
        report["categories_in_funnel_order"]["food_service_operations"][
            "incremental_exclusions_in_order"
        ]
        == 1
    )
    assert asdict(catalog.screening_decisions[0]) == original
