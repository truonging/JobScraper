"""Fast discovery from a validated search index; details from the source catalog."""

import sqlite3
from contextlib import closing
from datetime import datetime
from time import perf_counter

from job_matcher.catalog_search import (
    _FIELDS,
    _SORTS,
    SearchCriteria,
    SearchPage,
    _pattern,
    parse_keywords,
)
from job_matcher.search_index import (
    ASCII_LOWER,
    KEYS,
    REPAIR,
    SearchIndexError,
    catalog_stamp,
    readonly,
    validate_index,
)


def _predicates(criteria):
    if criteria.fields not in _FIELDS or criteria.sort not in _SORTS:
        raise ValueError("Invalid fields or sort")
    if criteria.source not in {
        "all",
        "lever",
        "greenhouse",
    } or criteria.outcome not in {"all", "keep", "exclude", "defer"}:
        raise ValueError("Invalid source or outcome")
    terms, mode = parse_keywords(criteria.keywords, criteria.mode)
    predicates, parameters = [], []
    terms_sql = []
    for term in terms:
        if len(term) >= 3 and "\0" not in term:
            # Quoting makes ALL punctuation and MATCH operators literal text.
            phrase = '"' + term.translate(ASCII_LOWER).replace('"', '""') + '"'
            columns = " ".join(_FIELDS[criteria.fields])
            terms_sql.append(
                "n.id IN (SELECT rowid FROM text_index WHERE text_index MATCH ?)"
            )
            parameters.append("{" + columns + "} : " + phrase)
        else:
            # Trigrams cannot find <3-character strings. Scan only indexed text.
            terms_sql.append(
                "n.id IN (SELECT rowid FROM text_index WHERE "
                + " OR ".join(
                    f"{column} LIKE ? ESCAPE '\\'"
                    for column in _FIELDS[criteria.fields]
                )
                + ")"
            )
            parameters.extend(
                [_pattern(term.translate(ASCII_LOWER))] * len(_FIELDS[criteria.fields])
            )
    if terms_sql:
        predicates.append("(" + f" {mode} ".join(terms_sql) + ")")
    if not criteria.include_inactive:
        predicates.append("n.status='active'")
    if criteria.source != "all":
        predicates.append("n.source=?")
        parameters.append(criteria.source)
    for field in ("company", "location"):
        if value := getattr(criteria, field):
            predicates.append(f"coalesce(n.{field}, '') LIKE ? ESCAPE '\\'")
            parameters.append(_pattern(value))
    for field in ("employment_type", "workplace_type"):
        if value := getattr(criteria, field):
            predicates.append(f"n.{field}=?")
            parameters.append(value)
    if criteria.discovered_since:
        datetime.fromisoformat(criteria.discovered_since)
        predicates.append("n.first_seen_at >= ?")
        parameters.append(criteria.discovered_since)
    return " AND ".join(predicates) or "1=1", parameters


def search_index(
    database,
    index,
    policy,
    criteria: SearchCriteria,
    *,
    page=1,
    page_size=25,
    timings: dict | None = None,
) -> SearchPage:
    """Never screen, mutate either database, or cache results for a query."""
    if page < 1 or page_size not in {25, 50, 100}:
        raise ValueError("Invalid page or page size")
    where, parameters = _predicates(criteria)
    started = perf_counter()
    measured = {}
    try:
        with closing(readonly(index)) as search, closing(readonly(database)) as catalog:
            search.execute("BEGIN")
            catalog.execute("BEGIN")
            if catalog.execute("PRAGMA user_version").fetchone()[0] not in (2, 3):
                raise SearchIndexError(
                    f"Source catalog schema is incompatible. {REPAIR}"
                )
            meta = validate_index(search, database, policy)
            measured["validation"] = perf_counter() - started
            start = perf_counter()
            # Materialize identifiers, not descriptions. Reused for count and page.
            # Each statement stays read-only; SQLite owns temporary query storage.
            cte = f"WITH matching AS MATERIALIZED (SELECT DISTINCT n.logical_job_id FROM postings n WHERE {where}) "
            outcome_clause = "" if criteria.outcome == "all" else "WHERE j.outcome=?"
            bindings = (*parameters, *((criteria.outcome,) if outcome_clause else ()))
            selection = (
                "FROM logical_jobs j JOIN matching m ON m.logical_job_id=j.id "
                + outcome_clause
            )
            total = search.execute(
                cte + "SELECT count(*) " + selection, bindings
            ).fetchone()[0]
            measured["match_filter_count"] = perf_counter() - start
            start = perf_counter()
            selected = search.execute(
                cte
                + "SELECT j.* "
                + selection
                + f" ORDER BY {_SORTS[criteria.sort]}, j.id LIMIT ? OFFSET ?",
                (*bindings, page_size, (page - 1) * page_size),
            ).fetchall()
            measured["sort_page"] = perf_counter() - start
            start = perf_counter()
            jobs = []
            for summary in selected:
                job = dict(summary)
                job["representations"] = []
                for rep in search.execute(
                    "SELECT * FROM postings WHERE logical_job_id=? ORDER BY source, source_scope, source_job_id",
                    (summary["id"],),
                ):
                    row = catalog.execute(
                        f"""
                        SELECT n.*, l.status, l.first_seen_at, l.last_seen_at
                        FROM normalized_jobs n JOIN source_job_lifecycle l USING ({KEYS})
                        WHERE n.source=? AND n.source_scope=? AND n.source_job_id=?
                    """,
                        (rep["source"], rep["source_scope"], rep["source_job_id"]),
                    ).fetchone()
                    if row is None:
                        raise SearchIndexError(
                            f"Catalog details changed; synchronize the index. {REPAIR}"
                        )
                    detail = dict(row)
                    detail.update(
                        outcome=rep["outcome"],
                        reasons=rep["reasons"],
                        filter_outcome=rep["filter_outcome"],
                        screening_outcome=rep["screening_outcome"],
                        evidence=rep["evidence"],
                    )
                    job["representations"].append(detail)
                jobs.append(job)
            measured["details"] = perf_counter() - start
            if catalog_stamp(database) != meta["catalog_stamp"]:
                raise SearchIndexError(f"Catalog changed during search. {REPAIR}")
    except (sqlite3.Error, OSError) as error:
        raise SearchIndexError(
            f"Search index unavailable or corrupt: {error}. {REPAIR}"
        ) from error
    measured["total"] = perf_counter() - started
    if timings is not None:
        timings.update(measured)
    return SearchPage(total, tuple(jobs))


def lookup_time(index, criteria):
    """Benchmark FTS/text candidate selection separately, without detail retrieval."""
    where, parameters = _predicates(criteria)
    with closing(readonly(index)) as connection:
        start = perf_counter()
        count = connection.execute(
            f"SELECT count(*) FROM postings n WHERE {where}", parameters
        ).fetchone()[0]
        return count, perf_counter() - start
