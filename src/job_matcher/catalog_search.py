"""Read-only, paginated logical catalog search using transient screening."""

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from urllib.parse import urlsplit

from job_matcher.catalog import LinkedSourceJob
from job_matcher.filter_policy import FilterPolicy
from job_matcher.filtering import DeterministicFilterEvaluator
from job_matcher.models import RawSourceRecord
from job_matcher.screening import (
    ScreeningOutcome,
    aggregate_outcomes,
    combine_outcomes,
    screen_job,
)
from job_matcher.sqlite_repository import SQLiteJobRepository


@dataclass(frozen=True)
class SearchCriteria:
    keywords: str = ""
    mode: str = "AND"
    fields: str = "title_description"
    source: str = "all"
    outcome: str = "keep"
    company: str = ""
    location: str = ""
    include_inactive: bool = False
    discovered_since: str = ""
    sort: str = "first_seen"
    employment_type: str = ""
    workplace_type: str = ""


@dataclass(frozen=True)
class SearchPage:
    total: int
    jobs: tuple[dict, ...]


def parse_keywords(value: str, mode: str) -> tuple[tuple[str, ...], str]:
    """Quoted phrases are literals; permit one uniform explicit operator."""
    if mode not in {"AND", "OR"}:
        raise ValueError("Matching mode must be AND or OR")
    if value.count('"') % 2:
        raise ValueError("Close the quoted phrase")
    tokens = re.findall(r'"[^"]*"|[^\s"]+', value)
    operators = {t for t in tokens if t in {"AND", "OR"}}
    if len(operators) > 1:
        raise ValueError("Use a single AND or OR operator")
    return tuple(
        t.strip('"') for t in tokens if t not in operators and t.strip('"')
    ), next(iter(operators), mode)


