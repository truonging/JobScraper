"""Rebuildable FTS and processing cache, independent of acquisition storage."""

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from job_matcher.catalog import LinkedSourceJob
from job_matcher.filter_policy import FilterPolicy, load_filter_policy
from job_matcher.filtering import DeterministicFilterEvaluator
from job_matcher.models import RawSourceRecord
from job_matcher.screening import (
    SCREENING_VERSION,
    ScreeningOutcome,
    combine_outcomes,
    outcome_priority,
    screen_job,
)
from job_matcher.sqlite_repository import SQLiteJobRepository

INDEX_VERSION = 2
KEYS = "source, source_scope, source_job_id"
ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
REPAIR = "Run python -m job_matcher.search_index sync (or rebuild for a missing/corrupt/incompatible index)."


class SearchIndexError(ValueError):
    """An unavailable or stale index must never masquerade as current results."""


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def implementation_signature() -> str:
    """Conservatively invalidate on changes to rules, contracts or cache code."""
    root = Path(__file__).parent
    names = (
        "filter_policy.py",
        "filtering.py",
        "screening.py",
        "occupational_screening.py",
        "geographic_classification.py",
        "geographic_enforcement.py",
        "geography_terms.py",
        "models.py",
        "catalog.py",
        "search_index.py",
        "sqlite_repository.py",
    )
    return fingerprint(
        [
            SCREENING_VERSION,
            *[hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names],
        ]
    )


def catalog_stamp(database: str | Path) -> str:
    """Cheap conservative freshness guard, not a content-change detector.

    Real synchronization hashes content. File identity, nanosecond timestamps,
    SQLite change counter and WAL state detect ordinary external commits/replaces.
    """
    path = Path(database).resolve()
    parts = [str(path)]
    for file in (path, Path(str(path) + "-wal")):
        if file.exists():
            stat = file.stat()
            with file.open("rb") as stream:
                header = stream.read(100)
            parts.append(
                [
                    file.name,
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                    header.hex(),
                ]
            )
        else:
            parts.append(None)
    if parts[1] is None:
        raise SearchIndexError(f"Source catalog does not exist. {REPAIR}")
    return encode(parts)


