"""Search correctness against a small real-schema catalog."""

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from job_matcher.catalog_search import (
    SearchCriteria,
    parse_keywords,
    safe_url,
    search_catalog,
)
from job_matcher.filter_policy import TextFilterRules, load_filter_policy
from job_matcher.sqlite_repository import SQLiteJobRepository

NOW = "2026-11-01T00:00:00+00:00"


@pytest.fixture
def catalog(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    SQLiteJobRepository(db).initialize_schema()
    policy_file = tmp_path / "policy.toml"
    # Use the documented full policy format, overriding just the exclusion.
    text = Path("examples/filter_policy.example.toml").read_text(encoding="utf-8")
    policy_file.write_text(text, encoding="utf-8")
    policy = load_filter_policy(policy_file)
    policy = replace(
        policy,
        title=TextFilterRules(exclude_any=("Blocked",)),
        location=TextFilterRules(),
        description=TextFilterRules(),
    )

    def add(
        number,
        *,
        logical=None,
        source="lever",
        title="Engineer",
        description="Build Python automation and web scraping systems",
        location="Austin, TX",
        active=True,
        raw='{"country":"US"}',
    ):
        logical = str(UUID(int=logical or number))
        key = (source, "test", str(number))
        with sqlite3.connect(db) as c:
            c.execute(
                "INSERT INTO raw_source_jobs VALUES (?, ?, ?, ?, ?)", (*key, NOW, raw)
            )
            c.execute(
                "INSERT INTO normalized_jobs (source,source_scope,source_job_id,company,title,description,location,job_url,apply_url,posted_at,retrieved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *key,
                    "Synthetic",
                    title,
                    description,
                    location,
                    f"https://example.com/{number}",
                    None,
                    None,
                    NOW,
                ),
            )
            c.execute(
                "INSERT INTO source_job_lifecycle VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    *key,
                    "active" if active else "inactive",
                    NOW,
                    NOW,
                    None if active else NOW,
                ),
            )
            c.execute("INSERT OR IGNORE INTO logical_jobs VALUES (?)", (logical,))
            c.execute(
                "INSERT INTO source_job_links VALUES (?, ?, ?, ?, ?)",
                (*key, logical, NOW),
            )

    return db, policy, add


def run(catalog, **kwargs):
    db, policy, _ = catalog
    return search_catalog(db, policy, SearchCriteria(**kwargs))


def test_rebuilt_index_uses_H_evidence_version_and_source_local_aggregation(
    catalog, tmp_path
):
    from job_matcher.search_index import (
        implementation_signature,
        readonly,
        synchronize,
        validate_index,
    )

    db, policy, add = catalog
    add(1, location="Bengaluru, India", raw='{"fixture":true}', source="greenhouse")
    add(2, location="Remote", raw='{"fixture":true}', source="greenhouse")
    add(3, title="Blocked Engineer")
    add(4, logical=1)
    index = tmp_path / "search.sqlite3"
    stats = synchronize(db, index, policy, rebuild=True, progress=lambda _: None)
    assert stats.screened == stats.indexed == 4
    with readonly(index) as connection:
        meta = validate_index(connection, db, policy)
        assert meta["implementation_signature"] == implementation_signature()
        rows = {
            r["source_job_id"]: r for r in connection.execute("SELECT * FROM postings")
        }
        assert {r["screening_version"] for r in rows.values()} == {"5"}
        assert rows["1"]["outcome"] == "exclude"
        assert rows["2"]["outcome"] == "keep"
        assert rows["3"]["outcome"] == "exclude"
        assert '"geo_h.action","value":"review_only"' in rows["2"]["evidence"]
        assert (
            connection.execute(
                "SELECT outcome FROM logical_jobs WHERE id=?", (str(UUID(int=1)),)
            ).fetchone()[0]
            == "keep"
        )


def test_keywords(catalog):
    _, _, add = catalog
    add(1)
    add(2, description="Playwright scraper")
    assert run(catalog, keywords="pYtHoN").total == 1
    assert run(catalog, keywords="Python AND automation").total == 1
    assert run(catalog, keywords="Python Playwright", mode="AND").total == 0
    assert run(catalog, keywords="scraping OR scraper").total == 2
    assert run(catalog, keywords='"web scraping"').total == 1
    assert run(catalog, keywords='"automation web"').total == 0
    assert run(catalog, keywords="Python", fields="title").total == 0
    assert run(catalog, keywords="Synthetic", fields="all").total == 2
    assert run(catalog, keywords="missing").jobs == ()


