"""Read-only SQLite projection for pre-AI screening; no schema initialization."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from job_matcher.catalog import LinkedSourceJob
from job_matcher.models import RawSourceRecord
from job_matcher.ports import PersistenceError
from job_matcher.sqlite_repository import SQLiteJobRepository


def read_active_screening_inputs(
    database: str | Path,
) -> Iterator[tuple[LinkedSourceJob, RawSourceRecord]]:
    """Stream a consistent existing v2 catalog without writing or migrating it."""
    try:
        connection = sqlite3.connect(
            Path(database).resolve().as_uri() + "?mode=ro", uri=True
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if connection.execute("PRAGMA user_version").fetchone()[0] not in (2, 3):
                raise PersistenceError(
                    "screening requires an existing schema v2 catalog"
                )
            rows = connection.execute("""
                SELECT n.*, s.logical_job_id, s.linked_at,
                       r.payload_json, r.retrieved_at AS raw_retrieved_at
                FROM normalized_jobs n
                JOIN source_job_links s USING (source, source_scope, source_job_id)
                JOIN source_job_lifecycle l USING (source, source_scope, source_job_id)
                JOIN raw_source_jobs r USING (source, source_scope, source_job_id)
                WHERE l.status = 'active'
                ORDER BY n.source, n.source_scope, n.source_job_id
            """)
            for row in rows:
                normalized = SQLiteJobRepository._normalized_from_identity_row(row)
                link = SQLiteJobRepository._link_from_row(row)
                yield (
                    LinkedSourceJob(normalized, link),
                    RawSourceRecord(
                        normalized.key,
                        datetime.fromisoformat(row["raw_retrieved_at"]),
                        row["payload_json"],
                    ),
                )
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise PersistenceError("could not read screening catalog") from error
