"""Source-posting lifecycle and logical-job identity contracts."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from job_matcher.models import ContractValidationError, NormalizedJob, SourceJobKey


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


class IdentityEvidenceKind(StrEnum):
    """Deterministic comparison that produced identity evidence."""

    EXACT_CANONICAL_JOB_URL = "exact_canonical_job_url"
    EXACT_CANONICAL_APPLY_URL = "exact_canonical_apply_url"
    EXACT_CONTENT_FINGERPRINT = "exact_content_fingerprint"


class IdentityDisposition(StrEnum):
    """Permitted use of deterministic identity evidence."""

    AUTOMATIC_LINK = "automatic_link"
    DUPLICATE_CANDIDATE = "duplicate_candidate"


@dataclass(frozen=True, slots=True)
class IdentityEvidence:
    """Source-independent evidence relating two source postings."""

    source_key: SourceJobKey
    matched_source_key: SourceJobKey
    matched_logical_job_id: LogicalJobId
    kind: IdentityEvidenceKind
    disposition: IdentityDisposition
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_key, SourceJobKey):
            raise ContractValidationError("source_key must be a SourceJobKey")
        if not isinstance(self.matched_source_key, SourceJobKey):
            raise ContractValidationError("matched_source_key must be a SourceJobKey")
        if self.source_key == self.matched_source_key:
            raise ContractValidationError(
                "identity evidence cannot match a job to itself"
            )
        if not isinstance(self.matched_logical_job_id, LogicalJobId):
            raise ContractValidationError(
                "matched_logical_job_id must be a LogicalJobId"
            )
        if not isinstance(self.kind, IdentityEvidenceKind):
            raise ContractValidationError("kind must be an IdentityEvidenceKind")
        if not isinstance(self.disposition, IdentityDisposition):
            raise ContractValidationError("disposition must be an IdentityDisposition")
        if not isinstance(self.value, str) or not self.value.strip():
            raise ContractValidationError("value must be a non-blank string")
        if (
            self.kind is IdentityEvidenceKind.EXACT_CONTENT_FINGERPRINT
            and self.disposition is not IdentityDisposition.DUPLICATE_CANDIDATE
        ):
            raise ContractValidationError(
                "content fingerprints cannot authorize an automatic link"
            )


@dataclass(frozen=True, slots=True)
class LinkedSourceJob:
    """Current normalized source job paired with its durable logical link."""

    normalized: NormalizedJob
    link: SourceJobLink

    def __post_init__(self) -> None:
        if not isinstance(self.normalized, NormalizedJob):
            raise ContractValidationError("normalized must be a NormalizedJob")
        if not isinstance(self.link, SourceJobLink):
            raise ContractValidationError("link must be a SourceJobLink")
        if self.normalized.key != self.link.source_key:
            raise ContractValidationError(
                "normalized job and link must use the same source job key"
            )


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    """Logical assignment and transient evidence for one source posting."""

    link: SourceJobLink
    created_logical_job: bool
    evidence: tuple[IdentityEvidence, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.link, SourceJobLink):
            raise ContractValidationError("link must be a SourceJobLink")
        if not isinstance(self.created_logical_job, bool):
            raise ContractValidationError("created_logical_job must be a boolean")
        if not isinstance(self.evidence, tuple):
            raise ContractValidationError("evidence must be an immutable tuple")
        if not all(isinstance(item, IdentityEvidence) for item in self.evidence):
            raise ContractValidationError(
                "evidence must contain IdentityEvidence records"
            )
