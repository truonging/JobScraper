"""Tests for source-independent deterministic filtering."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from job_matcher.catalog import LinkedSourceJob, LogicalJobId, SourceJobLink
from job_matcher.filter_policy import (
    CompanyFilterRules,
    FilterField,
    FilterOutcome,
    FilterPolicy,
    FilterReasonKind,
    TextFilterRules,
)
from job_matcher.filtering import (
    DeterministicFilterEvaluator,
    eligible_logical_job_ids,
)
from job_matcher.models import ContractValidationError, NormalizedJob, SourceJobKey

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
FINGERPRINT = "0" * 64
LOGICAL_ID_1 = LogicalJobId(UUID("00000000-0000-4000-8000-000000000001"))
LOGICAL_ID_2 = LogicalJobId(UUID("00000000-0000-4000-8000-000000000002"))


def make_policy(
    *,
    title_include: tuple[str, ...] = (),
    title_exclude: tuple[str, ...] = (),
    location_include: tuple[str, ...] = (),
    location_exclude: tuple[str, ...] = (),
    company_exclude: tuple[str, ...] = (),
    description_include: tuple[str, ...] = (),
    description_exclude: tuple[str, ...] = (),
) -> FilterPolicy:
    return FilterPolicy(
        version=1,
        title=TextFilterRules(title_include, title_exclude),
        location=TextFilterRules(location_include, location_exclude),
        company=CompanyFilterRules(company_exclude),
        description=TextFilterRules(description_include, description_exclude),
        fingerprint=FINGERPRINT,
    )


def make_linked_job(
    source: str = "lever",
    *,
    source_job_id: str = "job-1",
    logical_job_id: LogicalJobId = LOGICAL_ID_1,
    company: str = "Example Company",
    title: str = "Backend Software Engineer",
    location: str | None = "Remote, California",
    description: str = "Build reliable Python services.",
) -> LinkedSourceJob:
    key = SourceJobKey(source, "example", source_job_id)
    normalized = NormalizedJob(
        key=key,
        company=company,
        title=title,
        location=location,
        description=description,
        job_url=f"https://jobs.example/{source_job_id}",
        retrieved_at=NOW,
    )
    return LinkedSourceJob(
        normalized=normalized,
        link=SourceJobLink(key, logical_job_id, NOW),
    )


def evaluate(job: LinkedSourceJob, policy: FilterPolicy) -> FilterOutcome:
    return (
        DeterministicFilterEvaluator(policy, clock=lambda: NOW)
        .evaluate((job,))[0]
        .outcome
    )


def test_empty_inclusion_groups_impose_no_requirement() -> None:
    assert evaluate(make_linked_job(), make_policy()) is FilterOutcome.ELIGIBLE


def test_each_nonempty_inclusion_group_is_independently_required() -> None:
    policy = make_policy(
        title_include=("  BACKEND   SOFTWARE engineer ",),
        location_include=("remote",),
        description_include=("PYTHON",),
    )

    assert evaluate(make_linked_job(), policy) is FilterOutcome.ELIGIBLE
    decision = DeterministicFilterEvaluator(policy, clock=lambda: NOW).evaluate(
        (make_linked_job(location="New York"),)
    )[0]

    assert decision.outcome is FilterOutcome.REJECTED
    assert len(decision.reasons) == 1
    assert decision.reasons[0].field is FilterField.LOCATION
    assert decision.reasons[0].kind is FilterReasonKind.NO_INCLUDE_MATCH


def test_literal_exclusions_and_failed_includes_have_deterministic_reasons() -> None:
    policy = make_policy(
        title_include=("frontend",),
        title_exclude=("senior", "backend"),
        location_exclude=("california",),
        company_exclude=(" example   company ",),
        description_include=("rust",),
        description_exclude=("python",),
    )

    decision = DeterministicFilterEvaluator(policy, clock=lambda: NOW).evaluate(
        (make_linked_job(title="Senior Backend Engineer"),)
    )[0]

    assert decision.outcome is FilterOutcome.REJECTED
    assert [
        (reason.field, reason.kind, reason.matched_term) for reason in decision.reasons
    ] == [
        (FilterField.TITLE, FilterReasonKind.EXCLUDE_MATCH, "senior"),
        (FilterField.TITLE, FilterReasonKind.EXCLUDE_MATCH, "backend"),
        (FilterField.TITLE, FilterReasonKind.NO_INCLUDE_MATCH, None),
        (FilterField.LOCATION, FilterReasonKind.EXCLUDE_MATCH, "california"),
        (
            FilterField.COMPANY,
            FilterReasonKind.EXCLUDE_MATCH,
            " example   company ",
        ),
        (FilterField.DESCRIPTION, FilterReasonKind.EXCLUDE_MATCH, "python"),
        (FilterField.DESCRIPTION, FilterReasonKind.NO_INCLUDE_MATCH, None),
    ]


def test_company_exclusion_requires_exact_normalized_equality() -> None:
    policy = make_policy(company_exclude=("Example",))

    assert evaluate(make_linked_job(), policy) is FilterOutcome.ELIGIBLE
    assert (
        evaluate(make_linked_job(company="  EXAMPLE  "), policy)
        is FilterOutcome.REJECTED
    )


def test_missing_location_is_treated_as_empty_text() -> None:
    job = make_linked_job(location=None)

    assert evaluate(job, make_policy()) is FilterOutcome.ELIGIBLE
    decision = DeterministicFilterEvaluator(
        make_policy(location_include=("remote",)), clock=lambda: NOW
    ).evaluate((job,))[0]

    assert decision.outcome is FilterOutcome.REJECTED
    assert decision.reasons[0].field is FilterField.LOCATION
    assert decision.reasons[0].kind is FilterReasonKind.NO_INCLUDE_MATCH


def test_decisions_preserve_input_order_and_complete_provenance() -> None:
    policy = make_policy()
    first = make_linked_job("ashby", source_job_id="first")
    second = make_linked_job("greenhouse", source_job_id="second")

    decisions = DeterministicFilterEvaluator(policy, clock=lambda: NOW).evaluate(
        (first, second)
    )

    assert [decision.source_key for decision in decisions] == [
        first.normalized.key,
        second.normalized.key,
    ]
    assert all(decision.logical_job_id == LOGICAL_ID_1 for decision in decisions)
    assert all(decision.evaluated_at == NOW for decision in decisions)
    assert all(decision.policy_version == 1 for decision in decisions)
    assert all(decision.policy_fingerprint == FINGERPRINT for decision in decisions)


def test_clock_is_read_once_for_the_entire_batch() -> None:
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return NOW

    decisions = DeterministicFilterEvaluator(make_policy(), clock=clock).evaluate(
        (make_linked_job(), make_linked_job(source_job_id="job-2"))
    )

    assert calls == 1
    assert all(decision.evaluated_at is NOW for decision in decisions)


@pytest.mark.parametrize(
    "invalid_time",
    [datetime(2026, 9, 9, 12, 0), NOW.astimezone(timezone(timedelta(hours=1)))],
)
def test_clock_must_return_an_explicit_utc_datetime(invalid_time: datetime) -> None:
    evaluator = DeterministicFilterEvaluator(make_policy(), clock=lambda: invalid_time)

    with pytest.raises(ContractValidationError, match="datetime.UTC"):
        evaluator.evaluate((make_linked_job(),))


@pytest.mark.parametrize("source", ["lever", "ashby", "greenhouse"])
def test_evaluation_is_source_independent(source: str) -> None:
    policy = make_policy(title_include=("software",), description_include=("python",))

    decision = DeterministicFilterEvaluator(policy, clock=lambda: NOW).evaluate(
        (make_linked_job(source),)
    )[0]

    assert decision.outcome is FilterOutcome.ELIGIBLE
    assert decision.source_key.source == source


def test_logical_job_is_eligible_when_any_supplied_representation_is_eligible() -> None:
    policy = make_policy(description_include=("python",))
    rejected = make_linked_job(
        "lever",
        source_job_id="rejected",
        description="Build Java services.",
    )
    eligible = make_linked_job(
        "ashby",
        source_job_id="eligible",
        description="Build Python services.",
    )
    other_rejected = make_linked_job(
        "greenhouse",
        source_job_id="other",
        logical_job_id=LOGICAL_ID_2,
        description="Build Java services.",
    )
    decisions = DeterministicFilterEvaluator(policy, clock=lambda: NOW).evaluate(
        (rejected, eligible, other_rejected)
    )

    assert eligible_logical_job_ids(decisions) == (LOGICAL_ID_1,)
