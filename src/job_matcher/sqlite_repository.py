"""SQLite implementation of current-job and lifecycle persistence."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from job_matcher.catalog import SourceJobLifecycle, SourceJobStatus
from job_matcher.models import (
    AcquiredJob,
    NormalizedJob,
    RawSourceRecord,
    SourceJobKey,
    SourceSnapshot,
)
from job_matcher.ports import PersistenceError

_SCHEMA_VERSION = 2

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
_SCOPE_STATE_COLUMNS = {"source", "source_scope", "last_retrieved_at"}
_LIFECYCLE_COLUMNS = {
    "source",
    "source_scope",
    "source_job_id",
    "status",
    "first_seen_at",
    "last_seen_at",
    "inactive_at",
}
_LOGICAL_JOB_COLUMNS = {"logical_job_id"}
_SOURCE_LINK_COLUMNS = {
    "source",
    "source_scope",
    "source_job_id",
    "logical_job_id",
    "linked_at",
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

_CREATE_SCOPE_STATE_TABLE = """
CREATE TABLE source_scope_state (
    source TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    last_retrieved_at TEXT NOT NULL,
    PRIMARY KEY (source, source_scope)
)
"""

_CREATE_LIFECYCLE_TABLE = """
CREATE TABLE source_job_lifecycle (
    source TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    inactive_at TEXT,
    PRIMARY KEY (source, source_scope, source_job_id),
    FOREIGN KEY (source, source_scope, source_job_id)
        REFERENCES normalized_jobs (source, source_scope, source_job_id)
        ON DELETE CASCADE,
    CHECK (first_seen_at <= last_seen_at),
    CHECK (
        (status = 'active' AND inactive_at IS NULL)
        OR
        (status = 'inactive' AND inactive_at IS NOT NULL)
    ),
    CHECK (inactive_at IS NULL OR last_seen_at <= inactive_at)
)
"""

_CREATE_LOGICAL_JOB_TABLE = """
CREATE TABLE logical_jobs (
    logical_job_id TEXT NOT NULL PRIMARY KEY
)
"""

_CREATE_SOURCE_LINK_TABLE = """
CREATE TABLE source_job_links (
    source TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    logical_job_id TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (source, source_scope, source_job_id),
    FOREIGN KEY (source, source_scope, source_job_id)
        REFERENCES normalized_jobs (source, source_scope, source_job_id)
        ON DELETE CASCADE,
    FOREIGN KEY (logical_job_id)
        REFERENCES logical_jobs (logical_job_id)
        ON DELETE RESTRICT
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

_MARK_SCOPE_INACTIVE = """
UPDATE source_job_lifecycle
SET status = 'inactive', inactive_at = ?
WHERE source = ? AND source_scope = ? AND status = 'active'
"""

_UPSERT_ACTIVE_LIFECYCLE = """
INSERT INTO source_job_lifecycle (
    source,
    source_scope,
    source_job_id,
    status,
    first_seen_at,
    last_seen_at,
    inactive_at
) VALUES (?, ?, ?, 'active', ?, ?, NULL)
ON CONFLICT (source, source_scope, source_job_id) DO UPDATE SET
    status = 'active',
    last_seen_at = excluded.last_seen_at,
    inactive_at = NULL
"""

_UPSERT_SCOPE_STATE = """
INSERT INTO source_scope_state (source, source_scope, last_retrieved_at)
VALUES (?, ?, ?)
ON CONFLICT (source, source_scope) DO UPDATE SET
    last_retrieved_at = excluded.last_retrieved_at
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

_SELECT_LIFECYCLE = """
SELECT
    source,
    source_scope,
    source_job_id,
    status,
    first_seen_at,
    last_seen_at,
    inactive_at
FROM source_job_lifecycle
WHERE source = ? AND source_scope = ? AND source_job_id = ?
"""

_V1_TABLES = {"raw_source_jobs", "normalized_jobs"}
_V2_COLUMNS = {
    "raw_source_jobs": _RAW_COLUMNS,
    "normalized_jobs": _NORMALIZED_COLUMNS,
    "source_scope_state": _SCOPE_STATE_COLUMNS,
    "source_job_lifecycle": _LIFECYCLE_COLUMNS,
    "logical_jobs": _LOGICAL_JOB_COLUMNS,
    "source_job_links": _SOURCE_LINK_COLUMNS,
}


class SQLiteJobRepository:
    """Persist current source jobs and lifecycle state in SQLite."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    def initialize_schema(self) -> None:
        """Create schema v2, migrate v1, or validate an existing v2 schema."""
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
            if version == 0:
                if self._application_tables(connection):
                    raise PersistenceError(
                        "database has an unversioned or partial job schema"
                    )
                self._create_v2_schema(connection)
            elif version == 1:
                self._validate_schema_version(connection, 1)
                self._migrate_v1_to_v2(connection)
            elif version != _SCHEMA_VERSION:
                raise PersistenceError(
                    f"unsupported database schema version: {version}"
                )

            self._validate_schema_version(connection, _SCHEMA_VERSION)
        except PersistenceError:
            raise
        except sqlite3.Error as error:
            raise PersistenceError("could not initialize SQLite schema") from error
        finally:
            connection.close()

    def reconcile_snapshot(self, snapshot: SourceSnapshot) -> None:
        """Atomically store a strictly newer snapshot and reconcile lifecycle."""
        with self._existing_connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_newer_snapshot(connection, snapshot)

                for job in snapshot.jobs:
                    connection.execute(_UPSERT_RAW, self._raw_values(job.raw))
                    connection.execute(
                        _UPSERT_NORMALIZED,
                        self._normalized_values(job.normalized),
                    )

                retrieved_at = snapshot.retrieved_at.isoformat()
                connection.execute(
                    _MARK_SCOPE_INACTIVE,
                    (retrieved_at, snapshot.source, snapshot.source_scope),
                )
                for job in snapshot.jobs:
                    key = job.normalized.key
                    connection.execute(
                        _UPSERT_ACTIVE_LIFECYCLE,
                        (
                            key.source,
                            key.source_scope,
                            key.source_job_id,
                            retrieved_at,
                            retrieved_at,
                        ),
                    )

                connection.execute(
                    _UPSERT_SCOPE_STATE,
                    (snapshot.source, snapshot.source_scope, retrieved_at),
                )
                connection.commit()
            except PersistenceError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise PersistenceError("could not reconcile source snapshot") from error

    def get(self, key: SourceJobKey) -> AcquiredJob | None:
        """Return the current record for a source-local key, if present."""
        with self._existing_connection() as connection:
            try:
                row = connection.execute(_SELECT_JOB, self._key_values(key)).fetchone()
            except sqlite3.Error as error:
                raise PersistenceError("could not retrieve acquired job") from error

        if row is None:
            return None
        try:
            return self._job_from_row(row)
        except (TypeError, ValueError) as error:
            raise PersistenceError("stored acquired job is invalid") from error

    def get_lifecycle(self, key: SourceJobKey) -> SourceJobLifecycle | None:
        """Return current source-posting lifecycle state, if present."""
        with self._existing_connection() as connection:
            try:
                row = connection.execute(
                    _SELECT_LIFECYCLE, self._key_values(key)
                ).fetchone()
            except sqlite3.Error as error:
                raise PersistenceError(
                    "could not retrieve source job lifecycle"
                ) from error

        if row is None:
            return None
        try:
            return self._lifecycle_from_row(row)
        except (TypeError, ValueError) as error:
            raise PersistenceError("stored source job lifecycle is invalid") from error

    @classmethod
    def _create_v2_schema(cls, connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_CREATE_RAW_TABLE)
            connection.execute(_CREATE_NORMALIZED_TABLE)
            cls._create_v2_tables(connection)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise PersistenceError("could not create SQLite schema") from error

    @classmethod
    def _migrate_v1_to_v2(cls, connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            cls._create_v2_tables(connection)
            connection.execute(
                """
                INSERT INTO source_job_lifecycle (
                    source,
                    source_scope,
                    source_job_id,
                    status,
                    first_seen_at,
                    last_seen_at,
                    inactive_at
                )
                SELECT
                    source,
                    source_scope,
                    source_job_id,
                    'active',
                    retrieved_at,
                    retrieved_at,
                    NULL
                FROM normalized_jobs
                """
            )
            connection.execute(
                """
                INSERT INTO source_scope_state (
                    source, source_scope, last_retrieved_at
                )
                SELECT source, source_scope, MAX(retrieved_at)
                FROM normalized_jobs
                GROUP BY source, source_scope
                """
            )
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise PersistenceError(
                "could not migrate SQLite schema from version 1 to version 2"
            ) from error

    @staticmethod
    def _create_v2_tables(connection: sqlite3.Connection) -> None:
        connection.execute(_CREATE_SCOPE_STATE_TABLE)
        connection.execute(_CREATE_LIFECYCLE_TABLE)
        connection.execute(_CREATE_LOGICAL_JOB_TABLE)
        connection.execute(_CREATE_SOURCE_LINK_TABLE)

    @staticmethod
    def _require_newer_snapshot(
        connection: sqlite3.Connection, snapshot: SourceSnapshot
    ) -> None:
        row = connection.execute(
            """
            SELECT last_retrieved_at
            FROM source_scope_state
            WHERE source = ? AND source_scope = ?
            """,
            (snapshot.source, snapshot.source_scope),
        ).fetchone()
        if row is None:
            return
        try:
            last_retrieved_at = datetime.fromisoformat(row[0])
        except (TypeError, ValueError) as error:
            raise PersistenceError(
                "stored source-scope checkpoint is invalid"
            ) from error
        if last_retrieved_at.tzinfo is not UTC:
            raise PersistenceError("stored source-scope checkpoint is invalid")
        if snapshot.retrieved_at <= last_retrieved_at:
            raise PersistenceError(
                "source snapshot retrieved_at must be later than the last "
                "successful snapshot"
            )

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
            self._validate_schema_version(connection, _SCHEMA_VERSION)
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
        names = _V1_TABLES | set(_V2_COLUMNS)
        placeholders = ", ".join("?" for _ in names)
        rows = connection.execute(
            f"SELECT name FROM sqlite_master "
            f"WHERE type = 'table' AND name IN ({placeholders})",
            tuple(sorted(names)),
        ).fetchall()
        return {str(row[0]) for row in rows}

    @classmethod
    def _validate_schema_version(
        cls, connection: sqlite3.Connection, version: int
    ) -> None:
        actual_version = cls._schema_version(connection)
        if actual_version == 0:
            raise PersistenceError("SQLite schema has not been initialized")
        if actual_version != version:
            raise PersistenceError(
                f"unsupported database schema version: {actual_version}"
            )

        expected_columns = (
            {
                "raw_source_jobs": _RAW_COLUMNS,
                "normalized_jobs": _NORMALIZED_COLUMNS,
            }
            if version == 1
            else _V2_COLUMNS
        )
        for table_name, expected in expected_columns.items():
            rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            actual = {str(row[1]) for row in rows}
            if actual != expected:
                raise PersistenceError(
                    f"SQLite schema version {version} has an invalid {table_name} table"
                )

    @staticmethod
    def _key_values(key: SourceJobKey) -> tuple[str, str, str]:
        return (key.source, key.source_scope, key.source_job_id)

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

    @staticmethod
    def _lifecycle_from_row(row: sqlite3.Row) -> SourceJobLifecycle:
        return SourceJobLifecycle(
            key=SourceJobKey(
                source=row["source"],
                source_scope=row["source_scope"],
                source_job_id=row["source_job_id"],
            ),
            status=SourceJobStatus(row["status"]),
            first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
            inactive_at=(
                datetime.fromisoformat(row["inactive_at"])
                if row["inactive_at"] is not None
                else None
            ),
        )