def test_filters_missing_and_inactive(catalog):
    _, _, add = catalog
    add(1)
    add(2, source="greenhouse", description="", location=None)
    add(3, active=False)
    add(4, title="Blocked Engineer")
    assert run(catalog).total == 1
    assert run(catalog, outcome="defer").total == 1
    assert run(catalog, outcome="exclude").total == 1
    assert run(catalog, source="greenhouse", outcome="all").total == 1
    assert run(catalog, outcome="all", include_inactive=True).total == 4
    assert run(catalog, location="AUSTIN", company="synthe", outcome="all").total == 2
    assert run(catalog, discovered_since="2026-11-02").total == 0
    assert run(catalog, keywords="%", outcome="all").total == 0
    assert run(catalog, keywords="' OR 1=1 --", outcome="all").total == 0


def test_aggregation_and_no_combined_evidence(catalog):
    _, _, add = catalog
    add(1, title="Blocked Engineer")  # screen KEEP but filter rejected
    add(
        2, logical=1, raw='{"country":"GB","workplaceType":"onsite"}', location="London"
    )
    assert run(catalog).total == 0
    assert run(catalog, outcome="exclude").total == 1
    add(3, logical=1, active=False)  # inactive KEEP cannot rescue it
    assert run(catalog, include_inactive=True).total == 0
    add(4, logical=1, source="greenhouse", description="Build Playwright systems")
    result = run(catalog, keywords="Python", source="lever")
    assert result.total == 1
    assert len(result.jobs[0]["representations"]) == 4
    assert result.jobs[0]["outcome"] == "keep"
    # Terms cannot be spread across representations.
    assert run(catalog, keywords="Python Playwright").total == 0


@pytest.mark.parametrize("sort", ["first_seen", "last_seen", "company", "title"])
def test_pagination_read_only(catalog, sort):
    db, policy, add = catalog
    for number in range(1, 29):
        add(number)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    criteria = SearchCriteria(sort=sort)
    first = search_catalog(db, policy, criteria)
    second = search_catalog(db, policy, criteria, page=2)
    assert first.total == second.total == 28
    assert len(first.jobs) == 25 and len(second.jobs) == 3
    assert not {j["id"] for j in first.jobs} & {j["id"] for j in second.jobs}
    assert first == search_catalog(db, policy, criteria)
    assert len(search_catalog(db, policy, criteria, page_size=50).jobs) == 28
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_parser_and_urls():
    assert parse_keywords('"AND"', "OR") == (("AND",), "OR")
    for value in ['"unfinished', "a AND b OR c"]:
        with pytest.raises(ValueError):
            parse_keywords(value, "AND")
    for value in [
        None,
        "javascript:alert(1)",
        "file:///x",
        "https://u:p@example.com",
        "https://example.com/\nx",
        "https://[",
    ]:
        assert safe_url(value) is None
    assert safe_url("https://example.com/apply") == "https://example.com/apply"


def test_sort_order_and_unlinked_postings(catalog):
    db, policy, add = catalog
    add(1, title="Zebra Engineer")
    add(2, title="Alpha Engineer")
    add(3)
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM source_job_links WHERE source_job_id = '3'")
        connection.execute(
            "UPDATE normalized_jobs SET company='Zed' WHERE source_job_id='2'"
        )
        connection.execute(
            "UPDATE source_job_lifecycle SET first_seen_at='2026-10-01T00:00:00+00:00' WHERE source_job_id='1'"
        )
        connection.execute(
            "UPDATE source_job_lifecycle SET last_seen_at='2026-11-02T00:00:00+00:00' WHERE source_job_id='1'"
        )

    def ordered(sort):
        return [
            j["representations"][0]["source_job_id"]
            for j in search_catalog(db, policy, SearchCriteria(sort=sort)).jobs
        ]

    assert ordered("title") == ["2", "1"]
    assert ordered("company") == ["1", "2"]
    assert ordered("first_seen") == ["2", "1"]
    assert ordered("last_seen") == ["1", "2"]


