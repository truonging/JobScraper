"""Benchmark schema parity and explicit write boundaries on disposable catalogs."""

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_matcher.filter_policy import load_filter_policy
from job_matcher.identity import IdentityResolver
from job_matcher.models import (
    AcquiredJob,
    NormalizedJob,
    RawSourceRecord,
    SourceJobKey,
    SourceSnapshot,
)
from job_matcher.search_index import readonly, synchronize
from job_matcher.sqlite_repository import SQLiteJobRepository
from scripts import benchmark_catalog_search as benchmark

POLICY = Path("examples/filter_policy.example.toml").resolve()
NOW = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def catalog(tmp_path):
    database = tmp_path / "catalog.sqlite3"
    repository = SQLiteJobRepository(database)
    repository.initialize_schema()
    jobs = []
    for number in range(7):
        key = SourceJobKey("lever", "synthetic", str(number))
        jobs.append(
            AcquiredJob(
                NormalizedJob(
                    key=key,
                    company="Synthetic",
                    title=f"Software Engineer {number}",
                    description="Build Python Playwright scraper automation and web scraping.",
                    job_url=f"https://example.com/{number}",
                    retrieved_at=NOW,
                    location="Austin, TX",
                    posted_at=NOW if number % 2 else None,
                    employment_type="full_time" if number % 2 else None,
                    workplace_type="remote" if number % 3 else None,
                ),
                RawSourceRecord(key, NOW, '{"country":"US","fixture":true}'),
            )
        )
    repository.reconcile_snapshot(
        SourceSnapshot("lever", "synthetic", NOW, tuple(jobs))
    )
    IdentityResolver(repository).resolve_unlinked()
    return database


def snapshot(*paths):
    return {
        str(file): benchmark.digest(file) if file.exists() else None
        for path in paths
        for file in (path, *(Path(str(path) + s) for s in ("-wal", "-shm", "-journal")))
    }


def result(capsys):
    return json.loads(capsys.readouterr().out.split("RESULT ", 1)[1])


@pytest.mark.parametrize("version", [2, 3, "3_migrated"])
def test_synthetic_schema_metadata_and_exactly_five_changes(
    catalog, monkeypatch, version
):
    if version != 3:
        with closing(sqlite3.connect(catalog)) as connection, connection:
            connection.execute(
                "ALTER TABLE normalized_jobs DROP COLUMN employment_type"
            )
            connection.execute("ALTER TABLE normalized_jobs DROP COLUMN workplace_type")
            connection.execute("PRAGMA user_version=2")
        if version == "3_migrated":
            SQLiteJobRepository(catalog).initialize_schema()
    before = snapshot(catalog)
    with closing(readonly(catalog)) as connection:
        originals = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM normalized_jobs ORDER BY source_job_id"
            )
        ]
    calls = []
    temporary_paths = []

    def inspect_sync(database, index, policy, **kwargs):
        assert Path(database) != catalog
        temporary_paths.extend([Path(database), Path(index)])
        with closing(readonly(database)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == (
                2 if version == 2 else 3
            )
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM normalized_jobs ORDER BY source_job_id"
                )
            ]
        for original, copied in zip(originals, rows):
            assert {k: v for k, v in original.items() if k != "description"} == {
                k: v for k, v in copied.items() if k != "description"
            }
        calls.append(rows)
        return synchronize(database, index, policy, **kwargs)

    monkeypatch.setattr(benchmark, "synchronize", inspect_sync)
    measured = benchmark.changed_sync_benchmark(catalog, load_filter_policy(POLICY))
    assert measured["stats"] == dict(
        scanned=7, indexed=5, screened=5, metadata_updated=5, deleted=0
    )
    assert measured["seconds"] > 0
    assert len(calls) == 2
    assert sum(a != b for a, b in zip(*calls)) == 5
    assert snapshot(catalog) == before
    assert all(not path.exists() for path in temporary_paths)


@pytest.mark.parametrize("compare", [False, True])
@pytest.mark.parametrize("version", [2, 3])
def test_read_only_cli_never_synchronizes(
    catalog, tmp_path, monkeypatch, capsys, compare, version
):
    if version == 2:
        with closing(sqlite3.connect(catalog)) as connection, connection:
            connection.execute(
                "ALTER TABLE normalized_jobs DROP COLUMN employment_type"
            )
            connection.execute("ALTER TABLE normalized_jobs DROP COLUMN workplace_type")
            connection.execute("PRAGMA user_version=2")
    index = tmp_path / "index.sqlite3"
    synchronize(catalog, index, load_filter_policy(POLICY), rebuild=True)
    before = snapshot(catalog, index)

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only benchmark invoked synchronization")

    monkeypatch.setattr(benchmark, "synchronize", forbidden)
    args = ["--catalog", str(catalog), "--index", str(index), "--policy", str(POLICY)]
    benchmark.main(args + (["--compare"] if compare else []))
    measured = result(capsys)
    assert len(measured["queries"]) == 6 and len(measured["navigation"]) == 4
    assert "noop_sync_stats" not in measured and "build_stats" not in measured
    assert measured["queries"]["Python"][0]["logical_jobs"] == 7
    assert snapshot(catalog, index) == before


