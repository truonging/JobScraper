"""Small, evidence-bearing pre-AI screen; never mutates source facts."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from job_matcher.catalog import LinkedSourceJob, LogicalJobId
from job_matcher.filter_policy import FilterDecision, FilterOutcome
from job_matcher.models import ContractValidationError, RawSourceRecord, SourceJobKey
from job_matcher.occupational_screening import occupational_matches

SCREENING_VERSION = "5"


class ScreeningOutcome(StrEnum):
    KEEP = "keep"
    EXCLUDE = "exclude"
    DEFER = "defer"


_OUTCOME_PRIORITY = {
    ScreeningOutcome.EXCLUDE: 0,
    ScreeningOutcome.DEFER: 1,
    ScreeningOutcome.KEEP: 2,
}


def outcome_priority(outcome: ScreeningOutcome) -> int:
    """Rank a valid outcome; unknown values raise KeyError as in aggregation."""
    return _OUTCOME_PRIORITY[outcome]


def aggregate_outcomes(outcomes: Iterable[ScreeningOutcome]) -> ScreeningOutcome:
    """Aggregate exactly the supplied outcomes; callers select lifecycle scope.

    Empty groups are EXCLUDE. No inactive representation is selected implicitly.
    """
    return max(outcomes, key=outcome_priority, default=ScreeningOutcome.EXCLUDE)


def combine_outcomes(
    filtered: FilterOutcome, screened: ScreeningOutcome
) -> ScreeningOutcome:
    """A deterministic rejection overrides screening; eligibility preserves it."""
    if not isinstance(filtered, FilterOutcome) or not isinstance(
        screened, ScreeningOutcome
    ):
        raise ValueError("Expected FilterOutcome and ScreeningOutcome")
    return ScreeningOutcome.EXCLUDE if filtered is FilterOutcome.REJECTED else screened


class Geography(StrEnum):
    US = "us"
    US_REMOTE = "us_remote"
    FOREIGN_ONLY = "foreign_only"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class Evidence:
    field: str
    value: str


@dataclass(frozen=True, slots=True)
class ScreeningDecision:
    logical_job_id: LogicalJobId
    source_key: SourceJobKey
    source_retrieved_at: datetime
    evaluated_at: datetime
    geography: Geography
    outcome: ScreeningOutcome
    reasons: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    version: str = SCREENING_VERSION


def combine_decisions(
    filtered: FilterDecision, screened: ScreeningDecision
) -> ScreeningDecision:
    """Project the effective outcome without changing either decision's facts.

    Callers pair decisions for the same representation. Filter reasons stay in
    the separate FilterDecision; screening reasons, evidence and provenance stay
    in the ScreeningDecision, including when filtering overrides its outcome.
    """
    outcome = combine_outcomes(filtered.outcome, screened.outcome)
    return (
        screened if outcome is screened.outcome else replace(screened, outcome=outcome)
    )


_DEMO = re.compile(
    r"(?:these jobs are testing jobs of lever.s testing environment|this (?:job|posting) is (?:a |an )?(?:test|demo) (?:job|posting)|this (?:job|posting) is (?:part of |in )?(?:a |the )?testing environment|this is (?:a |an )(?:test|demo) (?:job|posting)).{0,200}?(?:do not|don't) apply",
    re.I,
)


def screen_job(
    linked: LinkedSourceJob, raw: RawSourceRecord | None = None
) -> ScreeningDecision:
    """Screen one representation using H geography and unchanged non-geographic rules."""
    # H imports these screening contracts. Resolve the callable imports after
    # this module has loaded, without moving contracts or duplicating the path.
    from job_matcher.geographic_classification import classify_geography
    from job_matcher.geographic_enforcement import (
        GeographicAction,
        apply_geographic_enforcement,
        geographic_enforcement,
    )

    job = linked.normalized
    if raw is not None and (raw.key != job.key or raw.retrieved_at != job.retrieved_at):
        raise ContractValidationError(
            "screening raw evidence must match key and retrieval"
        )
    description = " ".join(job.description.split())
    evidence = [
        Evidence("normalized.location", job.location or ""),
        Evidence("normalized.title", job.title),
    ]
    reasons = []
    demo = _DEMO.search(description)
    if demo:
        reasons.append("explicit_testing_do_not_apply")
        evidence.append(Evidence("normalized.description", demo.group()))
    missing = not description or description.casefold() in {
        "n/a",
        "tbd",
        "[insert general job description here]",
    }
    if missing:
        reasons.append("insufficient_description")
        evidence.append(Evidence("normalized.description", description))
    occupations = occupational_matches(job.title, job.description)
    for category, (title_match, *duties) in occupations.items():
        reasons.append(category)
        evidence.append(Evidence("normalized.title", title_match))
        evidence.extend(Evidence("normalized.description", duty) for duty in duties)
    outcome = (
        ScreeningOutcome.EXCLUDE
        if demo or occupations
        else ScreeningOutcome.DEFER
        if missing
        else ScreeningOutcome.KEEP
    )
    assessment = classify_geography(job, raw)
    enforcement = geographic_enforcement(assessment)
    outcome = apply_geographic_enforcement(outcome, enforcement)
    # This field is an H-derived summary, never a second classifier.
    geo = (
        Geography.FOREIGN_ONLY
        if enforcement.action is GeographicAction.EXCLUDE
        else (Geography.US_REMOTE if assessment.work_mode == "remote" else Geography.US)
        if enforcement.action is GeographicAction.PASS
        else Geography.UNCERTAIN
    )
    reasons.append(f"geo_h:{enforcement.action.value}:{assessment.reason}")
    evidence.extend(
        (
            Evidence("geo_h.action", enforcement.action.value),
            Evidence("geo_h.assessment", assessment.outcome.value),
            Evidence("geo_h.reason", assessment.reason),
            Evidence("geo_h.confidence", assessment.confidence.value),
            Evidence("geo_h.work_mode", assessment.work_mode),
        )
    )
    evidence.extend(Evidence(f"geo_h.{e.field}", e.value) for e in assessment.evidence)
    evidence.extend(
        Evidence(f"geo_h.us_exception.{e.field}", e.value)
        for e in assessment.us_exceptions
    )
    evidence.extend(
        Evidence(f"geo_h.foreign_restriction.{e.field}", e.value)
        for e in assessment.foreign_restrictions
    )
    evidence.extend(Evidence("geo_h.country", c) for c in assessment.countries)
    evidence.extend(Evidence("geo_h.region", r) for r in assessment.regions)
    evidence.extend(Evidence("geo_h.conflict", c) for c in assessment.conflicts)
    evidence.extend(
        Evidence("geo_h.uncertainty", u) for u in assessment.uncertainty_details
    )
    return ScreeningDecision(
        linked.link.logical_job_id,
        job.key,
        job.retrieved_at,
        datetime.now(UTC),
        geo,
        outcome,
        tuple(reasons),
        tuple(evidence),
    )


def aggregate_screening(
    decisions: Iterable[ScreeningDecision],
) -> dict[LogicalJobId, ScreeningOutcome]:
    """Group explicitly supplied decisions; lifecycle selection belongs to callers."""
    result: dict[LogicalJobId, ScreeningOutcome] = {}
    for decision in decisions:
        previous = result.get(decision.logical_job_id, ScreeningOutcome.EXCLUDE)
        result[decision.logical_job_id] = aggregate_outcomes(
            (previous, decision.outcome)
        )
    return result
