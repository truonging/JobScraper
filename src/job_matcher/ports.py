"""Boundary interfaces for acquisition and persistence."""

from typing import Protocol

from job_matcher.catalog import SourceJobLifecycle
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

    def reconcile_snapshot(self, snapshot: SourceSnapshot) -> None:
        """Atomically store a complete snapshot and reconcile lifecycle state."""
        ...

    def get(self, key: SourceJobKey) -> AcquiredJob | None:
        """Return the current record for a source-local key, if present."""
        ...

    def get_lifecycle(self, key: SourceJobKey) -> SourceJobLifecycle | None:
        """Return current source-posting lifecycle state, if present."""
        ...
