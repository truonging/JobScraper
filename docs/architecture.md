# Architecture

## Status and scope

Metadata enrichment adds authoritative schema v3 (two nullable normalized columns)
and search schema v2. See [job metadata](job_metadata.md) for mappings, backfill,
date provenance and aggregation. Screening remains version 5 and ignores these
fields. Earlier schema-version references below describe the lifecycle design.

Phases 1 and 2 are complete. The project includes
source-independent full-snapshot acquisition contracts, synchronous Lever and
Greenhouse adapters, a SQLite persistence adapter, manual acquisition commands,
and lifecycle, logical-identity, and deterministic filtering components under
`src/job_matcher/`, with tests under `tests/`. Authoritative SQLite schema version
3 preserves the version 2 lifecycle and checkpoint design and adds nullable
employment/workplace metadata. Derived search schema version 2 serves the
read-only Streamlit dashboard.

This document records the system's current high-level responsibilities and
boundaries. The choices recorded here do not decide additional external
providers beyond Lever/Greenhouse, future interfaces, AI models, scoring rules,
or document formats.

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

Lever retries only explicitly classified transient failures at the individual
page-request boundary, with at most three attempts and bounded exponential
backoff. Parsing and normalization failures are not retryable. If any page still
fails, the adapter returns no snapshot and persistence receives no partial batch.

The normalized representation and raw source record share a source-local key
and retrieval time. A successful adapter acquisition returns a complete
`SourceSnapshot` for one source scope, including when the snapshot contains no
postings. Failed or malformed acquisitions do not produce a snapshot.

`NormalizedJob.description` must be a string, but an empty string is valid and
represents unavailable substantive source content. Adapters preserve available
description text without fabricating content or substituting generic boilerplate.
Descriptionless postings remain in successful snapshots and participate normally
in persistence, lifecycle tracking, logical identity, and deterministic filtering.
Required source identity, company, title, URL, and retrieval metadata retain their
validation requirements. AI eligibility for insufficient descriptions is deferred
to Phase 3.

### Greenhouse acquisition

`GreenhouseJobSource` fetches the public complete-board jobs response, validates
the full array, and returns the existing source-independent contracts. Metadata
totals are optional consistency checks, not required completeness fields. A
failed request or posting normalization never yields a partial snapshot.
Persistence and identity implementations are unchanged; source-local identity
remains the `(source, source_scope, source_job_id)` triple.

The complete-board endpoint is `/v1/boards/{board_token}/jobs?content=true`;
the adapter does not invent pagination. A supplied `meta.total` must agree with
the jobs array. Invalid envelopes, duplicate posting IDs or malformed postings
fail the entire snapshot; an empty successful board reconciles an empty scope.
Posting identity uses the public posting `id`, not `internal_job_id`; prospect
posts with a null internal ID remain valid. Company comes from the registry,
`absolute_url` supplies the job URL, and no application URL is invented.
Publication metadata uses `first_published`, never `updated_at` as a fallback.
Raw job objects remain intact and missing content becomes an empty description.

Greenhouse retries transient transport failures and HTTP 408/429/500/502/503/504
up to three attempts, with bounded backoff and Retry-After (five seconds by
default). Longer Retry-After values fail safely; HTTP 404, TLS failures and
malformed source data do not retry. Diagnostics expose sanitized categories.

Shared validation helpers own registry loading and batch acquisition/catalog
processing. Provider/global reporting keeps independent contributions separate
from global logical identities. The limited Greenhouse runner is a composition
root and does not implement source fetching, persistence, or screening policy.
The combined IDE runner uses acquisition-only batches in Lever-then-Greenhouse
order and invokes shared catalog processing once after both batches. Independent
limits select the batches; the current configuration refreshes one Lever board
and acquires the full Greenhouse registry into the existing Lever SQLite catalog.
Unattempted scopes are preserved by the existing lifecycle rules.
Strategy H alone enforces geographic eligibility for both sources through
`screen_job`. Lever job-location alternatives are opening-specific evidence;
Greenhouse employer office associations do not establish eligibility. Uncertain
geography preserves non-geographic outcomes. No Phase 3 behavior is introduced.

### Persistence

Persistence owns storage and retrieval. It does not contain source-acquisition,
fit-evaluation, ranking, or resume-generation policy.

Stored source facts, normalized values, candidate facts, AI interpretations,
and generated artifacts have distinct ownership even if a future storage
technology keeps some of them together physically.

SQLite remains behind the persistence interface. Schema version 3 stores
separate current raw, normalized, and source-posting lifecycle records linked by
source-local identity. It also stores the latest successful retrieval time for
each source scope. Historical source versions and lifecycle events are not
retained.

