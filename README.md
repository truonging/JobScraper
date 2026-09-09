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

**Phase 1: Acquisition and persistence** is complete. The project includes
source-independent acquisition contracts, a synchronous adapter for Lever's
public Postings API, SQLite persistence, and a manual acquisition command.

Filtering, LLM integration, resume generation, and application workflows remain
outside the current implementation.

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

The tests cover package imports, acquisition contracts, the Lever adapter, and
SQLite persistence and orchestration.

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
than creating historical copies.

The global Lever API is used by default. Use `--api-base-url
https://api.eu.lever.co/v0/postings` for an EU-hosted Lever site. Optional
`--timeout-seconds` and `--page-size` arguments control each synchronous fetch.

The command reports source and persistence failures without a traceback and
returns exit code 1. It does not schedule acquisitions, retry failures, remove
jobs missing from later fetches, track lifecycle, deduplicate across sources, or
perform filtering or AI analysis.

## Project layout

- `src/job_matcher/`: application package, including acquisition contracts, the
  Lever source adapter, SQLite persistence adapter, orchestration, and CLI.
- `tests/`: automated tests.
- `pyproject.toml`: packaging, development dependencies, pytest, and Ruff settings.
- [AGENTS.md](AGENTS.md): contributor instructions.
- [Architecture](docs/architecture.md): high-level responsibilities and boundaries.
- [Roadmap](docs/roadmap.md): project phases.

## Private data

Keep private candidate profiles in `candidate_profiles/` and generated resumes
in `generated_resumes/`. Both directories are ignored. Do not commit private
data elsewhere or force-add ignored files. `.env` files and local database files
are also ignored. Use only synthetic, non-sensitive data in test fixtures and
examples.
