# Job Matcher — Product Context

Job Matcher is an AI-assisted job discovery, job-fit evaluation, and
resume-tailoring system designed to reduce the manual work involved in finding
relevant software-engineering opportunities and preparing targeted applications.

## Problem

Searching for jobs across multiple sources creates several repetitive tasks:

1. checking multiple job sources for new postings;
2. determining which postings are actually relevant;
3. comparing each job description against a candidate's experience;
4. identifying which experiences and projects best demonstrate fit; and
5. tailoring a resume to emphasize relevant, truthful experience using
   terminology appropriate to the job.

Simple keyword matching is insufficient for many of these decisions because
relevant experience may be semantically related without using the exact
terminology found in a job description. At the same time, sending every
collected posting directly to an LLM would be unnecessarily expensive and would
give AI responsibility for decisions that can often be made deterministically.

Job Matcher is intended to combine deterministic software with targeted AI
reasoning.

## Intended workflow

The following illustrates the approximate long-term workflow. Exact processing
rules and sequencing will be decided in the phases that implement them.

```text
Job Sources
    ↓
Fetch and Normalize
    ↓
Persist
    ↓
Deduplicate / Track Lifecycle
    ↓
Deterministic Filtering
    ↓
AI Job-Fit Evaluation
    ↓
Human Review
    ↓
Resume Tailoring
    ↓
Application Tracking
```

Jobs are collected from configured sources and normalized into a common
internal representation. They are persisted before AI analysis so that source
acquisition and analysis remain independent.

Deterministic filtering is intended to remove clearly unsuitable jobs using
objective criteria where possible. Remaining plausible jobs can then be
evaluated against a detailed candidate experience profile.

The candidate profile contains substantially more information than a normal
resume, including verified projects, responsibilities, technologies,
accomplishments, and alternative descriptions of experience. It is the source
of truth for candidate facts.

AI is used where semantic reasoning provides value, such as understanding how
candidate experience relates to a job description when their wording differs.
For promising jobs, the system can select relevant verified experience and
generate tailored resume content using terminology from the job description.

Generated claims must remain grounded in verified candidate experience. The
system must not fabricate skills, responsibilities, accomplishments, or
qualifications. The user remains responsible for reviewing recommendations and
application materials.

## Design goals

The project is intended to explore:

- integrating multiple external job sources behind common contracts;
- normalizing heterogeneous external data;
- persistent and restartable data pipelines;
- job identity, deduplication, and lifecycle tracking;
- deterministic filtering before expensive AI processing;
- structured and validated LLM output;
- provenance between AI output and source data;
- grounding generated content in verified facts;
- AI usage and cost management;
- failure handling at external-service boundaries;
- automated testing of an AI-assisted application; and
- human-in-the-loop workflows.

These are product and engineering goals, not commitments to specific providers,
schemas, models, interfaces, storage systems, or document formats. See the
[architecture](docs/architecture.md) for current boundaries and deferred
decisions.

## AI philosophy

AI is a component of the system rather than the system itself. The application
should prefer deterministic logic for problems that can be solved reliably
without an LLM. AI should be introduced where semantic understanding or
natural-language generation provides meaningful value.

The two primary intended AI use cases are:

1. **Job-fit evaluation:** Compare a job description against the candidate's
   detailed verified experience and identify meaningful matches, gaps, and
   potential blockers.
2. **Resume tailoring:** Select and rewrite relevant verified experience to
   better align with a selected job while preserving factual accuracy.

AI-generated data is derived, untrusted output. The application must validate it
before storage or use, and it must not overwrite source job data or verified
candidate facts.

## Current status

**Phase 1: Acquisition and persistence** and **Phase 2: Identity, lifecycle,
deduplication, and deterministic filtering** are complete. The project includes
source-independent full-snapshot acquisition
contracts, synchronous Lever and Greenhouse adapters, SQLite
persistence, a manual acquisition command, and foundational contracts for
source-posting lifecycle, logical identity, and deterministic filter policies.
SQLite schema version 3 reconciles successful source snapshots into current job
and lifecycle state. A source-independent identity resolver can assign unlinked
source postings to stable logical-job UUIDs using conservative deterministic
evidence.

Pre-AI screening remains version 5, with Strategy H as the sole geographic
authority. A separate schema v2 search database stores derived FTS and screening
results for the read-only Streamlit dashboard. Metadata enrichment currently
leaves that index intentionally stale; see [job metadata](docs/job_metadata.md).
Acquisition and dashboard startup never synchronize it automatically.

