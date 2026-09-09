# Architecture

## Status and scope

The project is currently in Phase 1. It includes source-independent acquisition
contracts, a synchronous Lever source adapter, and a SQLite persistence adapter
under `src/job_matcher/`, with tests under `tests/`. No acquisition workflow is
implemented yet.

This document records the system's current high-level responsibilities and
boundaries. It does not select external providers, user interfaces, AI models,
scoring rules, or document formats.

## System responsibilities

The intended system supports three related workflows:

- discover and normalize job opportunities;
- evaluate how well a job matches a candidate's verified experience; and
- tailor resume content for a selected job without inventing candidate facts.

These workflows may form a pipeline, but their components remain separately
responsible and communicate through explicit data contracts.

## Established constraints

- A job-source adapter only fetches source data and normalizes it into the
  system's source-independent job representation.
- Persistence is separate from source acquisition. Source adapters do not own
  database behavior, and persistence does not fetch external jobs.
- A job is stored before it is sent for AI analysis so acquisition results can
  be retained and analyzed later.
- Source data and AI-generated interpretations are distinct records. AI output
  never overwrites source data or becomes a source fact.
- The candidate profile is the source of truth for candidate experience.
  Resume generation uses only verified experience and must not invent claims.
- Secrets, private candidate data, real resumes, generated application
  documents, and local databases remain outside version control.

## Component boundaries

### Job sources

Source adapters own communication with external job sources and translation
from source-specific responses into a source-independent representation. They
do not evaluate candidate fit, decide workflow state, access candidate data,
or generate resumes.

Phase 1 uses Lever's public Postings API as its first structured source. The
adapter acquires and normalizes published postings without accessing
persistence.

The normalized representation must retain enough provenance to identify its
source and relate it to the source data. The exact representation and retention
policy are deferred.

### Persistence

Persistence owns storage and retrieval. It does not contain source-acquisition,
fit-evaluation, ranking, or resume-generation policy.

Stored source facts, normalized values, candidate facts, AI interpretations,
and generated artifacts have distinct ownership even if a future storage
technology keeps some of them together physically.

Phase 1 uses SQLite behind the persistence interface. It stores separate current
raw and normalized records linked by source-local identity. Reacquisition updates
those current records atomically; historical versions and lifecycle tracking are
deferred.

### Application orchestration

Application orchestration coordinates workflows across source adapters,
persistence, deterministic processing, AI services, and user-facing interfaces.
It owns sequencing and recovery decisions; interfaces such as a CLI, API, or UI
do not contain source or persistence implementations.

The concrete workflow, scheduling model, and retry mechanism are deferred.

### Job identity and state

Source identity, logical job identity, source-observed lifecycle, and candidate
application progress are separate concepts. Deduplication or identity resolution
must not silently destroy source provenance. Job-source lifecycle state must not
be conflated with application workflow state.

Identity rules, lifecycle states, deduplication algorithms, and processing order
are deferred until their requirements are defined.

### Candidate profile

The candidate profile owns verified candidate facts. AI services may reference
those facts but may not add to or mutate the verified record. Changes to the
profile require an explicit candidate-data workflow outside AI interpretation.

The profile format, verification process, and storage mechanism are deferred.

### Fit evaluation and ranking

Fit evaluation produces interpretations derived from stored job data and
verified candidate facts. AI-provider responses are untrusted outputs: the
application validates them before storage or use. Interpretations remain
traceable to the inputs used to produce them.

Filtering, scoring, ranking, recommendation policy, model choice, prompts, and
human-review requirements are deferred. Neither an AI provider nor a job source
controls final workflow decisions.

### Resume tailoring and rendering

Resume tailoring may select, organize, or rewrite verified candidate experience
for relevance to a job. Each resulting claim must remain grounded in verified
candidate facts. Generated content is a derived artifact and does not update the
candidate profile.

Content tailoring and document rendering are separate responsibilities. Their
technologies, templates, formats, and approval workflow are deferred.

### External services and configuration

External job sources and AI providers are integration boundaries. Failures or
malformed responses at those boundaries must not corrupt stored source or
candidate facts. Provider credentials and environment-specific configuration
remain outside domain data and source control.

Application logs and diagnostics must avoid exposing secrets or private
candidate content.

## Data provenance

The system must preserve the distinction among:

- data received from a job source;
- normalized source facts;
- verified candidate facts;
- deterministic processing results;
- AI-generated interpretations; and
- generated resume artifacts.

Derived data should retain enough linkage to identify the source inputs from
which it was produced. Exact identifiers, versions, and schemas are deferred.

## Deferred decisions

The following remain open until the phase that needs them:

- additional external job sources and acquisition methods;
- canonical job and candidate-profile schemas;
- persistence technology beyond Phase 1 and future schema evolution;
- job identity, lifecycle, deduplication, and filtering rules;
- AI providers, models, prompts, evaluation structure, and ranking policy;
- orchestration, scheduling, retries, and deployment;
- user interface and human-review workflow;
- resume templates, rendering technology, and output formats;
- application-tracking states and application-submission behavior; and
- hosting, CI/CD, containers, and distributed infrastructure.
