# Architecture

## Status and scope

Phase 1 is complete and Phase 2 is in progress. The project includes
source-independent full-snapshot acquisition contracts, a synchronous Lever
source adapter, a SQLite persistence adapter, a manual acquisition command,
and foundational lifecycle, logical-identity, and filter-policy contracts under
`src/job_matcher/`, with tests under `tests/`.

This document records the system's current high-level responsibilities and
boundaries. The Phase 1 choices recorded here do not decide additional external
providers, later user interfaces, AI models, scoring rules, or document formats.

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

The normalized representation and raw source record share a source-local key
and retrieval time. A successful adapter acquisition returns a complete
`SourceSnapshot` for one source scope, including when the snapshot contains no
postings. Failed or malformed acquisitions do not produce a snapshot.

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

The application service fetches a complete source snapshot and passes its jobs
to persistence as one atomic batch. The manual CLI is the composition root: it
constructs the Lever and SQLite adapters, explicitly initializes or validates
the database schema, and invokes the application service. Scheduling, retries,
and later processing stages remain deferred.

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

Logical jobs use opaque stable UUIDs. A durable source-posting link assigns each
source posting to one logical job; ordinary source-field changes do not move an
existing link. Logical-job activity is derived: a logical job is active when at
least one linked source posting is active. Persistence and transition behavior
for these contracts remain deferred to later Phase 2 issues.

Deduplication will use conservative deterministic identity rules. Exact
canonical URLs may provide strong evidence when they identify the same
opportunity. Equal normalized content may identify a duplicate candidate but
cannot automatically merge logical jobs. Evidence contracts, URL
canonicalization, matching, and assignment behavior remain deferred.

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
literal title, location, and description inclusion/exclusion terms plus exact
company exclusions. Matching uses Unicode case folding and collapsed whitespace;
an empty inclusion list imposes no requirement. Policy files receive only a
SHA-256 identifier of their exact contents for provenance. Filter evaluation,
scoring, ranking, model choice, prompts, and human-review requirements remain
deferred. Neither an AI provider nor a job source controls final workflow
decisions.

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
- SQLite lifecycle and logical-identity persistence and schema migration;
- deterministic identity evidence, matching, and logical-job assignment;
- deterministic filter evaluation and processing order;
- AI providers, models, prompts, evaluation structure, and ranking policy;
- scheduling, retries, and deployment;
- user interface and human-review workflow;
- resume templates, rendering technology, and output formats;
- application-tracking states and application-submission behavior; and
- hosting, CI/CD, containers, and distributed infrastructure.
