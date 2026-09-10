"""Boundary interfaces for Phase 1 acquisition and persistence."""

from collections.abc import Sequence
from typing import Protocol

from job_matcher.models import AcquiredJob, SourceJobKey, SourceSnapshot


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

    def upsert_batch(self, jobs: Sequence[AcquiredJob]) -> None:
        """Atomically insert or update a batch by source-local key."""
        ...

    def get(self, key: SourceJobKey) -> AcquiredJob | None:
        """Return the current record for a source-local key, if present."""
        ...