Schema initialization is explicit. Valid version 1 and version 2 databases migrate
to version 3 while preserving current source records. Since
version 1 did not retain observation history, migration uses each posting's
current retrieval time as both its first- and last-seen time. Version 2 also
introduced logical-job and source-link tables, retained in version 3.
Persistence can create durable
assignments and derive logical-job activity from linked source-posting lifecycle
state, but it does not decide whether two postings represent the same job.

### Application orchestration

Application orchestration coordinates workflows across source adapters,
persistence, deterministic processing, AI services, and user-facing interfaces.
It owns sequencing and recovery decisions; interfaces such as a CLI, API, or UI
do not contain source or persistence implementations.

The acquisition service fetches a complete source snapshot and passes it to
persistence for one atomic reconciliation. The Phase 2 pipeline then resolves
unlinked source postings, retrieves active linked postings, evaluates them, and
aggregates unique eligible logical jobs. The manual CLI is the composition root:
it constructs the Lever, SQLite, identity, and filtering components, explicitly
initializes or validates the database schema, and invokes the application
service.

Each stage retains its own failure and durability boundary; the pipeline is not
one transaction. Phase 2 deliberately reevaluates the complete active linked
catalog after each acquisition. This is a simple current behavior rather than a
permanent incremental-processing strategy. Scheduling, workflow-level retries, persisted
processing state, targeted reevaluation, and later processing stages remain
deferred.

### Job identity and state

Source identity, logical job identity, source-observed lifecycle, and candidate
application progress are separate concepts. Deduplication or identity resolution
must not silently destroy source provenance. Job-source lifecycle state must not
be conflated with application workflow state.

Lifecycle belongs to source postings. A source posting is active when present in
the latest successfully completed full snapshot for its source scope and becomes
inactive after one successful snapshot in which it is absent. Failed or
malformed acquisitions cannot change lifecycle state, and later observation
reactivates the posting. Lifecycle timestamps record first observation, most
recent observation, and the successful snapshot that established inactivity.

Successful snapshots for a source scope must have strictly increasing retrieval
times. Equal or older snapshots are rejected without changing current source
records, lifecycle state, or the source-scope checkpoint. This is the current
Phase 2 persistence invariant rather than a permanent replay policy.

Logical jobs use opaque stable UUIDs. A durable source-posting link assigns each
source posting to one logical job; ordinary source-field changes do not move an
existing link. Logical-job activity is derived: a logical job is active when at
least one linked source posting is active.

The identity resolver operates only on normalized source-independent records and
may consider active and inactive linked postings. It automatically links an
unlinked posting only when an exact same-field canonical job or application URL,
an equal normalized company, and all qualifying matches identify one logical
UUID. Job and application URLs are separate evidence categories. Conflicting or
ambiguous URL evidence creates a separate logical job.

An exact normalized company, title, location, and description fingerprint can
identify a duplicate candidate but cannot automatically merge logical jobs.
Candidate and ambiguous-match evidence is returned from resolution but is not
persisted. The resolver builds source-independent in-memory exact-match indexes
for each resolution pass without provider-specific rules, fuzzy matching, AI, or
persisted identity caches. Manual identity correction and persisted
candidate-review workflows remain deferred.

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

Phase 2 deterministic filtering is limited to clearly unsuitable postings and
uses source-independent normalized fields. Its version 1 TOML policy contains
literal title and description inclusion/exclusion terms plus exact
company exclusions. Matching uses Unicode case folding and collapsed whitespace;
an empty inclusion list imposes no requirement, and every nonempty inclusion
group must independently match. Policy files receive only a SHA-256 identifier
of their exact contents for provenance.
The required version 1 location rule slot must be empty; Strategy H owns
geographic eligibility and deterministic filtering never matches location text.

The evaluator processes the linked source postings supplied by orchestration and
returns one transient decision per representation. Pipeline orchestration selects
every active linked posting in the persisted catalog. A logical job proceeds when
at least one active representation is eligible. Filtering does not inspect raw
source payloads or contain provider-specific behavior. Authoritative decision
history, targeted catalog reevaluation, scoring, ranking, model choice, prompts, and
human-review requirements remain deferred. Neither an AI provider nor a job
source controls final workflow decisions.

### Pre-AI screening

The separate `screening_pipeline` application service coordinates the existing
deterministic filter and the small `screening` classifier on supplied active
representations. `screening_sqlite` is a read-only projection adapter for the
current schema, streaming normalized jobs, durable links, and matching raw
records within one read transaction. It does not initialize or migrate schema.
The offline screening script is only a composition root and report interface.