def safe_url(value: str | None) -> str | None:
    try:
        parsed = urlsplit(value or "")
        if (
            parsed.scheme in {"https", "http"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not any(c.isspace() or ord(c) < 32 for c in value)
        ):
            return value
    except ValueError:
        pass
    return None


def _pattern(value: str) -> str:
    return (
        "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    )


_SORTS = {
    "posted_newest": "posted_at IS NULL, posted_at DESC",
    "posted_oldest": "posted_at IS NULL, posted_at ASC",
    "first_seen": "first_seen DESC",
    "last_seen": "last_seen DESC",
    "company": "company COLLATE NOCASE, title COLLATE NOCASE",
    "title": "title COLLATE NOCASE, company COLLATE NOCASE",
}
_FIELDS = {
    "title": ("title",),
    "title_description": ("title", "description"),
    "all": ("title", "description", "company", "location"),
}
_KEYS = "source, source_scope, source_job_id"


def search_catalog(
    database: str | Path,
    policy: FilterPolicy,
    criteria: SearchCriteria,
    *,
    page: int = 1,
    page_size: int = 25,
) -> SearchPage:
    """Match one representation, screen all its siblings, paginate metadata.

    Inactive siblings never establish KEEP. Unlinked postings are omitted.
    Search predicates must all match the same representation. Logical screening
    considers all active siblings, independently of search/source predicates.
    """
    if page < 1 or page_size not in {25, 50, 100}:
        raise ValueError("Invalid page or page size")
    if criteria.fields not in _FIELDS or criteria.sort not in _SORTS:
        raise ValueError("Invalid fields or sort")
    if criteria.source not in {
        "all",
        "lever",
        "greenhouse",
    } or criteria.outcome not in {"all", "keep", "exclude", "defer"}:
        raise ValueError("Invalid source or outcome")
    terms, mode = parse_keywords(criteria.keywords, criteria.mode)
    conditions, params = [], []
    for term in terms:
        conditions.append(
            "("
            + " OR ".join(
                f"coalesce(n.{field}, '') LIKE ? ESCAPE '\\'"
                for field in _FIELDS[criteria.fields]
            )
            + ")"
        )
        params.extend([_pattern(term)] * len(_FIELDS[criteria.fields]))
    where = ["(" + f" {mode} ".join(conditions) + ")"] if conditions else ["1=1"]
    if not criteria.include_inactive:
        where.append("l.status = 'active'")
    if criteria.source != "all":
        where.append("n.source = ?")
        params.append(criteria.source)
    for field, value in (
        ("company", criteria.company),
        ("location", criteria.location),
    ):
        if value:
            where.append(f"coalesce(n.{field}, '') LIKE ? ESCAPE '\\'")
            params.append(_pattern(value))
    for field in ("employment_type", "workplace_type"):
        if value := getattr(criteria, field):
            where.append(f"n.{field} = ?")
            params.append(value)
    if criteria.discovered_since:
        datetime.fromisoformat(criteria.discovered_since)
        where.append("l.first_seen_at >= ?")
        params.append(criteria.discovered_since)
    evaluator = DeterministicFilterEvaluator(policy)
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with (
        closing(sqlite3.connect(uri, uri=True)) as catalog,
        closing(sqlite3.connect(":memory:")) as results,
    ):
        catalog.row_factory = sqlite3.Row
        catalog.execute("PRAGMA query_only=ON")
        catalog.execute("BEGIN")
        if catalog.execute("PRAGMA user_version").fetchone()[0] not in (2, 3):
            raise ValueError("Dashboard requires an existing schema v2 catalog")
        results.row_factory = sqlite3.Row
        results.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, first_seen TEXT, last_seen TEXT, outcome TEXT, posted_at TEXT)"
        )
        results.execute(
            "CREATE TABLE reps (id TEXT, source TEXT, source_scope TEXT, source_job_id TEXT, outcome TEXT, reasons TEXT)"
        )
        results.execute("CREATE INDEX reps_id ON reps(id)")
        rows = catalog.execute(
            f"""
            WITH candidates AS MATERIALIZED (
                SELECT DISTINCT s.logical_job_id FROM normalized_jobs n
                JOIN source_job_links s USING ({_KEYS})
                JOIN source_job_lifecycle l USING ({_KEYS})
                WHERE {" AND ".join(where)}
            )
            SELECT n.*, s.logical_job_id, s.linked_at, l.status,
                   l.first_seen_at, l.last_seen_at,
                   r.payload_json, r.retrieved_at AS raw_retrieved_at
            FROM candidates c
            JOIN source_job_links s ON s.logical_job_id = c.logical_job_id
            JOIN normalized_jobs n USING ({_KEYS})
            JOIN source_job_lifecycle l USING ({_KEYS})
            JOIN raw_source_jobs r USING ({_KEYS})
            ORDER BY s.logical_job_id, n.source, n.source_scope, n.source_job_id
        """,
            params,
        )
        for logical_id, representations in groupby(rows, lambda r: r["logical_job_id"]):
            outcome, first, last, representative = (
                ScreeningOutcome.EXCLUDE,
                None,
                None,
                None,
            )
            posted = []
            for row in representations:
                if row["status"] == "active" and row["posted_at"]:
                    posted.append(
                        datetime.fromisoformat(row["posted_at"])
                        .astimezone(UTC)
                        .isoformat()
                    )
                normalized = SQLiteJobRepository._normalized_from_identity_row(row)
                linked = LinkedSourceJob(
                    normalized, SQLiteJobRepository._link_from_row(row)
                )
                raw = RawSourceRecord(
                    normalized.key,
                    datetime.fromisoformat(row["raw_retrieved_at"]),
                    row["payload_json"],
                )
                filtered = evaluator.evaluate((linked,))[0]
                screened = screen_job(linked, raw)
                effective = combine_outcomes(filtered.outcome, screened.outcome)
                reasons = ", ".join(
                    [
                        *(
                            f"filter:{r.field.value}/{r.kind.value}"
                            for r in filtered.reasons
                        ),
                        *screened.reasons,
                    ]
                )
                results.execute(
                    "INSERT INTO reps VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        logical_id,
                        row["source"],
                        row["source_scope"],
                        row["source_job_id"],
                        effective,
                        reasons,
                    ),
                )
                if representative is None:
                    representative = (row["title"], row["company"])
                first = min(first or row["first_seen_at"], row["first_seen_at"])
                last = max(last or row["last_seen_at"], row["last_seen_at"])
                if row["status"] == "active":
                    preferred = aggregate_outcomes((outcome, effective))
                    if preferred != outcome:
                        outcome = preferred
                        representative = (row["title"], row["company"])
            results.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    logical_id,
                    *representative,
                    first,
                    last,
                    outcome,
                    min(posted) if posted else None,
                ),
            )
        clause, bindings = (
            ("", ())
            if criteria.outcome == "all"
            else ("WHERE outcome = ?", (criteria.outcome,))
        )
        total = results.execute(
            f"SELECT count(*) FROM jobs {clause}", bindings
        ).fetchone()[0]
        selected = results.execute(
            f"SELECT * FROM jobs {clause} ORDER BY {_SORTS[criteria.sort]}, id LIMIT ? OFFSET ?",
            (*bindings, page_size, (page - 1) * page_size),
        )
        jobs = []
        for summary in selected:
            job = dict(summary)
            job["representations"] = []
            for rep in results.execute(
                "SELECT * FROM reps WHERE id = ? ORDER BY source, source_scope, source_job_id",
                (summary["id"],),
            ):
                detail = dict(
                    catalog.execute(
                        f"SELECT n.*, l.status, l.first_seen_at, l.last_seen_at FROM normalized_jobs n JOIN source_job_lifecycle l USING ({_KEYS}) WHERE n.source=? AND n.source_scope=? AND n.source_job_id=?",
                        (rep["source"], rep["source_scope"], rep["source_job_id"]),
                    ).fetchone()
                )
                detail.update(outcome=rep["outcome"], reasons=rep["reasons"])
                job["representations"].append(detail)
            jobs.append(job)
        return SearchPage(total, tuple(jobs))