def test_missing_database_is_not_created(tmp_path):
    policy = load_filter_policy("examples/filter_policy.example.toml")
    path = tmp_path / "absent.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        search_catalog(path, policy, SearchCriteria())
    assert not path.exists()


@pytest.fixture
def indexed(catalog, tmp_path):
    from job_matcher.search_index import synchronize

    database, policy, add = catalog
    index = tmp_path / "search.sqlite3"
    add(1)
    add(2, description="Playwright scraper C++ a_b 100% x.y café")
    add(3, logical=1, source="greenhouse", description="Other representation")
    add(4, active=False)
    add(5, description="", location=None)
    add(6, title="Blocked Engineer")
    synchronize(database, index, policy, rebuild=True, progress=lambda _: None)
    return database, index, policy, add


@pytest.mark.parametrize(
    "criteria",
    [
        {},
        {"keywords": "pYtHoN"},
        {"keywords": "scraping OR scraper"},
        {"keywords": "Python AND automation"},
        {"keywords": '"web scraping"'},
        {"keywords": '"Python Playwright"'},
        {"keywords": "P", "outcome": "all"},
        {"keywords": "py"},
        {"keywords": "C++"},
        {"keywords": "a_b"},
        {"keywords": "100%"},
        {"keywords": "x.y"},
        {"keywords": "café"},
        {"keywords": '"OR 1=1 --"'},
        {"keywords": '"title:Python"'},
        {"keywords": '"*"'},
        {"keywords": '"("'},
        {"keywords": '"AND"'},
        {"keywords": "Synthetic", "fields": "all"},
        {"keywords": "Python", "fields": "title"},
        {"source": "greenhouse"},
        {"outcome": "defer"},
        {"outcome": "exclude"},
        {"outcome": "all", "include_inactive": True},
        {"company": "synthe", "location": "AUSTIN"},
        {"discovered_since": "2026-11-02"},
        {"keywords": "never_seen_before"},
    ],
)
def test_index_matches_legacy(indexed, criteria):
    from job_matcher.indexed_search import search_index

    database, index, policy, _ = indexed
    criteria = SearchCriteria(**criteria)
    original = search_catalog(database, policy, criteria)
    optimized = search_index(database, index, policy, criteria)
    assert original.total == optimized.total
    assert [j["id"] for j in original.jobs] == [j["id"] for j in optimized.jobs]
    for old, new in zip(original.jobs, optimized.jobs):
        assert {k: v for k, v in new.items() if k != "representations"} == {
            k: v for k, v in old.items() if k != "representations"
        }
        assert len(old["representations"]) == len(new["representations"])
        for old_rep, new_rep in zip(old["representations"], new["representations"]):
            assert old_rep == {k: new_rep[k] for k in old_rep}


def test_index_no_processing_during_search_or_unchanged_sync(indexed, monkeypatch):
    import job_matcher.search_index as cache
    from job_matcher.indexed_search import search_index

    database, index, policy, _ = indexed

    def unexpected(*args, **kwargs):
        raise AssertionError("Unchanged jobs must not be rescreened")

    monkeypatch.setattr(cache, "_decisions", unexpected)
    for word in ("Python", "scraper", "arbitrary-new-term"):
        search_index(database, index, policy, SearchCriteria(keywords=word))
    stats = cache.synchronize(database, index, policy, progress=lambda _: None)
    assert stats.scanned == 6
    assert (
        stats.indexed == stats.screened == stats.metadata_updated == stats.deleted == 0
    )


