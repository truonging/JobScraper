"""High-recall geographic enforcement shared by production and standalone callers.

Assessment uncertainty is diagnostic; it never creates a Phase 2 DEFER.
Call for one source representation before existing logical-job aggregation.
"""

from dataclasses import dataclass
from enum import StrEnum

from job_matcher.geographic_classification import (
    GeographicAssessment,
    LocationConfidence,
)
from job_matcher.screening import ScreeningOutcome


class GeographicAction(StrEnum):
    EXCLUDE = "exclude"
    PASS = "pass"
    REVIEW_ONLY = "review_only"


@dataclass(frozen=True, slots=True)
class GeographicEnforcement:
    assessment: GeographicAssessment
    action: GeographicAction
    reason: str


# Fail toward review if a future classifier adds an unvalidated exclusion rule.
_VALIDATED_EXCLUSIONS = frozenset(
    {
        "US_employment_prohibited",
        "explicit_foreign_employee_location",
        "structured_foreign_work_location",
        "structured_foreign_remote_boundary",
        "corroborated_foreign_city",
    }
)


def geographic_enforcement(assessment: GeographicAssessment) -> GeographicEnforcement:
    """Translate location evidence into a proposed action, without persistence."""
    if assessment.conflicts or assessment.uncertainty_details:
        action = GeographicAction.REVIEW_ONLY
    elif assessment.outcome is ScreeningOutcome.KEEP:
        action = GeographicAction.PASS
    elif (
        assessment.outcome is ScreeningOutcome.EXCLUDE
        and assessment.confidence is LocationConfidence.HIGH
        and assessment.reason in _VALIDATED_EXCLUSIONS
        and not assessment.us_exceptions
    ):
        action = GeographicAction.EXCLUDE
    else:
        action = GeographicAction.REVIEW_ONLY
    return GeographicEnforcement(assessment, action, assessment.reason)


def apply_geographic_enforcement(
    existing_outcome: ScreeningOutcome, enforcement: GeographicEnforcement
) -> ScreeningOutcome:
    """Preserve the source's existing outcome unless geography safely excludes.

    Supply the non-geographic outcome, including deterministic/occupational
    exclusions, for the same posting as enforcement.assessment.source_key. PASS
    and REVIEW_ONLY never undo those exclusions or defer a retained job. Legacy
    geographic decisions must not be supplied as the existing outcome.
    """
    if not isinstance(existing_outcome, ScreeningOutcome):
        raise ValueError("existing_outcome must be a ScreeningOutcome")
    if enforcement.action is GeographicAction.EXCLUDE:
        return ScreeningOutcome.EXCLUDE
    return existing_outcome
