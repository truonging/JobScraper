"""Integration tests for SQLite snapshot and lifecycle persistence."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from job_matcher import sqlite_repository
from job_matcher.catalog import SourceJobStatus
from job_matcher.models import (
    AcquiredJob,
    NormalizedJob,
    RawSourceRecord,
    SourceJobKey,
    SourceSnapshot,
)
from job_matcher.ports import PersistenceError
from job_matcher.sqlite_repository import SQLiteJobRepository

T1 = datetime(2026, 9, 8, 12, 30, tzinfo=UTC)
T2 = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
T3 = datetime(2026, 9, 8, 14, 30, tzinfo=UTC)


def make_job(
    *,
    source: str = "lever",
    source_scope: str = "example",
    source_job_id: str = "job-123",
    title: str = "Software Engineer",
    retrieved_at: datetime = T1,
    payload_json: str | None = None,
    include_optional: bool = True,
) -> AcquiredJob:
    key = SourceJobKey(source, source_scope, source_job_id)
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
        payload_json=payload_json or json.dumps({"id": source_job_id, "text": title}),
    )
    return AcquiredJob(normalized=normalized, raw=raw)


def make_snapshot(
    *jobs: AcquiredJob,
    source: str = "lever",
    source_scope: str = "example",
    retrieved_at: datetime = T1,
) -> SourceSnapshot:
    return SourceSnapshot(source, source_scope, retrieved_at, jobs)


def initialized_repository(database_path: Path) -> SQLiteJobRepository:
    repository = SQLiteJobRepository(database_path)
    repository.initialize_schema()
    return repository


def row_count(database_path: Path, table_name: str) -> int:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return int(row[0])


def checkpoint(database_path: Path, source: str, source_scope: str) -> str | None:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT last_retrieved_at
            FROM source_scope_state
            WHERE source = ? AND source_scope = ?
            """,
            (source, source_scope),
        ).fetchone()
    return None if row is None else str(row[0])


def database_state(database_path: Path) -> dict[str, list[tuple[Any, ...]]]:
    tables = (
        "raw_source_jobs",
        "normalized_jobs",
        "source_job_lifecycle",
        "source_scope_state",
    )
    with sqlite3.connect(database_path) as connection:
        return {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY 1, 2, 3"
            ).fetchall()
            for table in tables
        }


def create_v1_database(database_path: Path, jobs: tuple[AcquiredJob, ...]) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(sqlite_repository._CREATE_RAW_TABLE)
        connection.execute(sqlite_repository._CREATE_NORMALIZED_TABLE)
        for job in jobs:
            connection.execute(
                sqlite_repository._UPSERT_RAW,
                SQLiteJobRepository._raw_values(job.raw),
            )
            connection.execute(
                sqlite_repository._UPSERT_NORMALIZED,
                SQLiteJobRepository._normalized_values(job.normalized),
            )
        connection.execute("PRAGMA user_version = 1")


def test_initialize_schema_creates_version_two_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"

    initialized_repository(database_path)

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert version == 2
    assert {
        "raw_source_jobs",
        "normalized_jobs",
        "source_scope_state",
        "source_job_lifecycle",
        "logical_jobs",
        "source_job_links",
    } <= tables
    assert row_count(database_path, "logical_jobs") == 0
    assert row_count(database_path, "source_job_links") == 0


def test_initialize_schema_is_idempotent(tmp_path: Path) -> None:
    repository = initialized_repository(tmp_path / "jobs.sqlite3")

    repository.initialize_schema()


@pytest.mark.parametrize("operation", ["get", "get_lifecycle", "reconcile"])
def test_operations_require_explicit_initialization(
    tmp_path: Path, operation: str
) -> None:
    database_path = tmp_path / f"{operation}.sqlite3"
    repository = SQLiteJobRepository(database_path)

    with pytest.raises(PersistenceError, match="missing or cannot be opened"):
        if operation == "get":
            repository.get(make_job().normalized.key)
        elif operation == "get_lifecycle":
            repository.get_lifecycle(make_job().normalized.key)
        else:
            repository.reconcile_snapshot(make_snapshot())

    assert not database_path.exists()


