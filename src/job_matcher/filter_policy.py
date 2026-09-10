"""Contracts and TOML loading for deterministic filtering policies."""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from job_matcher.catalog import LogicalJobId
from job_matcher.models import SourceJobKey


class FilterPolicyError(ValueError):
    """Raised when a filter policy does not satisfy the version 1 schema."""


def _require_terms(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise FilterPolicyError(f"{field_name} must be an array of strings")
    terms: list[str] = []
    for term in value:
        if not isinstance(term, str) or not term.strip():
            raise FilterPolicyError(f"{field_name} must contain only non-blank strings")
        terms.append(term)
    return tuple(terms)


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise FilterPolicyError(f"{field_name} must be a non-blank string")


def _require_utc(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise FilterPolicyError(f"{field_name} must be timezone-aware")
    if value.tzinfo is not UTC:
        raise FilterPolicyError(f"{field_name} must use datetime.UTC")


@dataclass(frozen=True, slots=True)
class TextFilterRules:
    """Literal substring rules for one normalized text field."""

    include_any: tuple[str, ...] = ()
    exclude_any: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_term_tuple(self.include_any, "include_any")
        _validate_term_tuple(self.exclude_any, "exclude_any")


@dataclass(frozen=True, slots=True)
class CompanyFilterRules:
    """Exact normalized company names to reject."""

    exclude: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_term_tuple(self.exclude, "exclude")


def _validate_term_tuple(value: object, field_name: str) -> None:
    if not isinstance(value, tuple):
        raise FilterPolicyError(f"{field_name} must be an immutable tuple")
    for term in value:
        if not isinstance(term, str) or not term.strip():
            raise FilterPolicyError(f"{field_name} must contain only non-blank strings")


@dataclass(frozen=True, slots=True)
class FilterPolicy:
    """Validated version 1 deterministic filter policy."""

    version: int
    title: TextFilterRules
    location: TextFilterRules
    company: CompanyFilterRules
    description: TextFilterRules
    fingerprint: str

    def __post_init__(self) -> None:
        if self.version != 1 or isinstance(self.version, bool):
            raise FilterPolicyError("only filter policy version 1 is supported")
        if not isinstance(self.title, TextFilterRules):
            raise FilterPolicyError("title must be TextFilterRules")
        if not isinstance(self.location, TextFilterRules):
            raise FilterPolicyError("location must be TextFilterRules")
        if not isinstance(self.company, CompanyFilterRules):
            raise FilterPolicyError("company must be CompanyFilterRules")
        if not isinstance(self.description, TextFilterRules):
            raise FilterPolicyError("description must be TextFilterRules")
        _require_sha256(self.fingerprint, "fingerprint")


class FilterOutcome(StrEnum):
    """Deterministic filtering result for a logical job."""

    ELIGIBLE = "eligible"
    REJECTED = "rejected"


class FilterField(StrEnum):
    """Normalized field responsible for a filter reason."""

    TITLE = "title"
    LOCATION = "location"
    COMPANY = "company"
    DESCRIPTION = "description"


class FilterReasonKind(StrEnum):
    """Kind of deterministic policy failure."""

    NO_INCLUDE_MATCH = "no_include_match"
    EXCLUDE_MATCH = "exclude_match"


@dataclass(frozen=True, slots=True)
class FilterReason:
    """Structured explanation for rejecting a normalized posting."""

    field: FilterField
    kind: FilterReasonKind
    matched_term: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.field, FilterField):
            raise FilterPolicyError("field must be a FilterField")
        if self.kind is FilterReasonKind.EXCLUDE_MATCH:
            if self.matched_term is None:
                raise FilterPolicyError("an exclude match must identify its term")
            _require_non_blank(self.matched_term, "matched_term")
        elif self.kind is FilterReasonKind.NO_INCLUDE_MATCH:
            if self.matched_term is not None:
                raise FilterPolicyError(
                    "a missing include match must not identify a matched term"
                )
        else:
            raise FilterPolicyError("kind must be a FilterReasonKind")


@dataclass(frozen=True, slots=True)
class FilterDecision:
    """Provenance-bearing deterministic result for one normalized posting."""

    logical_job_id: LogicalJobId
    source_key: SourceJobKey
    evaluated_at: datetime
    policy_version: int
    policy_fingerprint: str
    outcome: FilterOutcome
    reasons: tuple[FilterReason, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.logical_job_id, LogicalJobId):
            raise FilterPolicyError("logical_job_id must be a LogicalJobId")
        if not isinstance(self.source_key, SourceJobKey):
            raise FilterPolicyError("source_key must be a SourceJobKey")
        _require_utc(self.evaluated_at, "evaluated_at")
        if self.policy_version != 1 or isinstance(self.policy_version, bool):
            raise FilterPolicyError("only filter policy version 1 is supported")
        _require_sha256(self.policy_fingerprint, "policy_fingerprint")
        if not isinstance(self.reasons, tuple):
            raise FilterPolicyError("reasons must be an immutable tuple")
        if not isinstance(self.outcome, FilterOutcome):
            raise FilterPolicyError("outcome must be a FilterOutcome")
        if not all(isinstance(reason, FilterReason) for reason in self.reasons):
            raise FilterPolicyError("reasons must contain FilterReason records")
        if self.outcome is FilterOutcome.ELIGIBLE and self.reasons:
            raise FilterPolicyError("an eligible decision must not have reasons")
        if self.outcome is FilterOutcome.REJECTED and not self.reasons:
            raise FilterPolicyError("a rejected decision must have at least one reason")


def _require_sha256(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FilterPolicyError(f"{field_name} must be a lowercase SHA-256 digest")


def load_filter_policy(path: str | Path) -> FilterPolicy:
    """Load and strictly validate a version 1 TOML filter policy."""
    policy_bytes = Path(path).read_bytes()
    try:
        document = tomllib.loads(policy_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise FilterPolicyError("filter policy must be valid UTF-8 TOML") from error

    expected_sections = {"version", "title", "location", "company", "description"}
    _require_exact_keys(document, expected_sections, "filter policy")
    version = document["version"]
    if type(version) is not int or version != 1:
        raise FilterPolicyError("only filter policy version 1 is supported")

    title = _text_rules(document["title"], "title")
    location = _text_rules(document["location"], "location")
    company = _company_rules(document["company"])
    description = _text_rules(document["description"], "description")
    return FilterPolicy(
        version=version,
        title=title,
        location=location,
        company=company,
        description=description,
        fingerprint=hashlib.sha256(policy_bytes).hexdigest(),
    )


def _text_rules(value: object, section: str) -> TextFilterRules:
    table = _require_table(value, section)
    _require_exact_keys(table, {"include_any", "exclude_any"}, section)
    return TextFilterRules(
        include_any=_require_terms(table["include_any"], f"{section}.include_any"),
        exclude_any=_require_terms(table["exclude_any"], f"{section}.exclude_any"),
    )


def _company_rules(value: object) -> CompanyFilterRules:
    table = _require_table(value, "company")
    _require_exact_keys(table, {"exclude"}, "company")
    return CompanyFilterRules(
        exclude=_require_terms(table["exclude"], "company.exclude")
    )


def _require_table(value: object, section: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise FilterPolicyError(f"{section} must be a TOML table")
    return value


def _require_exact_keys(
    value: dict[str, object], expected: set[str], context: str
) -> None:
    actual = set(value)
    if actual == expected:
        return
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    details: list[str] = []
    if unknown:
        details.append(f"unknown keys: {', '.join(unknown)}")
    if missing:
        details.append(f"missing keys: {', '.join(missing)}")
    raise FilterPolicyError(f"invalid {context} ({'; '.join(details)})")
