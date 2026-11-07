"""Coordinate deterministic filtering and screening of persisted active inputs."""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from itertools import combinations

from job_matcher.catalog import LinkedSourceJob, LogicalJobId
from job_matcher.filter_policy import FilterDecision, FilterOutcome
from job_matcher.filtering import DeterministicFilterEvaluator
from job_matcher.models import RawSourceRecord
from job_matcher.occupational_screening import OCCUPATIONAL_REASONS
from job_matcher.screening import (
    Geography,
    ScreeningDecision,
    ScreeningOutcome,
    aggregate_screening,
    combine_decisions,
    combine_outcomes,
    screen_job,
)


@dataclass(frozen=True, slots=True)
class ScreenedCatalog:
    filter_decisions: tuple[FilterDecision, ...]
    screening_decisions: tuple[ScreeningDecision, ...]
    logical_outcomes: dict[LogicalJobId, ScreeningOutcome]


def screen_catalog(
    active_inputs: Iterable[tuple[LinkedSourceJob, RawSourceRecord | None]],
    evaluator: DeterministicFilterEvaluator,
) -> ScreenedCatalog:
    """Evaluate each representation before aggregating; never resolve identities."""
    filters = []
    screens = []
    effective = []
    for linked, raw in active_inputs:
        filtered = evaluator.evaluate((linked,))[0]
        screened = screen_job(linked, raw)
        filters.append(filtered)
        screens.append(screened)
        effective.append(combine_decisions(filtered, screened))
    return ScreenedCatalog(
        tuple(filters), tuple(screens), aggregate_screening(effective)
    )


def occupational_summary(result: ScreenedCatalog) -> dict:
    """Report measured ID sets and a funnel, preserving representation semantics.

    Stages mask category reasons on already computed decisions. They never
    reevaluate source data or change stored screening results.
    """
    eligible = {
        f.logical_job_id
        for f in result.filter_decisions
        if f.outcome is FilterOutcome.ELIGIBLE
    }
    matches = {
        category: {
            s.logical_job_id
            for f, s in zip(result.filter_decisions, result.screening_decisions)
            if f.outcome is FilterOutcome.ELIGIBLE and category in s.reasons
        }
        for category in OCCUPATIONAL_REASONS
    }

    def stage(categories: set[str]) -> dict[LogicalJobId, ScreeningOutcome]:
        decisions = []
        for filtered, screened in zip(
            result.filter_decisions, result.screening_decisions
        ):
            excluded = (
                screened.geography is Geography.FOREIGN_ONLY
                or "explicit_testing_do_not_apply" in screened.reasons
                or bool(categories.intersection(screened.reasons))
            )
            outcome = (
                ScreeningOutcome.EXCLUDE
                if excluded
                else ScreeningOutcome.DEFER
                if "insufficient_description" in screened.reasons
                else ScreeningOutcome.KEEP
            )
            decisions.append(
                replace(screened, outcome=combine_outcomes(filtered.outcome, outcome))
            )
        return aggregate_screening(decisions)

    baseline = stage(set())
    previous = baseline
    cumulative: set[str] = set()
    categories = {}
    for category in OCCUPATIONAL_REASONS:
        alone = stage({category})
        cumulative.add(category)
        current = stage(cumulative)
        categories[category] = {
            "standalone_active_logical_matches": len(
                {
                    s.logical_job_id
                    for s in result.screening_decisions
                    if category in s.reasons
                }
            ),
            "standalone_filter_eligible_logical_matches": len(matches[category]),
            "incremental_exclusions_after_existing_screen": sum(
                alone[k] is ScreeningOutcome.EXCLUDE
                and baseline[k] is not ScreeningOutcome.EXCLUDE
                for k in eligible
            ),
            "incremental_exclusions_in_order": sum(
                current[k] is ScreeningOutcome.EXCLUDE
                and previous[k] is not ScreeningOutcome.EXCLUDE
                for k in eligible
            ),
            "remaining_keep": sum(
                current[k] is ScreeningOutcome.KEEP for k in eligible
            ),
            "remaining_defer": sum(
                current[k] is ScreeningOutcome.DEFER for k in eligible
            ),
        }
        previous = current
    assert previous == result.logical_outcomes
    return {
        "baseline_outcomes_among_filter_eligible": Counter(
            baseline[k] for k in eligible
        ),
        "categories_in_funnel_order": categories,
        "pairwise_overlap_among_filter_eligible": {
            f"{a} + {b}": len(matches[a] & matches[b])
            for a, b in combinations(OCCUPATIONAL_REASONS, 2)
        },
    }
