"""Source-independent deterministic filtering."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from job_matcher.catalog import LinkedSourceJob, LogicalJobId
from job_matcher.filter_policy import (
    FilterDecision,
    FilterField,
    FilterOutcome,
    FilterPolicy,
    FilterPolicyError,
    FilterReason,
    FilterReasonKind,
    TextFilterRules,
)
from job_matcher.models import ContractValidationError


class DeterministicFilterEvaluator:
    """Apply one deterministic policy to supplied linked source postings."""

    def __init__(
        self,
        policy: FilterPolicy,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(policy, FilterPolicy):
            raise FilterPolicyError("policy must be a FilterPolicy")
        self._policy = policy
        self._clock = clock or _utc_now

    def evaluate(self, jobs: Sequence[LinkedSourceJob]) -> tuple[FilterDecision, ...]:
        """Return one decision per supplied posting, preserving input order."""
        evaluated_at = self._clock()
        if not isinstance(evaluated_at, datetime) or evaluated_at.tzinfo is not UTC:
            raise ContractValidationError(
                "filter evaluation clock must return a datetime using datetime.UTC"
            )
        return tuple(self._evaluate_job(job, evaluated_at) for job in jobs)

    def _evaluate_job(
        self, job: LinkedSourceJob, evaluated_at: datetime
    ) -> FilterDecision:
        if not isinstance(job, LinkedSourceJob):
            raise ContractValidationError("jobs must contain LinkedSourceJob records")

        normalized = job.normalized
        reasons = (
            *_text_reasons(normalized.title, self._policy.title, FilterField.TITLE),
            *_text_reasons(
                normalized.location or "",
                self._policy.location,
                FilterField.LOCATION,
            ),
            *_company_reasons(normalized.company, self._policy.company.exclude),
            *_text_reasons(
                normalized.description,
                self._policy.description,
                FilterField.DESCRIPTION,
            ),
        )
        outcome = FilterOutcome.REJECTED if reasons else FilterOutcome.ELIGIBLE
        return FilterDecision(
            logical_job_id=job.link.logical_job_id,
            source_key=normalized.key,
            evaluated_at=evaluated_at,
            policy_version=self._policy.version,
            policy_fingerprint=self._policy.fingerprint,
            outcome=outcome,
            reasons=reasons,
        )


def eligible_logical_job_ids(
    decisions: Sequence[FilterDecision],
) -> tuple[LogicalJobId, ...]:
    """Return logical jobs with any eligible supplied source representation."""
    eligible: list[LogicalJobId] = []
    seen: set[LogicalJobId] = set()
    for decision in decisions:
        if not isinstance(decision, FilterDecision):
            raise ContractValidationError(
                "decisions must contain FilterDecision records"
            )
        logical_job_id = decision.logical_job_id
        if decision.outcome is FilterOutcome.ELIGIBLE and logical_job_id not in seen:
            eligible.append(logical_job_id)
            seen.add(logical_job_id)
    return tuple(eligible)


def _text_reasons(
    value: str,
    rules: TextFilterRules,
    field: FilterField,
) -> tuple[FilterReason, ...]:
    normalized_value = _normalize_text(value)
    reasons = [
        FilterReason(field, FilterReasonKind.EXCLUDE_MATCH, term)
        for term in rules.exclude_any
        if _normalize_text(term) in normalized_value
    ]
    if rules.include_any and not any(
        _normalize_text(term) in normalized_value for term in rules.include_any
    ):
        reasons.append(FilterReason(field, FilterReasonKind.NO_INCLUDE_MATCH))
    return tuple(reasons)


def _company_reasons(
    company: str, excluded_companies: tuple[str, ...]
) -> tuple[FilterReason, ...]:
    normalized_company = _normalize_text(company)
    return tuple(
        FilterReason(FilterField.COMPANY, FilterReasonKind.EXCLUDE_MATCH, term)
        for term in excluded_companies
        if _normalize_text(term) == normalized_company
    )


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _utc_now() -> datetime:
    return datetime.now(UTC)
