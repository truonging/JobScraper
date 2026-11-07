# Local indexed job catalog dashboard

Structured employment/workplace fields, UTC posting-date display and date sorting
are documented in [job metadata](job_metadata.md). Authoritative schema v3 and
search schema v2 support this enrichment without changing screening version 5.

Run from C:\Users\Truonging\PycharmProjects\JobScraper in PowerShell using the
existing Python 3.12 virtual environment:

~~~powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,ui]"

# First use, incompatible schema, or damaged index:
.\.venv\Scripts\python.exe -m job_matcher.search_index rebuild

# After acquisition, policy edits, or screening implementation changes:
.\.venv\Scripts\python.exe -m job_matcher.search_index sync

# Launch:
.\.venv\Scripts\python.exe -m streamlit run scripts/dashboard.py
~~~

Defaults are phase2_validation.sqlite3, job_search.sqlite3, and
examples/filter_policy.example.toml (the validation runner's policy).
The authoritative-only metadata enrichment on 2026-11-06 intentionally left
the search index stale. The commands above are maintenance workflows, not
required startup steps during a repository cleanup. Leave the index untouched
until a separate synchronization or rebuild is authorized; a stale-index
diagnostic is expected in the meantime.
Screening version 5 uses Strategy H as the sole geographic authority in `screen_job`
path. H REVIEW_ONLY is diagnostic and preserves the existing outcome; it does
not create a final DEFER. Reasons/evidence prefixed `geo_h` describe its action.
Deterministic and occupational exclusions remain effective. Legacy geographic
enforcement has been removed; cached outcomes must be recomputed under version 5.
All build/sync paths are configurable:

~~~powershell
.\.venv\Scripts\python.exe -m job_matcher.search_index sync --catalog "phase2_validation.sqlite3" --index "job_search.sqlite3" --policy "examples/filter_policy.example.toml"
~~~

The UI sidebar must select those same paths. A missing, incomplete, stale,
incompatible or unreadable index produces an explicit diagnostic and build/sync
instructions. Rebuild repairs a corrupt index; sync updates a valid existing
index. The index is optional to acquisition: no acquisition module imports or
opens it. No catalog schema, source facts, identity links or checkpoints change.

Open http://127.0.0.1:8501 and select **Search**. Empty keywords browse the catalog.
The local Streamlit configuration binds to loopback and disables usage statistics.
Stop with Ctrl+C. Opening a posting is an explicit browser action.
No acquisition or AI calls run from the dashboard.

In PyCharm, create a Python run configuration with module streamlit, parameters
"run scripts/dashboard.py", the project root as working directory and
.venv\Scripts\python.exe as interpreter. Build/sync configurations use module
job_matcher.search_index with parameters rebuild or sync.

## Search and identity semantics

- Default: active postings, logical KEEP, title + description, 25 results.
  Page sizes 50/100 and filter-driven page resets remain. Six sorts cover first
  discovered, last seen, company, title, newest posted and oldest posted.
- Literal substring search accepts arbitrary new words without configuration.
  Use the AND/OR selector, or one uniform uppercase explicit operator:
  scraping OR scraper, Python AND automation, "web scraping", Playwright.
  Explicit operators override the selector. Mixed operators and unclosed quotes
  produce validation messages. Quoted "AND" is a literal word.
- AND terms can match different selected fields of ONE representation; they
  cannot be spread across representations. Keyword, source, company, location,
  discovery-date and active filters must all match that same representation.
- A matching representation selects its logical job. Logical eligibility uses
  ALL active siblings, even outside the selected source or keywords. Each sibling
  must independently pass deterministic filtering AND contextual screening.
  A rejected filter forces that representation to EXCLUDE; among active siblings,
  KEEP wins over DEFER over EXCLUDE. No evidence is combined across unsuitable
  siblings. Inactive-only jobs are EXCLUDE when included, even if a representation
  would pass screening. The identity resolver is unchanged; no speculative links.
- Every sibling remains inspectable, including inactive siblings, with its title,
  company, source, location, description, links and reasons. The index omits
  unlinked postings, as the previous dashboard did.
- First discovered is the minimum linked first_seen_at; last seen is the maximum
  last_seen_at. The discovery filter applies to the MATCHING posting's first_seen,
  so a newly discovered sibling can select an older logical job. Neither value is
  the original publication date.
- The summary title/company comes from the first source-key-ordered active KEEP
  sibling, otherwise the first active DEFER sibling, otherwise the first sibling.
  This preserves the former dashboard's representative selection. Sorts use
  logical UUIDs to break ties deterministically.
- Descriptions are plain text. Only HTTP(S) posting links without credentials,
  whitespace or control characters are enabled. No source HTML executes.

## FTS5 design and substring compatibility

The separate database has four application tables:

| Table | Purpose |
| --- | --- |
| text_index | FTS5 title, description, company, location |
| postings | Persistent source triple, logical ID, metadata, hashes and screening cache |
| logical_jobs | Precomputed logical outcomes, representative labels and observation dates |
| meta | Complete marker, source state, policy/code fingerprints and sync time |

FTS row IDs reference postings.id; the durable source identity remains the
(source, source_scope, source_job_id) triple. Logical IDs remain original UUIDs.
Indexes support source/activity filtering, sibling lookup and logical sorting.
No raw API JSON is copied into the search database. Search text appears once in
FTS content storage, alongside FTS posting lists; the catalog remains authoritative
for descriptions and URLs.

FTS5 uses "trigram case_sensitive 1" with full position detail. Before indexing and
matching, ASCII A-Z is converted to a-z. This deliberately reproduces SQLite LIKE's
ASCII case-insensitivity while retaining its case-sensitive non-ASCII behavior.
No stemming, accent removal or full Unicode folding is introduced. Full positions
allow exact contiguous phrases and field restrictions without candidate text scans.

For each term of at least three Unicode characters, the service generates a
quoted FTS phrase, escaping internal quotation marks, and restricts it to the
selected fields. Parameters bind the generated expression; user text is never
executed as MATCH operators or SQL. Punctuation, percent, underscores, SQL syntax
and FTS field/operator syntax remain literal. Terms shorter than three characters
(or containing NUL) use parameterized escaped LIKE over FTS's stored text. They
remain correct but may require a scan. Company/location sidebar filters continue
to use literal LIKE over lightweight metadata.

There is no intentional keyword semantic change from the previous LIKE search.
Synthetic parity tests include punctuation, phrases, short words, operator-like
text, Unicode and all field modes. The cache returns the same current decisions;
it does not invent historical evaluations.

## Persistent processing cache and synchronization

Each posting stores separate filter, contextual-screening and final outcomes,
filter reasons (including matched terms), screening reasons/evidence, geography,
evaluation/source timestamps, policy version/fingerprint, screening version and
an implementation fingerprint. The fingerprint covers filter, screening,
geography, occupational rules, contracts, reconstruction and indexing code files.
The parsed policy is fingerprinted as well as retaining the policy file hash.
This conservative design also catches code changes without a manual version bump.

Search never calls either evaluator. There is no query-result cache or keyword
allowlist. Different searches reuse the same per-posting cached decisions.

The source schema has no reliable content revision counter or change log.
Consequently sync streams ALL currently linked source inputs in bounded batches
(default 250), comparing three SHA-256 fingerprints:

1. Four searchable text fields: only a change here rewrites that FTS row.
2. Screening inputs: text, logical ID and raw payload. Raw payload hashing is
   conservative: even an unrelated payload field change can cause rescreening,
   but the payload is not duplicated. Logical reassignment refreshes provenance.
3. Lightweight lifecycle/retrieval/link metadata: observation timestamps and
   deactivation/reactivation update metadata without rescreening unchanged inputs.

A policy or implementation change rescreens affected cached rows (currently all)
without rewriting unchanged FTS text. New rows are indexed and screened. Deleted
records and removed identity links remove index/cache entries. Logical summaries
are recomputed in SQL when anything changes. A no-change sync performs no FTS
updates or evaluations. It still pays the full source read/hash cost; last_seen
alone is never evidence of a content change.

Rebuild uses a unique temporary sibling database, FTS integrity and SQLite
quick checks, then os.replace after closing connections. The previous index stays
available until publication. A failed/interrupted rebuild cannot publish partial
results. Sync uses one BEGIN IMMEDIATE transaction; posting, FTS, logical summary
and completion metadata changes commit together. Exceptions/interruptions roll
back; rerunning is safe. An OS kill may leave an unpublished *.building.sqlite3
artifact, which can be removed while no build is running. On Windows, close active
readers before replacing an existing index; an open handle can make publication
fail, leaving the previous index intact.

The dashboard opens BOTH databases read-only and uses read transactions. Freshness
checks compare the recorded source path, file identity, size, nanosecond times,
SQLite header/change counter and WAL state, as well as policy/implementation
fingerprints. These checks are cheap guards for ordinary commits/replacements,
not cryptographic whole-file verification. Restoring files while deliberately
preserving every file marker is outside this mechanism; run sync after restores.
Source state is checked again before returning details. Sync checks source state
during its read snapshot; rebuild checks again before publication. Detected
concurrent changes abort. A commit after the final check instead leaves a stale
generation that the dashboard rejects; rerun sync. No stale results are silently
labelled current.

Run build/sync between acquisitions. Long source reads can delay writers in
rollback-journal mode, and simultaneous source changes require retrying. Readers
during a sync either see the previous complete index or the new committed one;
a stale previous generation is rejected. File-state changes may conservatively
request sync even if logical content is unchanged.

## Validation and benchmarks

~~~powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .

# Existing index: read-only queries (fails clearly if the index is stale):
.\.venv\Scripts\python.exe scripts/benchmark_catalog_search.py

# Disposable fixture paths; catalog.sqlite3 must be a populated test catalog:
$benchRoot = Join-Path $env:TEMP "job-matcher-benchmark"
New-Item -ItemType Directory -Force $benchRoot | Out-Null
$catalog = Join-Path $benchRoot "catalog.sqlite3"
$index = Join-Path $benchRoot "search.sqlite3"

# Explicitly authorize rebuilding this test index and timing a no-op sync:
.\.venv\Scripts\python.exe scripts/benchmark_catalog_search.py --catalog $catalog --index $index --build --compare --exercise-sync

# Read-only comparison using the now-current test index:
.\.venv\Scripts\python.exe scripts/benchmark_catalog_search.py --catalog $catalog --index $index --compare

# Selected files remain read-only; change/sync timings use disposable copies:
.\.venv\Scripts\python.exe scripts/benchmark_catalog_search.py --catalog $catalog --index $index --exercise-sync
~~~

Default and `--compare` modes never synchronize or rebuild. They require a current
index; a stale or incompatible index fails with a diagnostic instead of repairing
it. A stale production index requires separate explicit maintenance.
`--build` requires an explicit `--index`, rebuilds only that index,
and times its subsequent no-op sync. It rejects the selected catalog, either
production database, and filesystem aliases of those paths before writing.
It never authorizes catalog changes. Without `--build`, the report omits
`build_*` and `noop_sync_*` metrics. There is no new selected-index sync flag.

The benchmark checks the selected catalog's SHA-256 before/after, reports build
time when requested, index size, lookup/filter/count/sort/detail timings and
Windows peak process working set where available. Two consecutive query runs use
fresh SQLite connections but retain OS cache state; they are first/repeat runs,
not a claim that the OS disk cache was flushed. Lookup timing is separately
measured after each full query and is therefore warmed. Sorting/page timing
includes repeating candidate lookup; it is not pure sorting time. --compare
checks the four representative counts and first-page logical IDs against the
legacy implementation. `--exercise-sync` uses selected catalog identities with
small synthetic descriptions/raw evidence in isolated temporary databases. It
preserves schema v2 or v3 (no migration), metadata including NULLs, posting dates,
links and lifecycle state. Named-column inserts support both fresh and migrated
v3 column orders. Exactly five linked descriptions change before the timed sync;
at least five linked postings are required. No selected file is written by this
mode. It is not a measurement on real raw payload volume; full query phases
still require the selected index to be current. Temporary files are removed even
on failure. Run `pytest tests/test_benchmark_catalog_search.py` for small
self-contained v2/v3 fixtures and safety checks without production access.

The direct query in `catalog_search.py` remains the regression comparison
oracle; the dashboard uses `indexed_search.py`. The index requires SQLite FTS5.

FTS stores full trigram positions and normalized searchable text, so the index
costs disk space. Short terms still scan. A future compact candidate index with
literal verification could reduce storage, but would add complexity. A source
revision log could avoid full sync scans if acquisition later gains that contract.
No automatic acquisition integration is introduced.