def test_incremental_updates_and_staleness(indexed):
    from job_matcher.indexed_search import search_index
    from job_matcher.search_index import SearchIndexError, synchronize

    database, index, policy, add = indexed
    add(7, title="Novelword engineer")
    with pytest.raises(SearchIndexError, match="stale"):
        search_index(database, index, policy, SearchCriteria())
    stats = synchronize(database, index, policy, progress=lambda _: None)
    assert stats.indexed == stats.screened == 1
    assert (
        search_index(
            database, index, policy, SearchCriteria(keywords="novelword")
        ).total
        == 1
    )
    with sqlite3.connect(database) as c:
        c.execute(
            "UPDATE normalized_jobs SET description='Changedxyz systems' WHERE source_job_id='2'"
        )
        c.execute(
            "UPDATE source_job_lifecycle SET status='inactive', inactive_at=last_seen_at WHERE source_job_id='7'"
        )
        c.execute(
            "UPDATE normalized_jobs SET retrieved_at='2026-11-02T00:00:00+00:00' WHERE source_job_id='1'"
        )
        c.execute(
            "UPDATE raw_source_jobs SET retrieved_at='2026-11-02T00:00:00+00:00' WHERE source_job_id='1'"
        )
    stats = synchronize(database, index, policy, progress=lambda _: None)
    assert (
        stats.indexed == stats.screened == 1
    )  # last_seen/retrieval alone aren't content
    assert (
        search_index(
            database, index, policy, SearchCriteria(keywords="changedxyz")
        ).total
        == 1
    )
    assert (
        search_index(
            database, index, policy, SearchCriteria(keywords="novelword")
        ).total
        == 0
    )
    with sqlite3.connect(database) as c:
        c.execute(
            "UPDATE source_job_lifecycle SET status='active', inactive_at=NULL WHERE source_job_id='7'"
        )
        c.execute(
            'UPDATE raw_source_jobs SET payload_json=\'{"country":"GB","workplaceType":"onsite"}\' WHERE source_job_id=\'2\''
        )
        c.execute(
            "UPDATE normalized_jobs SET location='London' WHERE source_job_id='2'"
        )
    stats = synchronize(database, index, policy, progress=lambda _: None)
    assert stats.screened == 1
    assert (
        search_index(
            database, index, policy, SearchCriteria(keywords="novelword")
        ).total
        == 1
    )
    assert (
        search_index(
            database, index, policy, SearchCriteria(keywords="changedxyz")
        ).total
        == 0
    )


def test_policy_and_implementation_invalidation(indexed, monkeypatch):
    import job_matcher.search_index as cache
    from job_matcher.indexed_search import search_index

    database, index, policy, _ = indexed
    changed = replace(policy, title=TextFilterRules(exclude_any=("Engineer",)))
    with pytest.raises(cache.SearchIndexError, match="policy"):
        search_index(database, index, changed, SearchCriteria())
    stats = cache.synchronize(database, index, changed, progress=lambda _: None)
    assert stats.screened == 6 and stats.indexed == 0
    assert search_index(database, index, changed, SearchCriteria()).total == 0
    monkeypatch.setattr(cache, "implementation_signature", lambda: "new rules")
    with pytest.raises(cache.SearchIndexError, match="implementation"):
        search_index(database, index, changed, SearchCriteria())
    stats = cache.synchronize(database, index, changed, progress=lambda _: None)
    assert stats.screened == 6 and stats.indexed == 0


def test_identity_and_deletion(indexed):
    from job_matcher.indexed_search import search_index
    from job_matcher.search_index import synchronize

    database, index, policy, _ = indexed
    with sqlite3.connect(database) as c:
        c.execute(
            "UPDATE source_job_links SET logical_job_id=? WHERE source_job_id='2'",
            (str(UUID(int=1)),),
        )
        c.execute("DELETE FROM normalized_jobs WHERE source_job_id='6'")
        c.execute("DELETE FROM source_job_links WHERE source_job_id='5'")
    stats = synchronize(database, index, policy, progress=lambda _: None)
    assert stats.deleted == 2 and stats.screened == 1 and stats.indexed == 0
    result = search_index(database, index, policy, SearchCriteria(keywords="scraper"))
    assert result.total == 1 and len(result.jobs[0]["representations"]) == 3
    assert result.jobs[0]["id"] == str(UUID(int=1))