def readonly(database: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        Path(database).resolve().as_uri() + "?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
    except BaseException:
        connection.close()
        raise
    return connection


SCHEMA = """
PRAGMA user_version=2;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE postings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL, source_scope TEXT NOT NULL, source_job_id TEXT NOT NULL,
    logical_job_id TEXT NOT NULL,
    title TEXT NOT NULL, company TEXT NOT NULL, location TEXT,
    employment_type TEXT, workplace_type TEXT, posted_at TEXT,
    status TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    inactive_at TEXT, retrieved_at TEXT NOT NULL, linked_at TEXT NOT NULL,
    text_hash TEXT NOT NULL, input_hash TEXT NOT NULL, metadata_hash TEXT NOT NULL,
    filter_outcome TEXT NOT NULL, screening_outcome TEXT NOT NULL, outcome TEXT NOT NULL,
    filter_reasons TEXT NOT NULL, screening_reasons TEXT NOT NULL, evidence TEXT NOT NULL,
    reasons TEXT NOT NULL, geography TEXT NOT NULL,
    policy_version INTEGER NOT NULL, policy_fingerprint TEXT NOT NULL,
    policy_signature TEXT NOT NULL, screening_version TEXT NOT NULL,
    implementation_signature TEXT NOT NULL, evaluated_at TEXT NOT NULL,
    evaluated_source_retrieved_at TEXT NOT NULL,
    UNIQUE(source, source_scope, source_job_id)
);
CREATE INDEX postings_logical ON postings(logical_job_id, source, source_scope, source_job_id);
CREATE INDEX postings_source_status ON postings(source, status, logical_job_id);
CREATE INDEX postings_discovered ON postings(first_seen_at, logical_job_id);
CREATE VIRTUAL TABLE text_index USING fts5(
    title, description, company, location,
    tokenize='trigram case_sensitive 1'
);
CREATE TABLE logical_jobs (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, outcome TEXT NOT NULL,
    posted_at TEXT
);
CREATE INDEX logical_first ON logical_jobs(outcome, first_seen DESC, id);
CREATE INDEX logical_last ON logical_jobs(outcome, last_seen DESC, id);
CREATE INDEX logical_company ON logical_jobs(outcome, company COLLATE NOCASE, title COLLATE NOCASE, id);
CREATE INDEX logical_title ON logical_jobs(outcome, title COLLATE NOCASE, company COLLATE NOCASE, id);
"""


@dataclass
class SyncStats:
    scanned: int = 0
    indexed: int = 0
    screened: int = 0
    metadata_updated: int = 0
    deleted: int = 0


def check_structure(connection):
    if connection.execute("PRAGMA user_version").fetchone()[0] != INDEX_VERSION:
        raise SearchIndexError(f"Incompatible search schema. {REPAIR}")
    meta = dict(connection.execute("SELECT key, value FROM meta"))
    if meta.get("complete") != "yes":
        raise SearchIndexError(f"Incomplete search index. {REPAIR}")
    return meta


def validate_index(connection, database, policy):
    meta = check_structure(connection)
    if meta.get("catalog_stamp") != catalog_stamp(database):
        raise SearchIndexError(f"Search index is stale: catalog changed. {REPAIR}")
    if meta.get("policy_signature") != fingerprint(asdict(policy)):
        raise SearchIndexError(
            f"Search index is stale: filter policy changed. {REPAIR}"
        )
    if meta.get("implementation_signature") != implementation_signature():
        raise SearchIndexError(
            f"Search index is stale: processing implementation changed. {REPAIR}"
        )
    return meta


def _decisions(row, policy, implementation):
    normalized = SQLiteJobRepository._normalized_from_identity_row(row)
    linked = LinkedSourceJob(normalized, SQLiteJobRepository._link_from_row(row))
    raw = RawSourceRecord(
        normalized.key,
        datetime.fromisoformat(row["raw_retrieved_at"]),
        row["payload_json"],
    )
    filtered = DeterministicFilterEvaluator(policy).evaluate((linked,))[0]
    screened = screen_job(linked, raw)
    outcome = combine_outcomes(filtered.outcome, screened.outcome).value
    reasons = ", ".join(
        [
            *(f"filter:{r.field.value}/{r.kind.value}" for r in filtered.reasons),
            *screened.reasons,
        ]
    )
    return dict(
        filter_outcome=filtered.outcome.value,
        screening_outcome=screened.outcome.value,
        outcome=outcome,
        filter_reasons=encode([asdict(r) for r in filtered.reasons]),
        screening_reasons=encode(screened.reasons),
        evidence=encode([asdict(e) for e in screened.evidence]),
        reasons=reasons,
        geography=screened.geography.value,
        policy_version=policy.version,
        policy_fingerprint=policy.fingerprint,
        policy_signature=fingerprint(asdict(policy)),
        screening_version=SCREENING_VERSION,
        implementation_signature=implementation,
        evaluated_at=screened.evaluated_at.isoformat(),
        evaluated_source_retrieved_at=row["retrieved_at"],
    )


def _aggregate(connection):
    """Same representative and outcome priority as the original dashboard."""
    # SQL is a projection of the shared ranking, not a second priority policy.
    active_priority = (
        "CASE "
        + " ".join(
            f"WHEN status='active' AND outcome='{outcome.value}' THEN {outcome_priority(outcome)}"
            for outcome in ScreeningOutcome
            if outcome_priority(outcome)
        )
        + f" ELSE {outcome_priority(ScreeningOutcome.EXCLUDE)} END"
    )
    priority_outcome = (
        "CASE priority "
        + " ".join(
            f"WHEN {outcome_priority(outcome)} THEN '{outcome.value}'"
            for outcome in ScreeningOutcome
            if outcome_priority(outcome)
        )
        + f" ELSE '{ScreeningOutcome.EXCLUDE.value}' END"
    )
    connection.execute("DELETE FROM logical_jobs")
    connection.execute(f"""
        INSERT INTO logical_jobs
        WITH ranked AS (
            SELECT *, row_number() OVER (
                PARTITION BY logical_job_id
                ORDER BY {active_priority} DESC,
                         source, source_scope, source_job_id
            ) AS position,
            min(first_seen_at) OVER (PARTITION BY logical_job_id) AS first_seen,
            max(last_seen_at) OVER (PARTITION BY logical_job_id) AS last_seen,
            max({active_priority})
                OVER (PARTITION BY logical_job_id) AS priority
            FROM postings
        )
        SELECT logical_job_id, title, company, first_seen, last_seen,
               {priority_outcome},
               (SELECT min(p.posted_at) FROM postings p WHERE p.logical_job_id=ranked.logical_job_id AND p.status='active')
        FROM ranked WHERE position=1
    """)


def _synchronize(connection, database, policy, progress, batch_size):
    start_stamp = catalog_stamp(database)
    implementation = implementation_signature()
    policy_signature = fingerprint(asdict(policy))
    stats = SyncStats()
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("CREATE TEMP TABLE seen (id INTEGER PRIMARY KEY)")
        with closing(readonly(database)) as catalog:
            catalog.execute("BEGIN")
            source_schema = catalog.execute("PRAGMA user_version").fetchone()[0]
            if source_schema not in (2, 3):
                raise SearchIndexError("Source catalog must have schema version 2 or 3")
            rows = catalog.execute(f"""
                SELECT n.*, s.logical_job_id, s.linked_at, l.status,
                       l.first_seen_at, l.last_seen_at, l.inactive_at,
                       r.payload_json, r.retrieved_at AS raw_retrieved_at
                FROM normalized_jobs n
                JOIN source_job_links s USING ({KEYS})
                JOIN source_job_lifecycle l USING ({KEYS})
                JOIN raw_source_jobs r USING ({KEYS})
            """)
            while batch := rows.fetchmany(batch_size):
                for row in batch:
                    stats.scanned += 1
                    key = tuple(
                        row[k] for k in ("source", "source_scope", "source_job_id")
                    )
                    old = connection.execute(
                        "SELECT * FROM postings WHERE source=? AND source_scope=? AND source_job_id=?",
                        key,
                    ).fetchone()
                    text = [
                        row[k] or ""
                        for k in ("title", "description", "company", "location")
                    ]
                    text_hash = fingerprint(text)
                    # Hash all raw payload fields conservatively, without storing the payload.
                    input_hash = fingerprint(
                        [text, row["logical_job_id"], row["payload_json"]]
                    )
                    metadata = {
                        k: row[k]
                        for k in (
                            "source",
                            "source_scope",
                            "source_job_id",
                            "logical_job_id",
                            "title",
                            "company",
                            "location",
                            "status",
                            "first_seen_at",
                            "last_seen_at",
                            "inactive_at",
                            "retrieved_at",
                            "linked_at",
                        )
                    }
                    for field in ("employment_type", "workplace_type", "posted_at"):
                        metadata[field] = row[field] if field in row.keys() else None
                    if metadata["posted_at"]:
                        metadata["posted_at"] = (
                            datetime.fromisoformat(metadata["posted_at"])
                            .astimezone(UTC)
                            .isoformat()
                        )
                    metadata_hash = fingerprint(metadata)
                    needs_screen = (
                        old is None
                        or old["input_hash"] != input_hash
                        or old["policy_signature"] != policy_signature
                        or old["implementation_signature"] != implementation
                    )
                    needs_text = old is None or old["text_hash"] != text_hash
                    if (
                        needs_screen
                        or needs_text
                        or old["metadata_hash"] != metadata_hash
                    ):
                        values = dict(
                            metadata,
                            text_hash=text_hash,
                            input_hash=input_hash,
                            metadata_hash=metadata_hash,
                        )
                        if needs_screen:
                            values.update(_decisions(row, policy, implementation))
                            stats.screened += 1
                        if old is None:
                            names = list(values)
                            cursor = connection.execute(
                                f"INSERT INTO postings ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                                list(values.values()),
                            )
                            posting_id = cursor.lastrowid
                        else:
                            posting_id = old["id"]
                            connection.execute(
                                f"UPDATE postings SET {', '.join(name + '=?' for name in values)} WHERE id=?",
                                (*values.values(), posting_id),
                            )
                            stats.metadata_updated += 1
                        if needs_text:
                            connection.execute(
                                "DELETE FROM text_index WHERE rowid=?", (posting_id,)
                            )
                            connection.execute(
                                "INSERT INTO text_index(rowid, title, description, company, location) VALUES (?, ?, ?, ?, ?)",
                                (posting_id, *(t.translate(ASCII_LOWER) for t in text)),
                            )
                            stats.indexed += 1
                    else:
                        posting_id = old["id"]
                    connection.execute("INSERT INTO seen VALUES (?)", (posting_id,))
                progress(
                    f"Scanned {stats.scanned:,}; indexed {stats.indexed:,}; screened {stats.screened:,}"
                )
            if catalog_stamp(database) != start_stamp:
                raise SearchIndexError(
                    "Catalog changed during synchronization; retry after acquisition finishes"
                )
        stats.deleted = connection.execute(
            "SELECT count(*) FROM postings WHERE id NOT IN (SELECT id FROM seen)"
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM text_index WHERE rowid IN (SELECT id FROM postings WHERE id NOT IN (SELECT id FROM seen))"
        )
        connection.execute("DELETE FROM postings WHERE id NOT IN (SELECT id FROM seen)")
        if stats.indexed or stats.screened or stats.metadata_updated or stats.deleted:
            _aggregate(connection)
        meta = dict(
            complete="yes",
            catalog_stamp=start_stamp,
            policy_signature=policy_signature,
            implementation_signature=implementation,
            synced_at=datetime.now(UTC).isoformat(),
            source_schema=str(source_schema),
            index_version=str(INDEX_VERSION),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)", meta.items()
        )
        if (
            connection.execute("SELECT count(*) FROM postings").fetchone()[0]
            != stats.scanned
        ):
            raise SearchIndexError("Index posting count failed validation")
        connection.execute("DROP TABLE seen")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return stats