def test_stale_comparison_fails_without_writes(catalog, tmp_path, monkeypatch, capsys):
    index = tmp_path / "index.sqlite3"
    synchronize(catalog, index, load_filter_policy(POLICY), rebuild=True)
    with closing(sqlite3.connect(catalog)) as connection, connection:
        connection.execute(
            "UPDATE normalized_jobs SET description='Changed Python duties'"
        )
    before = snapshot(catalog, index)
    monkeypatch.setattr(benchmark, "synchronize", lambda *a, **k: pytest.fail("sync"))
    with pytest.raises(SystemExit) as error:
        benchmark.main(["--catalog", str(catalog), "--index", str(index), "--compare"])
    assert error.value.code == 1
    assert "stale" in capsys.readouterr().err
    assert snapshot(catalog, index) == before


def test_explicit_build_and_synthetic_cli(catalog, tmp_path, monkeypatch, capsys):
    index = tmp_path / "new-index.sqlite3"
    before = snapshot(catalog)
    calls = []

    def tracked(database, target, policy, **kwargs):
        calls.append((Path(database), Path(target), kwargs.get("rebuild", False)))
        return synchronize(database, target, policy, **kwargs)

    monkeypatch.setattr(benchmark, "synchronize", tracked)
    benchmark.main(
        [
            "--catalog",
            str(catalog),
            "--index",
            str(index),
            "--policy",
            str(POLICY),
            "--build",
            "--compare",
            "--exercise-sync",
        ]
    )
    measured = result(capsys)
    assert measured["build_stats"]["indexed"] == 7
    assert measured["noop_sync_stats"]["indexed"] == 0
    assert measured["noop_sync_stats"]["screened"] == 0
    assert measured["five_changes_synthetic"]["stats"]["screened"] == 5
    assert calls[:2] == [(catalog, index, True), (catalog, index, False)]
    assert len(calls) == 4
    assert all(source != catalog and target != index for source, target, _ in calls[2:])
    assert snapshot(catalog) == before


def test_exercise_sync_without_build_only_writes_temporary_files(
    catalog, tmp_path, monkeypatch, capsys
):
    index = tmp_path / "index.sqlite3"
    synchronize(catalog, index, load_filter_policy(POLICY), rebuild=True)
    before = snapshot(catalog, index)

    def isolated(database, target, policy, **kwargs):
        assert Path(database) != catalog and Path(target) != index
        return synchronize(database, target, policy, **kwargs)

    monkeypatch.setattr(benchmark, "synchronize", isolated)
    benchmark.main(
        ["--catalog", str(catalog), "--index", str(index), "--exercise-sync"]
    )
    assert result(capsys)["five_changes_synthetic"]["stats"]["indexed"] == 5
    assert snapshot(catalog, index) == before


@pytest.mark.parametrize(
    "target", ["omitted", "catalog", "authoritative", "search", "alias"]
)
def test_invalid_build_paths_fail_before_connect(
    catalog, tmp_path, monkeypatch, target
):
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    production = tmp_path / "job_search.sqlite3"
    production.write_bytes(b"protected")
    alias = tmp_path / "alias.sqlite3"
    os.link(production, alias)
    targets = {
        "catalog": catalog,
        "authoritative": tmp_path / "phase2_validation.sqlite3",
        "search": production,
        "alias": alias,
    }
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("connect"))
    monkeypatch.setattr(benchmark, "synchronize", lambda *a, **k: pytest.fail("sync"))
    args = ["--build", "--catalog", str(catalog)]
    if target != "omitted":
        args += ["--index", str(targets[target])]
    with pytest.raises(SystemExit) as error:
        benchmark.main(args)
    assert error.value.code == 2
    assert production.read_bytes() == b"protected"


def test_synthetic_requires_supported_schema_and_five_linked_rows(catalog):
    with closing(sqlite3.connect(catalog)) as connection, connection:
        connection.execute(
            "DELETE FROM source_job_links WHERE source_job_id IN ('0','1','2')"
        )
    before = snapshot(catalog)
    with pytest.raises(ValueError, match="five linked"):
        benchmark.changed_sync_benchmark(catalog, load_filter_policy(POLICY))
    assert snapshot(catalog) == before
    with closing(sqlite3.connect(catalog)) as connection, connection:
        connection.execute("PRAGMA user_version=1")
    before = snapshot(catalog)
    with pytest.raises(ValueError, match="v2 or v3"):
        benchmark.changed_sync_benchmark(catalog, load_filter_policy(POLICY))
    assert snapshot(catalog) == before
