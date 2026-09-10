"""Source-posting lifecycle and logical-job identity contracts."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from job_matcher.models import ContractValidationError, SourceJobKey


def _require_utc(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ContractValidationError(f"{field_name} must be timezone-aware")
    if value.tzinfo is not UTC:
        raise ContractValidationError(f"{field_name} must use datetime.UTC")


class SourceJobStatus(StrEnum):
    """Current availability of one source posting."""

    ACTIVE = "active"
    INACTIVE = "inactive"


@dataclass(frozen=True, slots=True)
class SourceJobLifecycle:
    """Current lifecycle state observed through successful source snapshots."""

    key: SourceJobKey
    status: SourceJobStatus
    first_seen_at: datetime
    last_seen_at: datetime
    inactive_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, SourceJobKey):
            raise ContractValidationError("key must be a SourceJobKey")
        if not isinstance(self.status, SourceJobStatus):
            raise ContractValidationError("status must be a SourceJobStatus")
        _require_utc(self.first_seen_at, "first_seen_at")
        _require_utc(self.last_seen_at, "last_seen_at")
        if self.first_seen_at > self.last_seen_at:
            raise ContractValidationError("first_seen_at must not follow last_seen_at")

        if self.status is SourceJobStatus.ACTIVE:
            if self.inactive_at is not None:
                raise ContractValidationError(
                    "an active source posting must not have inactive_at"
                )
            return

        if self.inactive_at is None:
            raise ContractValidationError(
                "an inactive source posting must have inactive_at"
            )
        _require_utc(self.inactive_at, "inactive_at")
        if self.inactive_at < self.last_seen_at:
            raise ContractValidationError("inactive_at must not precede last_seen_at")


@dataclass(frozen=True, slots=True)
class LogicalJobId:
    """Opaque stable identity shared by equivalent source postings."""

    value: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.value, UUID):
            raise ContractValidationError("value must be a UUID")


@dataclass(frozen=True, slots=True)
class SourceJobLink:
    """Durable assignment of a source posting to one logical job."""

    source_key: SourceJobKey
    logical_job_id: LogicalJobId
    linked_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.source_key, SourceJobKey):
            raise ContractValidationError("source_key must be a SourceJobKey")
        if not isinstance(self.logical_job_id, LogicalJobId):
            raise ContractValidationError("logical_job_id must be a LogicalJobId")
        _require_utc(self.linked_at, "linked_at")