def synchronize(
    database: str | Path,
    index: str | Path,
    policy: FilterPolicy,
    *,
    rebuild: bool = False,
    progress: Callable[[str], None] = print,
    batch_size: int = 250,
) -> SyncStats:
    """Atomic full build or transactional, idempotent incremental synchronization."""
    database, index = Path(database).resolve(), Path(index).resolve()
    if database == index or (index.exists() and os.path.samefile(database, index)):
        raise SearchIndexError(
            "Source catalog and search index must be different files"
        )
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    temporary = None
    if rebuild:
        fd, filename = tempfile.mkstemp(
            prefix=index.stem + "-", suffix=".building.sqlite3", dir=index.parent
        )
        os.close(fd)
        temporary = Path(filename)
    elif not index.is_file():
        raise SearchIndexError(f"Search index is missing. {REPAIR}")
    try:
        with closing(sqlite3.connect(temporary or index)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA cache_size=-16384")
            if rebuild:
                connection.executescript(SCHEMA)
            else:
                check_structure(connection)
            stats = _synchronize(connection, database, policy, progress, batch_size)
            if rebuild:
                connection.execute(
                    "INSERT INTO text_index(text_index) VALUES ('optimize')"
                )
                connection.commit()
                connection.execute(
                    "INSERT INTO text_index(text_index) VALUES ('integrity-check')"
                )
                connection.commit()
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise SearchIndexError("Search database integrity check failed")
        if temporary:
            if catalog_stamp(database) != dict_meta_stamp(temporary):
                raise SearchIndexError(
                    "Catalog changed before index publication; retry"
                )
            os.replace(temporary, index)
        return stats
    except sqlite3.Error as error:
        raise SearchIndexError(
            f"Could not synchronize search index: {error}. {REPAIR}"
        ) from error
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def dict_meta_stamp(index):
    with closing(readonly(index)) as connection:
        return check_structure(connection)["catalog_stamp"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["rebuild", "sync"])
    parser.add_argument("--catalog", default="phase2_validation.sqlite3")
    parser.add_argument("--index", default="job_search.sqlite3")
    parser.add_argument("--policy", default="examples/filter_policy.example.toml")
    args = parser.parse_args()
    try:
        stats = synchronize(
            args.catalog,
            args.index,
            load_filter_policy(args.policy),
            rebuild=args.command == "rebuild",
        )
        print(encode(asdict(stats)))
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