def test_interruption_and_rebuild_publication(indexed):
    from job_matcher.indexed_search import search_index
    from job_matcher.search_index import SearchIndexError, synchronize

    database, index, policy, add = indexed
    before = index.read_bytes()
    add(7)

    def interrupt(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        synchronize(database, index, policy, progress=interrupt, batch_size=1)
    assert index.read_bytes() == before
    with pytest.raises(SearchIndexError, match="stale"):
        search_index(database, index, policy, SearchCriteria())
    with pytest.raises(KeyboardInterrupt):
        synchronize(
            database, index, policy, rebuild=True, progress=interrupt, batch_size=1
        )
    assert index.read_bytes() == before
    assert not list(index.parent.glob("*.building.sqlite3"))
    stats = synchronize(database, index, policy, progress=lambda _: None)
    assert stats.screened == 1
    assert synchronize(database, index, policy, progress=lambda _: None).screened == 0


def test_index_missing_incomplete_corrupt_and_source_safety(indexed, tmp_path):
    from job_matcher.indexed_search import search_index
    from job_matcher.search_index import SearchIndexError, synchronize

    database, index, policy, _ = indexed
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    for path in [tmp_path / "missing.sqlite3", tmp_path / "corrupt.sqlite3"]:
        if "corrupt" in path.name:
            path.write_bytes(b"not sqlite")
        with pytest.raises(SearchIndexError):
            search_index(database, path, policy, SearchCriteria())
    with sqlite3.connect(index) as c:
        c.execute("UPDATE meta SET value='no' WHERE key='complete'")
    c.close()
    with pytest.raises(SearchIndexError, match="Incomplete"):
        search_index(database, index, policy, SearchCriteria())
    with pytest.raises(SearchIndexError, match="different files"):
        synchronize(database, database, policy, rebuild=True)
    synchronize(database, index, policy, rebuild=True, progress=lambda _: None)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_index_pagination_and_sorting(catalog, tmp_path):
    from job_matcher.indexed_search import search_index
    from job_matcher.search_index import synchronize

    database, policy, add = catalog
    index = tmp_path / "search.sqlite3"
    for i in range(1, 60):
        add(i, title=f"Engineer {60 - i:02}")
    synchronize(database, index, policy, rebuild=True, progress=lambda _: None)
    for sort in (
        "first_seen",
        "last_seen",
        "company",
        "title",
        "posted_newest",
        "posted_oldest",
    ):
        criteria = SearchCriteria(sort=sort)
        first = search_index(database, index, policy, criteria)
        second = search_index(database, index, policy, criteria, page=2)
        assert first.total == 59 and len(first.jobs) == len(second.jobs) == 25
        assert not {j["id"] for j in first.jobs} & {j["id"] for j in second.jobs}
        assert [j["id"] for j in first.jobs] == [
            j["id"] for j in search_catalog(database, policy, criteria).jobs
        ]


def test_cached_eligibility_never_combines_representations(catalog, tmp_path):
    from job_matcher.indexed_search import search_index
    from job_matcher.search_index import synchronize

    database, policy, add = catalog
    index = tmp_path / "search.sqlite3"
    add(1, title="Blocked Engineer")
    add(
        2, logical=1, location="London", raw='{"country":"GB","workplaceType":"onsite"}'
    )
    add(3, logical=1, active=False)
    synchronize(database, index, policy, rebuild=True, progress=lambda _: None)
    assert search_index(database, index, policy, SearchCriteria()).total == 0
    result = search_index(database, index, policy, SearchCriteria(outcome="exclude"))
    assert result.total == 1 and len(result.jobs[0]["representations"]) == 3
    with sqlite3.connect(index) as c:
        row = c.execute(
            "SELECT filter_outcome, screening_outcome, outcome, policy_fingerprint, screening_version, evidence FROM postings WHERE source_job_id='1'"
        ).fetchone()
        assert row[:3] == ("rejected", "keep", "exclude")
        assert row[3] == policy.fingerprint
        assert row[4] and row[5]
    c.close()
    add(
        4,
        logical=1,
        source="greenhouse",
        description="A different active representation",
    )
    synchronize(database, index, policy, progress=lambda _: None)
    result = search_index(
        database, index, policy, SearchCriteria(keywords="Python", source="lever")
    )
    assert result.total == 1 and len(result.jobs[0]["representations"]) == 4
    assert (
        search_index(
            database, index, policy, SearchCriteria(keywords="Python different")
        ).total
        == 0
    )


def test_source_changes_during_sync_are_not_published(indexed):
    from job_matcher.indexed_search import search_index
    from job_matcher.search_index import SearchIndexError, synchronize

    database, index, policy, _ = indexed
    before = index.read_bytes()
    changed = False

    def concurrent_change(_):
        nonlocal changed
        if not changed:
            # Rollback-journal readers block writers; WAL allows the concurrent commit.
            with sqlite3.connect(database) as c:
                c.execute(
                    "UPDATE normalized_jobs SET description='Concurrent change' WHERE source_job_id='2'"
                )
            c.close()
            changed = True

    with sqlite3.connect(database) as c:
        c.execute("PRAGMA journal_mode=WAL")
    c.close()
    with pytest.raises(SearchIndexError, match="changed during"):
        synchronize(database, index, policy, progress=concurrent_change, batch_size=1)
    assert index.read_bytes() == before
    with pytest.raises(SearchIndexError, match="stale"):
        search_index(database, index, policy, SearchCriteria())
    assert synchronize(database, index, policy, progress=lambda _: None).screened == 1


def test_raw_evidence_only_change_and_failed_first_build(indexed, tmp_path):
    from job_matcher.search_index import synchronize

    database, index, policy, _ = indexed
    with sqlite3.connect(database) as c:
        c.execute(
            'UPDATE raw_source_jobs SET payload_json=\'{"country":"GB","workplaceType":"hybrid"}\' WHERE source_job_id=\'2\''
        )
    c.close()
    stats = synchronize(database, index, policy, progress=lambda _: None)
    assert stats.screened == 1 and stats.indexed == 0
    with sqlite3.connect(index) as c:
        assert (
            '"geo_h.raw.country","value":"GB"'
            in c.execute(
                "SELECT evidence FROM postings WHERE source_job_id='2'"
            ).fetchone()[0]
        )
    c.close()
    missing = tmp_path / "never_published.sqlite3"

    def interrupt(_):
        raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError, match="injected"):
        synchronize(
            database, missing, policy, rebuild=True, progress=interrupt, batch_size=1
        )
    assert not missing.exists()
    assert not list(tmp_path.glob("*.building.sqlite3"))


