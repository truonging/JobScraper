"""SQLite implementation of the acquired-job persistence boundary."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from job_matcher.models import AcquiredJob, NormalizedJob, RawSourceRecord, SourceJobKey
from job_matcher.ports import PersistenceError

_SCHEMA_VERSION = 1

_RAW_COLUMNS = {
    "source",
    "source_scope",
    "source_job_id",
    "retrieved_at",
    "payload_json",
}
_NORMALIZED_COLUMNS = {
    "source",
    "source_scope",
    "source_job_id",
    "company",
    "title",
    "description",
    "location",
    "job_url",
    "apply_url",
    "posted_at",
    "retrieved_at",
}

_CREATE_RAW_TABLE = """
CREATE TABLE raw_source_jobs (
    source TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (source, source_scope, source_job_id)
)
"""

_CREATE_NORMALIZED_TABLE = """
CREATE TABLE normalized_jobs (
    source TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    location TEXT,
    job_url TEXT NOT NULL,
    apply_url TEXT,
    posted_at TEXT,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (source, source_scope, source_job_id),
    FOREIGN KEY (source, source_scope, source_job_id)
        REFERENCES raw_source_jobs (source, source_scope, source_job_id)
        ON DELETE CASCADE
)
"""

_UPSERT_RAW = """
INSERT INTO raw_source_jobs (
    source, source_scope, source_job_id, retrieved_at, payload_json
) VALUES (?, ?, ?, ?, ?)
ON CONFLICT (source, source_scope, source_job_id) DO UPDATE SET
    retrieved_at = excluded.retrieved_at,
    payload_json = excluded.payload_json
