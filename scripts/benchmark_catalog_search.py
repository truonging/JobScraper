"""Measure search read-only; --build explicitly authorizes index writes."""

import argparse
import ctypes
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from time import perf_counter

from job_matcher.catalog_search import SearchCriteria, search_catalog
from job_matcher.filter_policy import load_filter_policy
from job_matcher.indexed_search import lookup_time, search_index
from job_matcher.search_index import readonly, synchronize

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def peak_memory():
    if os.name != "nt":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            *[
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                )
            ],
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    function = ctypes.windll.psapi.GetProcessMemoryInfo
    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if function(ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb):
        return counters.PeakWorkingSetSize
    return None


def changed_sync_benchmark(database, policy):
    # Preserve source schema/metadata, replacing only raw payloads/descriptions.
    with tempfile.TemporaryDirectory(prefix="job-search-benchmark-") as directory:
        catalog = Path(directory) / "synthetic.sqlite3"
        index = Path(directory) / "search.sqlite3"
        with (
            closing(readonly(database)) as source,
            closing(sqlite3.connect(catalog)) as copy,
        ):
            source.execute("BEGIN")
            version = source.execute("PRAGMA user_version").fetchone()[0]
            if version not in (2, 3):
                raise ValueError("Synthetic benchmark requires catalog schema v2 or v3")
            for name in (
                "raw_source_jobs",
                "normalized_jobs",
                "source_scope_state",
                "source_job_lifecycle",
                "logical_jobs",
                "source_job_links",
            ):
                ddl = source.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (name,),
                ).fetchone()[0]
                copy.execute(ddl)
            rows = source.execute("SELECT * FROM normalized_jobs")
            columns = [column[0] for column in rows.description]
            insert_normalized = (
                "INSERT INTO normalized_jobs ("
                + ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
                + ") VALUES ("
                + ", ".join("?" for _ in columns)
                + ")"
            )
            while batch := rows.fetchmany(250):
                for row in batch:
                    key = tuple(
                        row[k] for k in ("source", "source_scope", "source_job_id")
                    )
                    copy.execute(
                        "INSERT INTO raw_source_jobs (source, source_scope, source_job_id, retrieved_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                        (*key, row["retrieved_at"], '{"country":"US"}'),
                    )
                    values = dict(row)
                    values["description"] = (
                        "Build Python and scraper automation systems."
                    )
                    copy.execute(insert_normalized, [values[name] for name in columns])
            for name in (
                "source_scope_state",
                "source_job_lifecycle",
                "logical_jobs",
                "source_job_links",
            ):
                cursor = source.execute(f"SELECT * FROM {name}")
                columns = [column[0] for column in cursor.description]
                while batch := cursor.fetchmany(250):
                    copy.executemany(
                        f"INSERT INTO {name} ("
                        + ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
                        + ") VALUES ("
                        + ",".join("?" for _ in batch[0])
                        + ")",
                        [tuple(row) for row in batch],
                    )
            changed_rows = copy.execute("""
                SELECT n.rowid FROM normalized_jobs n
                JOIN source_job_links s USING (source, source_scope, source_job_id)
                ORDER BY n.source, n.source_scope, n.source_job_id LIMIT 5
            """).fetchall()
            if len(changed_rows) != 5:
                raise ValueError(
                    "Synthetic benchmark requires at least five linked postings"
                )
            copy.execute(f"PRAGMA user_version={version}")
            copy.commit()
        synchronize(catalog, index, policy, rebuild=True, progress=lambda _: None)
        with closing(sqlite3.connect(catalog)) as copy:
            copy.executemany(
                "UPDATE normalized_jobs SET description='Changed novel scraper duties.' WHERE rowid=?",
                changed_rows,
            )
            copy.commit()
        start = perf_counter()
        stats = synchronize(catalog, index, policy, progress=lambda _: None)
        return {"seconds": perf_counter() - start, "stats": asdict(stats)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build",
        action="store_true",
        help="Rebuild the explicit --index and time a no-op sync; never write the catalog",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Slow: compare first pages/counts with legacy search",
    )
    parser.add_argument(
        "--exercise-sync",
        action="store_true",
        help="Time five changed records using a disposable synthetic catalog",
    )
    parser.add_argument(
        "--catalog", type=Path, default=ROOT / "phase2_validation.sqlite3"
    )
    parser.add_argument(
        "--index", type=Path, help="Search database (required with --build)"
    )
    parser.add_argument(
        "--policy", type=Path, default=ROOT / "examples/filter_policy.example.toml"
    )
    args = parser.parse_args(argv)
    if args.build and args.index is None:
        parser.error(
            "--build requires an explicit --index outside the production databases"
        )
    args.index = args.index or ROOT / "job_search.sqlite3"
    if args.build:
        protected = (
            args.catalog,
            ROOT / "phase2_validation.sqlite3",
            ROOT / "job_search.sqlite3",
        )
        for path in protected:
            if args.index.resolve() == path.resolve() or (
                args.index.exists()
                and path.exists()
                and os.path.samefile(args.index, path)
            ):
                parser.error(
                    "--build index must differ from the catalog and production databases"
                )
    try:
        _run_benchmark(args)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"Benchmark failed: {error}\n")