def test_metadata_backfill_search_and_screening_stability(catalog, tmp_path):
    from job_matcher.indexed_search import search_index
    from job_matcher.metadata_backfill import backfill
    from job_matcher.search_index import readonly, synchronize

    db, policy, add = catalog
    add(
        1,
        raw='{"categories":{"commitment":"Full-time"},"workplaceType":"remote","createdAt":1767225600000}',
    )
    add(
        2,
        title="Blocked Engineer",
        raw='{"workplaceType":"onsite","createdAt":1750000000000}',
    )
    add(3, description="", active=False)
    add(4, description="")
    add(
        5,
        logical=1,
        source="greenhouse",
        raw='{"metadata":[{"name":"Workplace Type","value":"Hybrid"}]}',
    )
    index = tmp_path / "search.sqlite3"
    synchronize(db, index, policy, rebuild=True, progress=lambda _: None)
    with readonly(index) as c:
        before = [
            tuple(r)
            for r in c.execute(
                "SELECT source,source_job_id,outcome,evaluated_at FROM postings ORDER BY source,source_job_id"
            )
        ]
    with sqlite3.connect(db) as c:
        preserved = {
            table: c.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "raw_source_jobs",
                "source_job_links",
                "source_job_lifecycle",
                "source_scope_state",
            )
        }
        original = c.execute(
            "SELECT source,source_job_id,description,job_url,retrieved_at FROM normalized_jobs"
        ).fetchall()
    counts = backfill(db)
    assert counts["lever"]["posted_at_enriched"] == 2
    assert not any(k.endswith("_enriched") for v in backfill(db).values() for k in v)
    with sqlite3.connect(db) as c:
        assert (
            original
            == c.execute(
                "SELECT source,source_job_id,description,job_url,retrieved_at FROM normalized_jobs"
            ).fetchall()
        )
        for table, rows in preserved.items():
            assert rows == c.execute(f"SELECT * FROM {table}").fetchall()
    stats = synchronize(db, index, policy, progress=lambda _: None)
    assert stats.screened == stats.indexed == 0
    with readonly(index) as c:
        assert before == [
            tuple(r)
            for r in c.execute(
                "SELECT source,source_job_id,outcome,evaluated_at FROM postings ORDER BY source,source_job_id"
            )
        ]

    def query(**kwargs):
        return search_index(db, index, policy, SearchCriteria(outcome="all", **kwargs))

    newest = query(sort="posted_newest").jobs
    assert [j["id"] for j in newest[:2]] == [str(UUID(int=1)), str(UUID(int=2))]
    assert newest[-1]["posted_at"] is None
    assert query(sort="posted_oldest").jobs[0]["id"] == str(UUID(int=2))
    assert query(employment_type="full_time", workplace_type="remote").total == 1
    assert query(employment_type="full_time", workplace_type="hybrid").total == 0
    assert query(workplace_type="hybrid").total == 1


