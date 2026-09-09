"""Application orchestration for acquiring and persisting jobs."""

from job_matcher.ports import JobRepository, JobSource


def acquire_and_persist(source: JobSource, repository: JobRepository) -> int:
    """Fetch one source and atomically persist its complete acquired batch."""
    jobs = source.fetch_jobs()
    repository.upsert_batch(jobs)
    return len(jobs)