The deterministic filter evaluator applies the version 1 policy to supplied
linked normalized postings and returns transient decisions with source, logical
job, policy, and evaluation-time provenance. A logical job proceeds when at
least one supplied active representation is eligible.

LLM integration, resume generation, and application workflows remain outside
the current implementation.

## Development setup

Use Python 3.12. From the project root in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

If a Python 3.12 `.venv` already exists, reuse it and skip its creation. In
PyCharm, select `.venv\Scripts\python.exe` as the project interpreter. The
editable install exposes `src/job_matcher` without manual `PYTHONPATH` changes.

The project currently has no runtime dependencies. Pytest and Ruff are
development dependencies, and setuptools is the build backend. Dependency
ranges in `pyproject.toml` are not a lockfile.

## Verification

Run from the project root:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pytest
```

The tests cover package imports, acquisition and Phase 2 contracts, filter-policy
loading and evaluation, the Lever adapter, identity resolution, the complete
Phase 2 pipeline, and SQLite persistence and orchestration.

## Manual acquisition

Acquire all published jobs from one Lever site into SQLite:

```powershell
.\.venv\Scripts\job-matcher.exe acquire-lever `
  --site example `
  --company "Example Company" `
  --database jobs.sqlite3
```

The command explicitly creates or validates the SQLite schema before fetching.
The database path's parent directory must already exist. Repeated acquisitions
update the current raw and normalized records for each Lever posting ID rather
than creating historical copies. A posting becomes inactive when it is absent
from one later successful full snapshot and is reactivated if it reappears.

Existing schema version 1 and 2 databases migrate to version 3 during explicit
initialization. Version 3 adds nullable employment and workplace metadata to the
version 2 lifecycle design. Because version 1 retained only the latest
retrieval, migrated jobs use that retrieval time for both their initial
`first_seen_at` and `last_seen_at` lifecycle values.

Snapshot timestamps must increase strictly within each source scope. A snapshot
whose `retrieved_at` is equal to or earlier than the last successful snapshot is
rejected without changing stored jobs, lifecycle state, or the scope checkpoint.
This is a Phase 2 persistence invariant and may be reconsidered if replay support
is needed later.

The global Lever API is used by default. Use `--api-base-url
https://api.eu.lever.co/v0/postings` for an EU-hosted Lever site. Optional
`--timeout-seconds` and `--page-size` arguments control each synchronous fetch.

The command reports source and persistence failures without a traceback and
returns exit code 1. The adapter uses the bounded page-request retries described
below. The command does not schedule acquisitions, deduplicate across sources,
or perform filtering or AI analysis.

## Complete Phase 2 pipeline

Run acquisition, lifecycle reconciliation, logical identity resolution, and
deterministic filtering with one command:

```powershell
.\.venv\Scripts\job-matcher.exe run-lever-pipeline `
  --site example `
  --company "Example Company" `
  --database jobs.sqlite3 `
  --filter-policy private/filter_policy.toml
```

The command loads and validates the policy before acquisition, explicitly
initializes or validates SQLite, and then runs each Phase 2 stage in order. Its
summary distinguishes postings acquired in the current source snapshot, source
postings resolved during the current identity stage, active catalog records
evaluated during filtering, and unique logical jobs eligible after aggregation.

Phase 2 deliberately reevaluates every active linked posting in the persisted
catalog after each acquisition. Filter decisions and identity evidence remain
transient. Persisted processing state and targeted reevaluation may be added if
later workflows require them.

Each completed stage retains its own durability boundary. Snapshot reconciliation
is atomic, identity links are durable, and a later filtering failure does not
roll back completed persistence or identity work. The entire pipeline is not one
database transaction.

## Multi-company Phase 2 validation

`scripts/run_phase2_validation.py` acquires the verified Lever registry first,
then the verified Greenhouse registry into one SQLite catalog. It continues past
individual source-acquisition failures and performs identity resolution and
deterministic filtering once after both batches, followed by shared screening
and provider/global reporting. Failed scopes retain their prior persisted state.

Run the file directly from PyCharm or another IDE. Its constants configure the
registries, validation database, filter policy, request settings, sample sizes,
delay, and independent company limits. `COMPANY_LIMIT = None` selects the full
Lever registry; `GREENHOUSE_COMPANY_LIMIT = None` selects the full Greenhouse
registry. The current Lever limit is `1`, refreshing one board while retaining
the existing catalog; Greenhouse defaults to `None` for the full registry. A
limit of `0` skips acquisition for that provider. The default delay is 0.8 seconds
between boards within each batch. `DATABASE_PATH` defaults to the existing
`phase2_validation.sqlite3`, so Greenhouse joins the persisted Lever catalog.
Unattempted Lever scopes remain active. Existing catalogs
are reused intentionally and their existence is printed before initialization.