def test_v2_metadata_migration_preserves_rows(catalog):
    db, _, add = catalog
    add(1, active=False)
    with sqlite3.connect(db) as c:
        c.execute("ALTER TABLE normalized_jobs DROP COLUMN employment_type")
        c.execute("ALTER TABLE normalized_jobs DROP COLUMN workplace_type")
        c.execute("PRAGMA user_version=2")
        before = c.execute("SELECT * FROM normalized_jobs").fetchall()
    repo = SQLiteJobRepository(db)
    repo.initialize_schema()
    repo.initialize_schema()
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 3
        assert c.execute("SELECT * FROM normalized_jobs").fetchall() == [
            (*row, None, None) for row in before
        ]
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("screened", ["keep", "defer", "exclude"])
def test_combined_decision_parity_across_consumers(
    catalog, tmp_path, blocked, screened
):
    from types import SimpleNamespace

    from job_matcher.filtering import DeterministicFilterEvaluator
    from job_matcher.indexed_search import search_index
    from job_matcher.screening import combine_outcomes
    from job_matcher.screening_pipeline import occupational_summary, screen_catalog
    from job_matcher.screening_sqlite import read_active_screening_inputs
    from job_matcher.search_index import readonly, synchronize
    from job_matcher.validation_reporting import catalog_report

    database, policy, add = catalog
    descriptions = {
        "keep": "Build Python automation systems.",
        "defer": "",
        "exclude": "This job is part of a testing environment. Please do not apply.",
    }
    add(
        1,
        title="Blocked Engineer" if blocked else "Engineer",
        description=descriptions[screened],
    )
    expected = "exclude" if blocked else screened
    index = tmp_path / "index.sqlite3"
    synchronize(database, index, policy, rebuild=True, progress=lambda _: None)
    pipeline = screen_catalog(
        read_active_screening_inputs(database), DeterministicFilterEvaluator(policy)
    )
    assert pipeline.screening_decisions[0].outcome.value == screened
    assert {outcome.value for outcome in pipeline.logical_outcomes.values()} == {
        expected
    }
    assert (
        combine_outcomes(
            pipeline.filter_decisions[0].outcome,
            pipeline.screening_decisions[0].outcome,
        ).value
        == expected
    )
    criteria = SearchCriteria(outcome="all")
    direct = search_catalog(database, policy, criteria)
    indexed_page = search_index(database, index, policy, criteria)
    assert direct.jobs[0]["outcome"] == indexed_page.jobs[0]["outcome"] == expected
    assert direct.jobs[0]["representations"][0]["description"] == descriptions[screened]
    assert (
        direct.jobs[0]["representations"][0]["reasons"]
        == indexed_page.jobs[0]["representations"][0]["reasons"]
    )
    with readonly(index) as connection:
        row = connection.execute("SELECT * FROM postings").fetchone()
        assert row["outcome"] == expected
        assert row["screening_outcome"] == screened
        assert json.loads(row["screening_reasons"]) == list(
            pipeline.screening_decisions[0].reasons
        )
        assert json.loads(row["evidence"]) == [
            {"field": e.field, "value": e.value}
            for e in pipeline.screening_decisions[0].evidence
        ]
    repository = SQLiteJobRepository(database)
    report = catalog_report(
        repository,
        SimpleNamespace(
            active_linked_jobs=repository.list_active_linked_jobs(),
            identity_resolutions=(),
        ),
        pipeline,
        source="lever",
    )
    assert report["global"]["logical_outcomes"] == {expected: 1}
    assert report["by_source"]["lever"]["independent_logical_outcomes"] == {expected: 1}
    assert report["by_source"]["lever"][
        "screening_representations_among_filter_eligible"
    ] == ({} if blocked else {screened: 1})
    assert sum(
        occupational_summary(pipeline)[
            "baseline_outcomes_among_filter_eligible"
        ].values()
    ) == (0 if blocked else 1)


