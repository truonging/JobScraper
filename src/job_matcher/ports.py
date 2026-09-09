"""Boundary interfaces for Phase 1 acquisition and persistence."""

from collections.abc import Sequence
from typing import Protocol

from job_matcher.models import AcquiredJob, SourceJobKey


class SourceAcquisitionError(RuntimeError):
    """Raised when a source cannot produce a valid acquisition result."""


class JobSource(Protocol):
    """Fetch and normalize jobs from one configured external source."""

    def fetch_jobs(self) -> Sequence[AcquiredJob]:
        """Return the jobs available from the configured source."""
        ...


class JobRepository(Protocol):
    """Store and retrieve acquired jobs without exposing storage details."""

    def upsert_batch(self, jobs: Sequence[AcquiredJob]) -> None:
        """Atomically insert or update a batch by source-local key."""
        ...

    def get(self, key: SourceJobKey) -> AcquiredJob | None:
        """Return the current record for a source-local key, if present."""
        ...