The final report distinguishes current-run acquisition totals from cumulative
catalog totals. A successful empty snapshot reconciles lifecycle state, while a
failed or malformed acquisition leaves that source scope unchanged. Detailed job
output is limited to small eligible and rejected samples.

The runner prints whether its database file existed before initialization.
Acquisition counts cover only the current run; catalog counts, filtering, and
samples cover the entire database, including retained active postings from failed
or unattempted scopes. Request diagnostics identify the site, pagination offset,
HTTP status or transport category, and available exception type and numeric error
code without printing arbitrary exception messages or response bodies.

The validation runner uses a configurable 20-second request timeout. Each page
gets at most three attempts, with 1-second and 2-second exponential backoffs and
a configurable 5-second delay cap. Only timeouts, explicitly classified temporary
connection/network/DNS failures, and HTTP 408, 429, 500, 502, 503, and 504 are
retryable. Valid `Retry-After` seconds or HTTP dates increase the wait when within
the cap; a longer requested wait fails acquisition rather than retrying too early.
HTTP 404, certificate errors, malformed postings, invalid JSON, and unexpected
response shapes fail immediately. Exhausted retries retain the sanitized cause,
page context, and attempt count. No partial snapshot is reconciled. Aggregate
retry metrics are deferred. The unrelated CLI timeout default remains 10 seconds.

For an isolated 100-company Lever-only validation from PyCharm, set
`COMPANY_LIMIT = 100`, `GREENHOUSE_COMPANY_LIMIT = 0`,
and set `DATABASE_PATH` to a new filename under `PROJECT_ROOT`, such as
`PROJECT_ROOT / "phase2_validation_100_clean_2026-11-03.sqlite3"`. Verify that the
file does not exist before starting, and expect `Database existed before run: no`.
Run `scripts/run_phase2_validation.py` using the project's interpreter. Reusing
that filename on a later run intentionally reuses its catalog; choose another
unused filename for another clean run. Preserve existing validation databases.

## Logical identity

Logical identity resolution operates only on persisted normalized jobs. It may
automatically link postings when the same URL field has an exact conservative
canonical match, normalized company names match, and every qualifying URL match
identifies the same logical-job UUID. Job URLs and application URLs are compared
only with their corresponding fields. Title and location differences do not
prevent an otherwise strong URL match.

If URL matches conflict or identify multiple logical jobs, the resolver creates
a separate logical job. An exact normalized company, title, location, and
description fingerprint is reported only as transient duplicate-candidate
evidence and never causes an automatic merge. Existing source links are durable
and normal source-field changes do not move them.

The resolver builds source-independent in-memory indexes for normalized company,
canonical job and application URLs, and content fingerprints during each pass.
It has no provider-specific behavior, fuzzy matching, persisted evidence, or
persistent performance indexing.

## Filter policy

Phase 2 defines a small version 1 TOML policy contract. A synthetic example is
available at `examples/filter_policy.example.toml`. Keep a real personal policy
at `private/filter_policy.toml`; the `private/` directory is excluded from
version control.

Matching is case-insensitive using Unicode case folding after trimming and
collapsing whitespace. Title and description rules use literal
substring matching. Company exclusions use exact normalized-name matching.
Terms are not regular expressions, and the policy does not use stemming, fuzzy
matching, synonyms, or AI. An empty `include_any` imposes no requirement; a
nonempty `include_any` requires at least one matching term, independently for
each configured field. Any exclusion match rejects the posting.
The required version 1 `[location]` arrays must remain empty: geographic
eligibility is enforced solely by Strategy H, independently of literal rules.

The evaluator processes exactly the linked normalized postings supplied by its
caller. Pipeline orchestration selects every active linked posting currently in
the catalog. Decisions are transient, and a logical job remains eligible when
any active representation is eligible. The evaluator does not rank jobs or
inspect source-specific raw payloads.

## Project layout

- `src/job_matcher/`: application package, including acquisition contracts, the
  Lever source adapter, SQLite persistence adapter, orchestration, and CLI.
- `tests/`: automated tests.
- `examples/`: synthetic, safe-to-commit configuration examples.
- `pyproject.toml`: packaging, development dependencies, pytest, and Ruff settings.
- [AGENTS.md](AGENTS.md): contributor instructions.
- [Architecture](docs/architecture.md): high-level responsibilities and boundaries.
- [Roadmap](docs/roadmap.md): project phases.
- [Dashboard](docs/dashboard.md): operation, search semantics and benchmarks.
- [Geographic policy](docs/geographic_classification.md): Strategy H evidence and enforcement.
- [Job metadata](docs/job_metadata.md): extraction, provenance and backfill/recovery.

