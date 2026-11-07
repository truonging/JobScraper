"""Offline, transactional metadata enrichment from current stored raw records."""

import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime

from job_matcher.job_metadata import extract_metadata


def backfill(
    database,
    *,
    fields=("employment_type", "workplace_type", "posted_at"),
    require_unchanged_existing=False,
):
    """Fill missing values only, including inactive records; safe to rerun.

    Caller migrates schema explicitly and takes a consistent backup first.
    A transaction commits all changes or none. No acquisition or screening runs.
    Existing non-NULL metadata remains authoritative until explicitly reviewed.
    ``fields`` can restrict enrichment to employment/workplace without filling
    publication dates. Strict mode aborts and rolls back if extraction disagrees
    with any selected existing classification, including returning NULL.
    """
    if not fields or set(fields) - {"employment_type", "workplace_type", "posted_at"}:
        raise ValueError("Select supported metadata fields")
    counts = defaultdict(Counter)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        for row in connection.execute("""
            SELECT n.*, r.payload_json FROM normalized_jobs n
            JOIN raw_source_jobs r USING (source, source_scope, source_job_id)
        """):
            count = counts[row["source"]]
            count["total"] += 1
            try:
                payload = json.loads(row["payload_json"])
                if not isinstance(payload, dict):
                    raise ValueError("payload must be an object")
                values = extract_metadata(
                    row["source"], payload, datetime.fromisoformat(row["retrieved_at"])
                )
            except (ValueError, TypeError):
                if require_unchanged_existing:
                    raise
                count["invalid_payload"] += 1
                continue
            updates = {}
            for field, value in values.items():
                if field not in fields:
                    continue
                if field == "posted_at" and value is not None:
                    value = value.isoformat()
                if (
                    require_unchanged_existing
                    and row[field] is not None
                    and row[field] != value
                ):
                    raise ValueError("Backfill would change an existing classification")
                if row[field] is None and value is not None:
                    updates[field] = value
                    count[field + "_enriched"] += 1
                if row[field] is not None or value is not None:
                    count[field + "_populated"] += 1
                else:
                    count[field + "_missing"] += 1
            if updates:
                connection.execute(
                    "UPDATE normalized_jobs SET "
                    + ", ".join(f"{field}=?" for field in updates)
                    + " WHERE source=? AND source_scope=? AND source_job_id=?",
                    (
                        *updates.values(),
                        row["source"],
                        row["source_scope"],
                        row["source_job_id"],
                    ),
                )
    return {source: dict(count) for source, count in counts.items()}