@pytest.mark.parametrize("active_outcome", ["keep", "defer", "exclude", None])
def test_inactive_keep_never_overrides_active_results(
    catalog, tmp_path, active_outcome
):
    from job_matcher.filtering import DeterministicFilterEvaluator
    from job_matcher.indexed_search import search_index
    from job_matcher.screening_pipeline import screen_catalog
    from job_matcher.screening_sqlite import read_active_screening_inputs
    from job_matcher.search_index import synchronize

    database, policy, add = catalog
    add(1, active=False, description="Historical Python keeper")
    if active_outcome:
        add(
            2,
            logical=1,
            source="greenhouse",
            title="Blocked Engineer" if active_outcome == "exclude" else "Engineer",
            description="" if active_outcome == "defer" else "Current Python duties",
        )
    index = tmp_path / "index.sqlite3"
    synchronize(database, index, policy, rebuild=True, progress=lambda _: None)
    pipeline = screen_catalog(
        read_active_screening_inputs(database), DeterministicFilterEvaluator(policy)
    )
    expected = active_outcome or "exclude"
    assert list(pipeline.logical_outcomes.values()) == (
        [expected] if active_outcome else []
    )
    criteria = SearchCriteria(outcome="all", include_inactive=True)
    direct = search_catalog(database, policy, criteria)
    cached = search_index(database, index, policy, criteria)
    assert direct.jobs[0]["outcome"] == cached.jobs[0]["outcome"] == expected
    assert direct.jobs[0]["title"] == cached.jobs[0]["title"]
    assert len(direct.jobs[0]["representations"]) == (2 if active_outcome else 1)


def test_shared_helpers_are_in_signature_and_rescreen_atomically(
    indexed, tmp_path, monkeypatch
):
    import job_matcher.search_index as cache
    from job_matcher.indexed_search import search_index

    database, index, policy, _ = indexed
    baseline = cache.implementation_signature()
    module_root = Path(cache.__file__).parent
    signature_root = tmp_path / "signature-code"
    signature_root.mkdir()
    for path in module_root.glob("*.py"):
        (signature_root / path.name).write_bytes(path.read_bytes())
    # Redirect signature byte reads, leaving installed files untouched.
    monkeypatch.setattr(cache, "__file__", str(signature_root / "search_index.py"))
    assert cache.implementation_signature() == baseline
    with (signature_root / "screening.py").open("a", encoding="utf-8") as stream:
        stream.write("\n# Simulated shared decision helper revision\n")
    assert cache.implementation_signature() != baseline
    with pytest.raises(cache.SearchIndexError, match="implementation"):
        search_index(database, index, policy, SearchCriteria())
    original = index.read_bytes()

    def interrupted(_):
        raise RuntimeError("injected interruption")

    with pytest.raises(RuntimeError, match="injected"):
        cache.synchronize(database, index, policy, progress=interrupted, batch_size=1)
    assert index.read_bytes() == original
    stats = cache.synchronize(database, index, policy, progress=lambda _: None)
    assert stats.screened == 6 and stats.indexed == 0
    assert (
        cache.synchronize(database, index, policy, progress=lambda _: None).screened
        == 0
    )
    assert search_index(database, index, policy, SearchCriteria()).total > 0


@pytest.mark.parametrize("bad_outcome", ["unknown", "KEEP", ""])
def test_sql_aggregation_retains_historical_unknown_fallback(indexed, bad_outcome):
    from contextlib import closing

    from job_matcher.search_index import _aggregate

    _, index, _, _ = indexed
    with closing(sqlite3.connect(index)) as connection, connection:
        # Existing SQL falls back to EXCLUDE; Python's typed aggregation rejects
        # unknown outcomes. Preserve this intentional storage-projection boundary.
        connection.execute("UPDATE postings SET outcome=?", (bad_outcome,))
        _aggregate(connection)
        assert set(connection.execute("SELECT outcome FROM logical_jobs")) == {
            ("exclude",)
        }