"""

_UPSERT_NORMALIZED = """
INSERT INTO normalized_jobs (
    source,
    source_scope,
    source_job_id,
    company,
    title,
    description,
    location,
    job_url,
    apply_url,
    posted_at,
    retrieved_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (source, source_scope, source_job_id) DO UPDATE SET
    company = excluded.company,
    title = excluded.title,
    description = excluded.description,
    location = excluded.location,
    job_url = excluded.job_url,
    apply_url = excluded.apply_url,
    posted_at = excluded.posted_at,
    retrieved_at = excluded.retrieved_at
"""

_SELECT_JOB = """
SELECT
    normalized.source,
    normalized.source_scope,
    normalized.source_job_id,
    normalized.company,
    normalized.title,
    normalized.description,
    normalized.location,
    normalized.job_url,
    normalized.apply_url,
    normalized.posted_at,
    normalized.retrieved_at AS normalized_retrieved_at,
    raw.retrieved_at AS raw_retrieved_at,
    raw.payload_json
FROM normalized_jobs AS normalized
JOIN raw_source_jobs AS raw USING (source, source_scope, source_job_id)
WHERE
    normalized.source = ?
    AND normalized.source_scope = ?
    AND normalized.source_job_id = ?
"""


class SQLiteJobRepository:
    """Persist the current representation of each source-local job in SQLite."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    def initialize_schema(self) -> None:
        """Create schema version 1, or validate an existing version 1 schema."""
        if not self._database_path.parent.is_dir():
            raise PersistenceError(
                f"database directory does not exist: {self._database_path.parent}"
            )

        try:
            connection = sqlite3.connect(self._database_path)
        except sqlite3.Error as error:
            raise PersistenceError("could not open SQLite database") from error

        try:
            self._configure(connection)
            version = self._schema_version(connection)
            if version not in {0, _SCHEMA_VERSION}:
                raise PersistenceError(
                    f"unsupported database schema version: {version}"
                )

            if version == 0:
                existing_tables = self._application_tables(connection)
                if existing_tables:
                    raise PersistenceError(
                        "database has an unversioned or partial job schema"
                    )
                with connection:
                    connection.execute(_CREATE_RAW_TABLE)
                    connection.execute(_CREATE_NORMALIZED_TABLE)
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

            self._validate_schema(connection)
        except PersistenceError:
            raise
        except sqlite3.Error as error:
            raise PersistenceError("could not initialize SQLite schema") from error
        finally:
            connection.close()

    def upsert_batch(self, jobs: Sequence[AcquiredJob]) -> None:
        """Atomically insert or update current jobs by source-local key."""
        with self._existing_connection() as connection:
            try:
                with connection:
                    for job in jobs:
                        connection.execute(_UPSERT_RAW, self._raw_values(job.raw))
                        connection.execute(
                            _UPSERT_NORMALIZED,
                            self._normalized_values(job.normalized),
                        )
            except sqlite3.Error as error:
                raise PersistenceError(
                    "could not persist acquired job batch"
                ) from error

    def get(self, key: SourceJobKey) -> AcquiredJob | None:
        """Return the current record for a source-local key, if present."""
        with self._existing_connection() as connection:
            try:
                row = connection.execute(
                    _SELECT_JOB,
                    (key.source, key.source_scope, key.source_job_id),
                ).fetchone()
            except sqlite3.Error as error:
                raise PersistenceError("could not retrieve acquired job") from error

        if row is None:
            return None

        try:
            return self._job_from_row(row)
        except (TypeError, ValueError) as error:
            raise PersistenceError("stored acquired job is invalid") from error

    @contextmanager
    def _existing_connection(self) -> Iterator[sqlite3.Connection]:
        database_uri = f"{self._database_path.resolve().as_uri()}?mode=rw"
        try:
            connection = sqlite3.connect(database_uri, uri=True)
        except sqlite3.Error as error:
            raise PersistenceError(
                "SQLite database is missing or cannot be opened"
            ) from error

        try:
            connection.row_factory = sqlite3.Row
            self._configure(connection)
            self._validate_schema(connection)
            yield connection
        except PersistenceError:
            raise
        except sqlite3.Error as error:
            raise PersistenceError("SQLite persistence operation failed") from error
        finally:
            connection.close()

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _schema_version(connection: sqlite3.Connection) -> int:
        row = connection.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    @staticmethod
    def _application_tables(connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name IN ('raw_source_jobs', 'normalized_jobs')
            """
        ).fetchall()
        return {str(row[0]) for row in rows}

    @classmethod
    def _validate_schema(cls, connection: sqlite3.Connection) -> None:
        version = cls._schema_version(connection)
        if version == 0:
            raise PersistenceError("SQLite schema has not been initialized")
        if version != _SCHEMA_VERSION:
            raise PersistenceError(f"unsupported database schema version: {version}")

        expected_columns = {
            "raw_source_jobs": _RAW_COLUMNS,
            "normalized_jobs": _NORMALIZED_COLUMNS,
        }
        for table_name, expected in expected_columns.items():
            rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            actual = {str(row[1]) for row in rows}
            if actual != expected:
                raise PersistenceError(
                    f"SQLite schema version {_SCHEMA_VERSION} has an invalid "
                    f"{table_name} table"
                )

    @staticmethod
    def _raw_values(raw: RawSourceRecord) -> tuple[str, ...]:
        return (
            raw.key.source,
            raw.key.source_scope,
            raw.key.source_job_id,
            raw.retrieved_at.isoformat(),
            raw.payload_json,
        )

    @staticmethod
    def _normalized_values(normalized: NormalizedJob) -> tuple[str | None, ...]:
        return (
            normalized.key.source,
            normalized.key.source_scope,
            normalized.key.source_job_id,
            normalized.company,
            normalized.title,
            normalized.description,
            normalized.location,
            normalized.job_url,
            normalized.apply_url,
            normalized.posted_at.isoformat() if normalized.posted_at else None,
            normalized.retrieved_at.isoformat(),
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> AcquiredJob:
        key = SourceJobKey(
            source=row["source"],
            source_scope=row["source_scope"],
            source_job_id=row["source_job_id"],
        )
        normalized = NormalizedJob(
            key=key,
            company=row["company"],
            title=row["title"],
            description=row["description"],
            location=row["location"],
            job_url=row["job_url"],
            apply_url=row["apply_url"],
            posted_at=(
                datetime.fromisoformat(row["posted_at"])
                if row["posted_at"] is not None
                else None
            ),
            retrieved_at=datetime.fromisoformat(row["normalized_retrieved_at"]),
        )
        raw = RawSourceRecord(
            key=key,
            retrieved_at=datetime.fromisoformat(row["raw_retrieved_at"]),
            payload_json=row["payload_json"],
        )
        return AcquiredJob(normalized=normalized, raw=raw)
