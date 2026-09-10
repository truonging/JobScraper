"""Tests for deterministic filter-policy contracts and TOML loading."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from job_matcher.catalog import LogicalJobId
from job_matcher.filter_policy import (
    FilterDecision,
    FilterField,
    FilterOutcome,
    FilterPolicyError,
    FilterReason,
    FilterReasonKind,
    TextFilterRules,
    load_filter_policy,
)
from job_matcher.models import SourceJobKey

EXAMPLE_POLICY = Path("examples/filter_policy.example.toml")
FINGERPRINT = "0" * 64


def valid_policy_text() -> str:
    return """\
version = 1

[title]
include_any = []
exclude_any = []

[location]
include_any = []
exclude_any = []

[company]
exclude = []

[description]
include_any = []
exclude_any = []
"""


def write_policy(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policy.toml"
    path.write_text(text, encoding="utf-8", newline="")
    return path


def test_loads_committed_synthetic_example() -> None:
    policy_bytes = EXAMPLE_POLICY.read_bytes()

    policy = load_filter_policy(EXAMPLE_POLICY)

    assert policy.version == 1
    assert policy.title.include_any == ("software engineer", "backend engineer")
    assert policy.company.exclude == ("Example Excluded Company",)
    assert policy.fingerprint == hashlib.sha256(policy_bytes).hexdigest()


def test_policy_fingerprint_changes_with_exact_file_contents(tmp_path: Path) -> None:
    first_path = write_policy(tmp_path, valid_policy_text())
    first = load_filter_policy(first_path)
    second_path = write_policy(tmp_path, valid_policy_text() + "\n")
    second = load_filter_policy(second_path)

    assert first.fingerprint != second.fingerprint


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("version = 1", "version = 2", "version 1"),
        ("version = 1", 'version = 1\nextra = "value"', "unknown keys"),
        ("[company]\nexclude = []", "[company]\n", "missing keys"),
        ("include_any = []", 'include_any = "python"', "array of strings"),
        ("include_any = []", 'include_any = ["  "]', "non-blank strings"),
    ],
)
def test_loader_rejects_invalid_policy(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    path = write_policy(tmp_path, valid_policy_text().replace(old, new, 1))

    with pytest.raises(FilterPolicyError, match=message):
        load_filter_policy(path)


def test_rule_contracts_require_immutable_non_blank_terms() -> None:
    with pytest.raises(FilterPolicyError, match="immutable tuple"):
        TextFilterRules(include_any=["python"])  # type: ignore[arg-type]
    with pytest.raises(FilterPolicyError, match="non-blank"):
        TextFilterRules(exclude_any=(" ",))


def test_eligible_decision_has_policy_and_source_provenance() -> None:
    decision = FilterDecision(
        logical_job_id=LogicalJobId(uuid4()),
        source_key=SourceJobKey("ashby", "example", "job-123"),
        evaluated_at=datetime(2026, 9, 9, tzinfo=UTC),
        policy_version=1,
        policy_fingerprint=FINGERPRINT,
        outcome=FilterOutcome.ELIGIBLE,
    )

    assert decision.reasons == ()


def test_rejected_decision_requires_structured_reason() -> None:
    reason = FilterReason(
        FilterField.TITLE,
        FilterReasonKind.EXCLUDE_MATCH,
        "director",
    )
    decision = FilterDecision(
        logical_job_id=LogicalJobId(uuid4()),
        source_key=SourceJobKey("greenhouse", "example", "job-123"),
        evaluated_at=datetime(2026, 9, 9, tzinfo=UTC),
        policy_version=1,
        policy_fingerprint=FINGERPRINT,
        outcome=FilterOutcome.REJECTED,
        reasons=(reason,),
    )

    assert decision.reasons == (reason,)


@pytest.mark.parametrize(
    ("outcome", "reasons", "message"),
    [
        (
            FilterOutcome.ELIGIBLE,
            (FilterReason(FilterField.TITLE, FilterReasonKind.EXCLUDE_MATCH, "x"),),
            "must not have reasons",
        ),
        (FilterOutcome.REJECTED, (), "at least one reason"),
    ],
)
def test_filter_decision_rejects_inconsistent_outcome(
    outcome: FilterOutcome, reasons: tuple[FilterReason, ...], message: str
) -> None:
    with pytest.raises(FilterPolicyError, match=message):
        FilterDecision(
            logical_job_id=LogicalJobId(uuid4()),
            source_key=SourceJobKey("lever", "example", "job-123"),
            evaluated_at=datetime(2026, 9, 9, tzinfo=UTC),
            policy_version=1,
            policy_fingerprint=FINGERPRINT,
            outcome=outcome,
            reasons=reasons,
        )


def test_exclude_reason_requires_matched_term() -> None:
    with pytest.raises(FilterPolicyError, match="identify its term"):
        FilterReason(FilterField.DESCRIPTION, FilterReasonKind.EXCLUDE_MATCH)


def test_filter_reason_requires_known_normalized_field() -> None:
    with pytest.raises(FilterPolicyError, match="FilterField"):
        FilterReason("raw_payload", FilterReasonKind.NO_INCLUDE_MATCH)  # type: ignore[arg-type]