def test_operations_reject_an_uninitialized_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    sqlite3.connect(database_path).close()
    repository = SQLiteJobRepository(database_path)

    with pytest.raises(PersistenceError, match="has not been initialized"):
        repository.get(make_job().normalized.key)


def test_initialization_rejects_an_unversioned_partial_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE raw_source_jobs (source TEXT NOT NULL)")

    with pytest.raises(PersistenceError, match="unversioned or partial"):
        SQLiteJobRepository(database_path).initialize_schema()


def test_schema_initialization_rejects_an_unsupported_version(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 3")

    with pytest.raises(PersistenceError, match="unsupported.*3"):
        SQLiteJobRepository(database_path).initialize_schema()


def test_operations_reject_a_versioned_partial_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE raw_source_jobs (source TEXT NOT NULL)")
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(PersistenceError, match="invalid raw_source_jobs table"):
        SQLiteJobRepository(database_path).get(make_job().normalized.key)


def test_migration_rejects_a_partial_version_one_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(sqlite_repository._CREATE_RAW_TABLE)
        connection.execute("PRAGMA user_version = 1")

    with pytest.raises(PersistenceError, match="invalid normalized_jobs table"):
        SQLiteJobRepository(database_path).initialize_schema()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_migration_preserves_jobs_and_backfills_lifecycle(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    original = make_job(payload_json='{ "tags": ["python"], "id": 123 }')
    create_v1_database(database_path, (original,))

    repository = SQLiteJobRepository(database_path)
    repository.initialize_schema()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    restored = repository.get(original.normalized.key)
    assert restored is not None
    assert restored.normalized == original.normalized
    assert json.loads(restored.raw.payload_json) == json.loads(
        original.raw.payload_json
    )
    lifecycle = repository.get_lifecycle(original.normalized.key)
    assert lifecycle is not None
    assert lifecycle.status is SourceJobStatus.ACTIVE
    assert lifecycle.first_seen_at == T1
    assert lifecycle.last_seen_at == T1
    assert lifecycle.inactive_at is None
    assert checkpoint(database_path, "lever", "example") == T1.isoformat()
    assert row_count(database_path, "logical_jobs") == 0
    assert row_count(database_path, "source_job_links") == 0


def test_source_link_schema_allows_at_most_one_logical_job_per_source_posting(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    repository = initialized_repository(database_path)
    job = make_job()
    repository.reconcile_snapshot(make_snapshot(job))
    key = job.normalized.key
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("INSERT INTO logical_jobs VALUES ('logical-1')")
        connection.execute("INSERT INTO logical_jobs VALUES ('logical-2')")
        connection.execute(
            "INSERT INTO source_job_links VALUES (?, ?, ?, ?, ?)",
            (*SQLiteJobRepository._key_values(key), "logical-1", T1.isoformat()),
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO source_job_links VALUES (?, ?, ?, ?, ?)",
                (*SQLiteJobRepository._key_values(key), "logical-2", T2.isoformat()),
            )


def test_migration_uses_latest_retained_time_for_scope_checkpoint(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    create_v1_database(
        database_path,
        (
            make_job(source_job_id="job-1", retrieved_at=T1),
            make_job(source_job_id="job-2", retrieved_at=T2),
        ),
    )

    SQLiteJobRepository(database_path).initialize_schema()

    assert checkpoint(database_path, "lever", "example") == T2.isoformat()


def test_failed_migration_rolls_back_schema_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    create_v1_database(database_path, (make_job(),))
    monkeypatch.setattr(sqlite_repository, "_CREATE_LOGICAL_JOB_TABLE", "invalid SQL")

    with pytest.raises(PersistenceError, match="migrate SQLite schema"):
        SQLiteJobRepository(database_path).initialize_schema()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "source_scope_state" not in tables
    assert "source_job_lifecycle" not in tables


def test_complete_job_round_trips_with_equivalent_json(tmp_path: Path) -> None:
    repository = initialized_repository(tmp_path / "jobs.sqlite3")
    original = make_job(payload_json='{ "tags": ["python", "api"], "id": 123 }')

    repository.reconcile_snapshot(make_snapshot(original))
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

    repository.reconcile_snapshot(make_snapshot(original))

    assert repository.get(original.normalized.key) == original


def test_missing_job_and_lifecycle_return_none(tmp_path: Path) -> None:
    repository = initialized_repository(tmp_path / "jobs.sqlite3")
    key = make_job().normalized.key

    assert repository.get(key) is None
    assert repository.get_lifecycle(key) is None


def test_first_snapshot_creates_active_lifecycle(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    repository = initialized_repository(database_path)
    job = make_job()

    repository.reconcile_snapshot(make_snapshot(job))

    lifecycle = repository.get_lifecycle(job.normalized.key)
    assert lifecycle is not None
    assert lifecycle.status is SourceJobStatus.ACTIVE
    assert lifecycle.first_seen_at == T1
    assert lifecycle.last_seen_at == T1
    assert lifecycle.inactive_at is None
    assert checkpoint(database_path, "lever", "example") == T1.isoformat()


def test_newer_snapshot_updates_current_job_and_preserves_first_seen(
    tmp_path: Path,
) -> None:
    repository = initialized_repository(tmp_path / "jobs.sqlite3")
    original = make_job()
    updated = make_job(
        title="Senior Software Engineer",
        retrieved_at=T2,
    )

    repository.reconcile_snapshot(make_snapshot(original))
    repository.reconcile_snapshot(make_snapshot(updated, retrieved_at=T2))

    assert repository.get(updated.normalized.key) == updated
    lifecycle = repository.get_lifecycle(updated.normalized.key)
    assert lifecycle is not None
    assert lifecycle.first_seen_at == T1
    assert lifecycle.last_seen_at == T2
    assert lifecycle.status is SourceJobStatus.ACTIVE
    assert row_count(tmp_path / "jobs.sqlite3", "raw_source_jobs") == 1
    assert row_count(tmp_path / "jobs.sqlite3", "normalized_jobs") == 1
    assert row_count(tmp_path / "jobs.sqlite3", "source_job_lifecycle") == 1


def test_absence_inactivates_and_later_observation_reactivates(
    tmp_path: Path,
) -> None:
    repository = initialized_repository(tmp_path / "jobs.sqlite3")
    first = make_job(retrieved_at=T1)

    repository.reconcile_snapshot(make_snapshot(first, retrieved_at=T1))
    repository.reconcile_snapshot(make_snapshot(retrieved_at=T2))

    inactive = repository.get_lifecycle(first.normalized.key)
    assert inactive is not None
    assert inactive.status is SourceJobStatus.INACTIVE
    assert inactive.first_seen_at == T1
    assert inactive.last_seen_at == T1
    assert inactive.inactive_at == T2

    returned = make_job(retrieved_at=T3)
    repository.reconcile_snapshot(make_snapshot(returned, retrieved_at=T3))

    active = repository.get_lifecycle(first.normalized.key)
    assert active is not None
    assert active.status is SourceJobStatus.ACTIVE
    assert active.first_seen_at == T1
    assert active.last_seen_at == T3
    assert active.inactive_at is None


def test_repeated_absence_preserves_original_inactive_time(tmp_path: Path) -> None:
    repository = initialized_repository(tmp_path / "jobs.sqlite3")
    job = make_job()
    repository.reconcile_snapshot(make_snapshot(job))
    repository.reconcile_snapshot(make_snapshot(retrieved_at=T2))

    repository.reconcile_snapshot(make_snapshot(retrieved_at=T3))

    lifecycle = repository.get_lifecycle(job.normalized.key)
    assert lifecycle is not None
    assert lifecycle.inactive_at == T2


def test_reconciliation_isolated_by_source_and_scope(tmp_path: Path) -> None:
    repository = initialized_repository(tmp_path / "jobs.sqlite3")
    lever = make_job(source="lever", source_scope="one")
    ashby = make_job(source="ashby", source_scope="one")
    other_scope = make_job(source="lever", source_scope="two")
    repository.reconcile_snapshot(
        make_snapshot(lever, source="lever", source_scope="one")
    )
    repository.reconcile_snapshot(
        make_snapshot(ashby, source="ashby", source_scope="one")
    )
    repository.reconcile_snapshot(
        make_snapshot(other_scope, source="lever", source_scope="two")
    )

    repository.reconcile_snapshot(
        make_snapshot(source="lever", source_scope="one", retrieved_at=T2)
    )

    assert (
        repository.get_lifecycle(lever.normalized.key).status
        is SourceJobStatus.INACTIVE
    )  # type: ignore[union-attr]
    assert (
        repository.get_lifecycle(ashby.normalized.key).status is SourceJobStatus.ACTIVE
    )  # type: ignore[union-attr]
    assert (
        repository.get_lifecycle(other_scope.normalized.key).status
        is SourceJobStatus.ACTIVE
    )  # type: ignore[union-attr]


@pytest.mark.parametrize("source", ["lever", "ashby", "greenhouse"])
def test_reconciliation_is_source_independent(tmp_path: Path, source: str) -> None:
    repository = initialized_repository(tmp_path / f"{source}.sqlite3")
    job = make_job(source=source)

    repository.reconcile_snapshot(make_snapshot(job, source=source))

    assert repository.get(job.normalized.key) == job
    assert repository.get_lifecycle(job.normalized.key).status is SourceJobStatus.ACTIVE  # type: ignore[union-attr]


@pytest.mark.parametrize("rejected_at", [T1, T1 - timedelta(minutes=1)])
def test_equal_or_older_snapshot_is_rejected_without_database_changes(
    tmp_path: Path, rejected_at: datetime
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    repository = initialized_repository(database_path)
    original = make_job()
    repository.reconcile_snapshot(make_snapshot(original))
    state_before = database_state(database_path)
    rejected = make_job(title="Changed", retrieved_at=rejected_at)

    with pytest.raises(PersistenceError, match="must be later"):
        repository.reconcile_snapshot(make_snapshot(rejected, retrieved_at=rejected_at))

    assert database_state(database_path) == state_before
    assert repository.get(original.normalized.key) == original
    assert checkpoint(database_path, "lever", "example") == T1.isoformat()


def test_reconciliation_failure_rolls_back_every_change(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    repository = initialized_repository(database_path)
    original = make_job(source_job_id="job-1")
    repository.reconcile_snapshot(make_snapshot(original))
    state_before = database_state(database_path)
    updated = make_job(source_job_id="job-1", title="Updated", retrieved_at=T2)
    rejected = make_job(source_job_id="job-2", title="Reject me", retrieved_at=T2)
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

    with pytest.raises(PersistenceError, match="reconcile source snapshot"):
        repository.reconcile_snapshot(make_snapshot(updated, rejected, retrieved_at=T2))

    assert database_state(database_path) == state_before


def test_invalid_stored_data_is_reported_as_persistence_error(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    repository = initialized_repository(database_path)
    job = make_job()
    repository.reconcile_snapshot(make_snapshot(job))
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE raw_source_jobs SET payload_json = '{}' ")

    with pytest.raises(PersistenceError, match="stored acquired job is invalid"):
        repository.get(job.normalized.key)


def test_invalid_stored_lifecycle_is_reported_as_persistence_error(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    repository = initialized_repository(database_path)
    job = make_job()
    repository.reconcile_snapshot(make_snapshot(job))
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE source_job_lifecycle SET status = 'unknown'")

    with pytest.raises(
        PersistenceError, match="stored source job lifecycle is invalid"
    ):
        repository.get_lifecycle(job.normalized.key)