Geographic raw-evidence interpretation is shared by screening callers through
Strategy H. Structured job metadata extraction is separately shared by live
adapters and offline backfill in `job_metadata`. Identity resolution and
persistence lifecycle behavior remain independent. Decisions retain version,
source/logical identity, retrieval and
evaluation timestamps, reason codes, and matched source evidence. Raw records
remain authoritative and unchanged. Filter decisions retain policy provenance.
The screening service returns transient derived results; the search index
persists its own rebuildable decision cache. Any active representation passing both
stages keeps its logical job eligible; otherwise an unresolved content deferral
survives an excluded representation. Geographic ambiguity alone never defers or
excludes a job.

Screening version 2 introduced four fixed contextual
occupational checks in `occupational_screening`: paid survey participation,
retail store operations, licensed clinical care, and food-service operations.
They require title and duty evidence, preserving technical occupations and
specific engineering duties without allowing generic technology vocabulary to
override clear unrelated work. Independent category reasons preserve overlap
and source duty clauses. The reporting helper masks category reasons to compute
logical-job funnels from the existing decisions; it does not run another
pipeline. No acquisition, identity, lifecycle, travel-booking exclusions, or AI
calls are introduced. These occupational checks remain in screening version 5.

Occupational exclusions require positive job-duty evidence, not application
questionnaires or negated duties. Paid participation also requires compensation
evidence. Software QA, clinical technology, survey analysis and POS development
remain distinct from unrelated occupations; generic technology vocabulary does
not override clear nontechnical duties. There is no source-wide blacklist.

#### Effective decisions and logical aggregation

`screening.py` owns `combine_outcomes`, `combine_decisions`, `outcome_priority`,
`aggregate_outcomes` and `aggregate_screening`. Deterministic outcomes are
ELIGIBLE/REJECTED; screening outcomes are KEEP/DEFER/EXCLUDE. REJECTED always
combines to EXCLUDE; ELIGIBLE preserves the same representation's screen outcome.
`combine_decisions` changes only the derived outcome, retaining source/logical
identity, screening version, timestamps, reasons and evidence. Filter reasons
remain separate; source facts are never rewritten.

Combine each representation before grouping: a filter pass on one posting and
a screen pass on another do not jointly establish eligibility. Logical priority
is KEEP > DEFER > EXCLUDE across active representations. Aggregation helpers
operate on the explicitly supplied set and do not select lifecycle state.
Search may show inactive siblings in details and observation-date summaries,
but inactive outcomes cannot establish logical eligibility. Inactive-only groups
are EXCLUDE when included; active-only processing omits them. Empty outcome
groups default to EXCLUDE and empty `aggregate_screening` input returns `{}`.
Representative ties retain source-key order; publication dates use active
siblings. A provider's independent EXCLUDE may coexist with a global KEEP.

The index SQL projects the shared priority ranks while retaining SQL grouping
and representative selection. Occupational funnel reporting masks selected
reasons on existing decisions; it does not rescreen or mutate the original
evidence. Per-provider contributions can overlap and must not be summed as
global unique-job counts. Geographic policy details are maintained in
[Strategy H](geographic_classification.md).

### Derived search and dashboard

`search_index.synchronize` explicitly builds or updates the separate search
database from the authoritative catalog. It caches per-posting filtering and
screening decisions with policy, input and implementation fingerprints, and
aggregates logical jobs. `indexed_search` validates freshness and queries FTS
and logical summaries, then reads source details from the authoritative catalog.
`scripts/dashboard.py` is the Streamlit composition root; startup and queries
do not acquire, migrate, backfill or synchronize data. The direct
`catalog_search.search_catalog` path remains a benchmark and regression oracle.

The 2026-11-06 authoritative-only metadata enrichment deliberately leaves the
search database stale. This is expected and must not be repaired as part of
repository cleanup. See [job metadata](job_metadata.md) and
[dashboard operations](dashboard.md) for separately invoked maintenance workflows.

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
which it was produced. Current source keys, logical IDs, policy fingerprints and
screening versions provide that linkage; AI artifact schemas remain deferred.

## Deferred decisions

The following remain open until the phase that needs them:

- additional external job sources and acquisition methods;
- canonical job and candidate-profile schemas;
- persistence technology beyond Phase 1 and future schema evolution;
- manual logical-job merge, split, unlink, and candidate-review workflows;
- persisted filter-decision history and targeted reevaluation;
- AI providers, models, prompts, evaluation structure, and ranking policy;
- scheduling, workflow-level retries, and deployment;
- interfaces beyond the current catalog dashboard and human-review workflow;
- resume templates, rendering technology, and output formats;
- application-tracking states and application-submission behavior; and
- hosting, CI/CD, containers, and distributed infrastructure.
