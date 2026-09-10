"""Application orchestration for acquiring and persisting jobs."""

from job_matcher.ports import JobRepository, JobSource


def acquire_and_persist(source: JobSource, repository: JobRepository) -> int:
    """Fetch one source and atomically persist its complete acquired batch."""
    snapshot = source.fetch_snapshot()
    repository.upsert_batch(snapshot.jobs)
    return len(snapshot.jobs)
