"""Source-independent data contracts for acquired jobs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit


class ContractValidationError(ValueError):
    """Raised when data does not satisfy an acquisition contract."""


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be a non-blank string")


def _require_web_url(value: str, field_name: str) -> None:
    _require_non_blank(value, field_name)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ContractValidationError(f"{field_name} must be an HTTP or HTTPS URL")
    if any(character.isspace() for character in value):
        raise ContractValidationError(f"{field_name} must not contain whitespace")


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ContractValidationError(f"{field_name} must be timezone-aware")


def _require_canonical_utc(value: datetime, field_name: str) -> None:
    _require_aware(value, field_name)
    if value.tzinfo is not UTC:
        raise ContractValidationError(f"{field_name} must use datetime.UTC")


@dataclass(frozen=True, slots=True)
class SourceJobKey:
    """Identity of a posting within one configured external source."""

    source: str
    source_scope: str
    source_job_id: str

    def __post_init__(self) -> None:
        _require_non_blank(self.source, "source")
        _require_non_blank(self.source_scope, "source_scope")
        _require_non_blank(self.source_job_id, "source_job_id")


@dataclass(frozen=True, slots=True)
class NormalizedJob:
    """Source-independent facts normalized from one external posting."""

    key: SourceJobKey
    company: str
    title: str
    description: str
    job_url: str
    retrieved_at: datetime
    location: str | None = None
    apply_url: str | None = None
    posted_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_non_blank(self.company, "company")
        _require_non_blank(self.title, "title")
        _require_non_blank(self.description, "description")
        _require_web_url(self.job_url, "job_url")
        _require_canonical_utc(self.retrieved_at, "retrieved_at")

        if self.location is not None:
            _require_non_blank(self.location, "location")
        if self.apply_url is not None:
            _require_web_url(self.apply_url, "apply_url")
        if self.posted_at is not None:
            _require_aware(self.posted_at, "posted_at")


@dataclass(frozen=True, slots=True)
class RawSourceRecord:
    """Immutable JSON snapshot associated with one source posting."""

    key: SourceJobKey
    retrieved_at: datetime
    payload_json: str

    def __post_init__(self) -> None:
        _require_canonical_utc(self.retrieved_at, "retrieved_at")
        _require_non_blank(self.payload_json, "payload_json")

        try:
            payload: Any = json.loads(self.payload_json)
        except json.JSONDecodeError as error:
            raise ContractValidationError(
                "payload_json must contain valid JSON"
            ) from error

        if not isinstance(payload, dict) or not payload:
            raise ContractValidationError(
                "payload_json must contain a non-empty JSON object"
            )


@dataclass(frozen=True, slots=True)
class AcquiredJob:
    """A normalized job paired with the source snapshot it came from."""

    normalized: NormalizedJob
    raw: RawSourceRecord

    def __post_init__(self) -> None:
        if self.normalized.key != self.raw.key:
            raise ContractValidationError(
                "normalized and raw records must use the same source job key"
            )
        if self.normalized.retrieved_at != self.raw.retrieved_at:
            raise ContractValidationError(
                "normalized and raw records must use the same retrieved_at"
            )