## Greenhouse acquisition validation

The Greenhouse adapter uses the same source snapshots, SQLite schema, identity
resolver, deterministic filter, and screening service as Lever. The isolated
small-run entry point is `scripts/run_greenhouse_validation.py`; select a fresh
database with `--database` and explicitly add `--resume` for a repeated run.
It accepts at most five boards and protects the existing Lever validation file.
See [Greenhouse acquisition](docs/architecture.md#greenhouse-acquisition) for the
response and normalization contract, and the geographic policy below for screening.
The combined `run_phase2_validation.py` entry point supports the full registries
in Lever-then-Greenhouse order. The standalone Greenhouse runner remains limited
to five boards for small experiments.

## Local pre-AI screening

Run `scripts/screen_persisted_catalog.py` with the project interpreter to screen
the entire existing `phase2_validation.sqlite3` catalog. Optional `--database`
and `--policy` arguments select other local inputs; `--explain` prints JSON lines
with every filter decision, screening decision, matched evidence, source key,
retrieval timestamp, screening version, and final logical outcome. The script
opens SQLite read-only and never acquires jobs, initializes schema, resolves
identities, changes lifecycle, or calls AI. Phase 2 acquisition commands remain
independent; use this offline command after validation.

Screening version 5 uses Strategy H as the sole geographic eligibility authority,
replacing the former Lever and Greenhouse geographic rules. It evaluates each
source representation using normalized facts and matching raw location metadata.
Only H EXCLUDE enforces geographic exclusion; PASS and REVIEW_ONLY preserve
non-geographic outcomes. Uncertainty cannot itself create a final DEFER.
Testing-job disclaimers, missing-description deferrals, occupational exclusions
and deterministic filtering remain effective. No source facts are rewritten.
See [the geographic policy](docs/geographic_classification.md) for evidence
thresholds, structured reasons and cache refresh instructions.

Screening version 5 retains four independent contextual checks: paid poll/survey
participation, retail store operations, licensed clinical care, and food-service
operations. Each requires an occupational title and positive duty evidence;
paid participation also needs compensation evidence. Technical occupations and
explicit engineering duties are retained. Generic mentions of software, data,
or technology do not protect unrelated work. For example, cashier POS usage is
store work, while developing POS integrations is technical work. Clinical
psychological testing is patient care; software QA is technical work. Negated
duties and application questionnaires do not establish occupational exclusions.
Each matched category retains its title and duty clauses as evidence, including
multiple categories when they overlap. The command reports standalone matches,
overlaps, and a funnel in the order polls, retail, clinical, food, using actual
logical IDs and the existing representation-level aggregation.

KEEP is ready for later evaluation, EXCLUDE is unsuitable under explicit rules,
and DEFER needs substantive source content. Empty descriptions and exact
placeholders (`N/A`, `TBD`, `[Insert General Job Description Here]`) defer rather
than disappear. Short substantive descriptions remain eligible. An explicit
exclusion takes precedence over missing content. At the logical level, KEEP
beats DEFER, which beats EXCLUDE, after combining the existing filter and new
screen on each active representation. Excluded/deferred jobs remain in SQLite.

The legacy title filter has one narrow exception: the exact normalized title
`IT Dispatcher` ignores only the `dispatcher` exclusion. All other title and
description exclusions still apply, and other dispatcher occupations retain
their existing behavior.

The current [screening contract](docs/architecture.md#effective-decisions-and-logical-aggregation)
defines combination, active-sibling aggregation and reporting scope. Obtain
current catalog counts from the read-only screening command above; historical
snapshot totals are not maintained as current product documentation.

## Private data

Keep private candidate profiles in `candidate_profiles/` and generated resumes
in `generated_resumes/`. Both directories are ignored. Do not commit private
data elsewhere or force-add ignored files. `.env` files and local database files
are also ignored. Use only synthetic, non-sensitive data in test fixtures and
examples.

## Local catalog dashboard

The optional Streamlit UI searches a separate rebuildable FTS5 index with cached
per-posting screening decisions. Build it once, then sync after acquisition:

~~~powershell
.\.venv\Scripts\python.exe -m job_matcher.search_index rebuild
.\.venv\Scripts\python.exe -m job_matcher.search_index sync
.\.venv\Scripts\python.exe -m streamlit run scripts/dashboard.py
~~~

See [dashboard setup and search semantics](docs/dashboard.md) for dependency setup,
PyCharm launch instructions, cache invalidation, and read-only validation.
