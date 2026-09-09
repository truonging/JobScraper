"""Integration tests for SQLite acquired-job persistence."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from job_matcher.models import AcquiredJob, NormalizedJob, RawSourceRecord, SourceJobKey
from job_matcher.ports import PersistenceError
from job_matcher.sqlite_repository import SQLiteJobRepository


def make_job(
    *,
    source_scope: str = "example",
    source_job_id: str = "job-123",
    title: str = "Software Engineer",
    retrieved_at: datetime = datetime(2026, 9, 8, 12, 30, tzinfo=UTC),
    payload_json: str = '{"id":"job-123","text":"Software Engineer"}',
    include_optional: bool = True,
) -> AcquiredJob:
    key = SourceJobKey(
        source="lever",
        source_scope=source_scope,
        source_job_id=source_job_id,
    )
    normalized = NormalizedJob(
        key=key,
        company="Example Company",
        title=title,
        description="Build reliable software.",
        location="Remote" if include_optional else None,
        job_url=f"https://jobs.example.com/{source_job_id}",
        apply_url=(
            f"https://jobs.example.com/{source_job_id}/apply"
            if include_optional
            else None
        ),
        posted_at=(
            datetime(2026, 9, 1, 8, 0, tzinfo=timezone(timedelta(hours=-7)))
            if include_optional
            else None
        ),
        retrieved_at=retrieved_at,
    )
    raw = RawSourceRecord(
        key=key,
        retrieved_at=retrieved_at,
        payload_json=payload_json,
    )
    return AcquiredJob(normalized=normalized, raw=raw)


def initialized_repository(database_path: Path) -> SQLiteJobRepository:
    repository = SQLiteJobRepository(database_path)
    repository.initialize_schema()
    return repository


def row_count(database_path: Path, table_name: str) -> int:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return int(row[0])


def test_initialize_schema_creates_separate_versioned_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    repository = SQLiteJobRepository(database_path)

    repository.initialize_schema()

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert version == 1
    assert {"raw_source_jobs", "normalized_jobs"} <= tables


def test_initialize_schema_is_idempotent(tmp_path: Path) -> None:
    repository = SQLiteJobRepository(tmp_path / "jobs.sqlite3")

    repository.initialize_schema()
    repository.initialize_schema()


@pytest.mark.parametrize("operation", ["get", "empty_upsert"])
def test_operations_require_explicit_initialization(
    tmp_path: Path, operation: str
) -> None:
    database_path = tmp_path / f"{operation}.sqlite3"
    repository = SQLiteJobRepository(database_path)

    with pytest.raises(PersistenceError, match="missing or cannot be opened"):
        if operation == "get":
            repository.get(make_job().normalized.key)
        else:
            repository.upsert_batch([])

    assert not database_path.exists()


def test_operations_reject_an_uninitialized_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    sqlite3.connect(database_path).close()
    repository = SQLiteJobRepository(database_path)

    with pytest.raises(PersistenceError, match="has not been initialized"):
        repository.get(make_job().normalized.key)


def test_schema_initialization_rejects_an_unsupported_version(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 2")
    repository = SQLiteJobRepository(database_path)

    with pytest.raises(PersistenceError, match="unsupported.*2"):
        repository.initialize_schema()


def test_operations_reject_a_versioned_partial_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE raw_source_jobs (source TEXT NOT NULL)")
        connection.execute("PRAGMA user_version = 1")
    repository = SQLiteJobRepository(database_path)

    with pytest.raises(PersistenceError, match="invalid raw_source_jobs table"):
        repository.get(make_job().normalized.key)


def test_complete_job_round_trips_with_equivalent_json(tmp_path: Path) -> None:
    repository = initialized_repository(tmp_path / "jobs.sqlite3")
    original = make_job(payload_json='{ "tags": ["python", "api"], "id": 123 }')

    repository.upsert_batch([original])
    restored = repository.get(original.normalized.key)

    assert restored is not None
    assert restored.normalized == original.normalized
    assert restored.raw.key == original.raw.key
    assert restored.raw.retrieved_at == original.raw.retrieved_at
    assert json.loads(restored.raw.payload_json) == json.loads(
        original.raw.payload_json
    )


def test_optional_fields_round_trip_as_none(tmp_path: Path) -> None:
    repository = initialized_repository(tmp_path / "jobs.sqlite3")
    original = make_job(include_optional=False)

    repository.upsert_batch([original])

    assert repository.get(original.normalized.key) == original


def test_missing_job_returns_none(tmp_path: Path) -> None:
    repository = initialized_repository(tmp_path / "jobs.sqlite3")

    assert repository.get(make_job().normalized.key) is None


def test_batch_stores_jobs_with_distinct_source_keys(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    repository = initialized_repository(database_path)
    first = make_job()
    second = make_job(source_scope="another", source_job_id="job-456")

    repository.upsert_batch([first, second])

    assert repository.get(first.normalized.key) == first
    assert repository.get(second.normalized.key) == second
    assert row_count(database_path, "raw_source_jobs") == 2
    assert row_count(database_path, "normalized_jobs") == 2


def test_repeated_job_updates_current_raw_and_normalized_records(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    repository = initialized_repository(database_path)
    original = make_job()
    updated = make_job(
        title="Senior Software Engineer",
        retrieved_at=datetime(2026, 9, 8, 13, 30, tzinfo=UTC),
        payload_json='{"id":"job-123","text":"Senior Software Engineer"}',
    )

    repository.upsert_batch([original])
    repository.upsert_batch([updated])

    assert repository.get(updated.normalized.key) == updated
    assert row_count(database_path, "raw_source_jobs") == 1
    assert row_count(database_path, "normalized_jobs") == 1


def test_batch_failure_rolls_back_every_job(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    repository = initialized_repository(database_path)
    first = make_job(source_job_id="job-1")
    rejected = make_job(source_job_id="job-2", title="Reject me")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_test_title
            BEFORE INSERT ON normalized_jobs
            WHEN NEW.title = 'Reject me'
            BEGIN
                SELECT RAISE(ABORT, 'rejected by test');
            END
            """
        )

    with pytest.raises(PersistenceError, match="persist acquired job batch"):
        repository.upsert_batch([first, rejected])

    assert repository.get(first.normalized.key) is None
    assert repository.get(rejected.normalized.key) is None


def test_invalid_stored_data_is_reported_as_persistence_error(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    repository = initialized_repository(database_path)
    job = make_job()
    repository.upsert_batch([job])
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE raw_source_jobs SET payload_json = '{}' ")

    with pytest.raises(PersistenceError, match="stored acquired job is invalid"):
        repository.get(job.normalized.key)
