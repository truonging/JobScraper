"""Boundary interfaces for acquisition, persistence, and logical identity."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from job_matcher.catalog import (
    LinkedSourceJob,
    LogicalJobId,
    SourceJobLifecycle,
    SourceJobLink,
)
from job_matcher.models import AcquiredJob, NormalizedJob, SourceJobKey, SourceSnapshot


class SourceAcquisitionError(RuntimeError):
    """Raised when a source cannot produce a valid acquisition result."""


class PersistenceError(RuntimeError):
    """Raised when persistence cannot store or reconstruct job data."""


class JobSource(Protocol):
    """Fetch one complete snapshot from a configured external source."""

    def fetch_snapshot(self) -> SourceSnapshot:
        """Return a successful full snapshot, including when it is empty."""
        ...


class JobRepository(Protocol):
    """Store and retrieve acquired jobs without exposing storage details."""

    def reconcile_snapshot(self, snapshot: SourceSnapshot) -> None:
        """Atomically store a complete snapshot and reconcile lifecycle state."""
        ...

    def get(self, key: SourceJobKey) -> AcquiredJob | None:
        """Return the current record for a source-local key, if present."""
        ...

    def get_lifecycle(self, key: SourceJobKey) -> SourceJobLifecycle | None:
        """Return current source-posting lifecycle state, if present."""
        ...


class IdentityRepository(Protocol):
    """Store logical assignments without owning identity policy."""

    def list_unlinked_jobs(self) -> Sequence[NormalizedJob]:
        """Return persisted normalized jobs without logical assignments."""
        ...

    def list_linked_jobs(self) -> Sequence[LinkedSourceJob]:
        """Return persisted normalized jobs with their logical assignments."""
        ...

    def assign_logical_job(
        self,
        source_key: SourceJobKey,
        proposed_logical_job_id: LogicalJobId,
        linked_at: datetime,
    ) -> SourceJobLink:
        """Create an assignment or return the source posting's existing link."""
        ...

    def get_link(self, source_key: SourceJobKey) -> SourceJobLink | None:
        """Return a source posting's durable logical assignment, if present."""
        ...

    def get_logical_job_activity(self, logical_job_id: LogicalJobId) -> bool | None:
        """Return derived activity, or None when the logical job does not exist."""
        ...