def _run_benchmark(args):
    policy = load_filter_policy(args.policy)
    before = digest(args.catalog)
    print("SHA256 before:", before, flush=True)
    report = {}

    def progress(message):
        count = int(message.split(";")[0].split()[1].replace(",", ""))
        if count % 5000 == 0:
            print(message, flush=True)

    if args.build:
        start = perf_counter()
        stats = synchronize(
            args.catalog, args.index, policy, rebuild=True, progress=progress
        )
        report["build_seconds"] = perf_counter() - start
        report["build_stats"] = asdict(stats)
        print("Build:", json.dumps(report), flush=True)
    report["index_bytes"] = args.index.stat().st_size
    report["queries"] = {}
    for term in (
        "scraping",
        "scraper",
        "Python",
        "Playwright",
        "automation",
        "zzNeverSearchedBefore",
    ):
        criteria = SearchCriteria(keywords=term)
        runs = []
        first = None
        for run in range(2):
            timings = {}
            result = search_index(
                args.catalog, args.index, policy, criteria, timings=timings
            )
            postings, seconds = lookup_time(args.index, criteria)
            timings.update(
                lookup_seconds=seconds,
                matching_postings=postings,
                logical_jobs=result.total,
            )
            runs.append(timings)
            first = first or result
        report["queries"][term] = runs
        print("Query", term, json.dumps(runs), flush=True)
        if args.compare and term in {"scraping", "scraper", "Python", "Playwright"}:
            legacy = search_catalog(args.catalog, policy, criteria)
            assert legacy.total == first.total
            assert [j["id"] for j in legacy.jobs] == [j["id"] for j in first.jobs]
            print("Legacy parity OK:", term, flush=True)
    report["navigation"] = {}
    for sort, page in (
        ("title", 1),
        ("company", 1),
        ("first_seen", 2),
        ("last_seen", 1),
    ):
        timings = {}
        search_index(
            args.catalog,
            args.index,
            policy,
            SearchCriteria(keywords="Python", sort=sort),
            page=page,
            timings=timings,
        )
        report["navigation"][f"{sort}/{page}"] = timings
    if args.build:
        start = perf_counter()
        report["noop_sync_stats"] = asdict(
            synchronize(args.catalog, args.index, policy, progress=progress)
        )
        report["noop_sync_seconds"] = perf_counter() - start
    if args.exercise_sync:
        report["five_changes_synthetic"] = changed_sync_benchmark(args.catalog, policy)
    report["peak_process_working_set_bytes"] = peak_memory()
    report["sha256_before"] = before
    report["sha256_after"] = digest(args.catalog)
    assert report["sha256_after"] == before, "Authoritative catalog changed"
    print("RESULT", json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
