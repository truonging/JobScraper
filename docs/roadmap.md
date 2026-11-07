# Project roadmap

Later phases are provisional and require their own scope and acceptance criteria.

| Phase | Focus |
| --- | --- |
| 0 | Bootstrap: Python 3.12 environment, package layout, documentation, pytest, and Ruff |
| 1 | Job acquisition, normalization contracts, and persistence |
| 2 | Job identity, lifecycle, deduplication, and deterministic filtering |
| 3 | Candidate experience profile and AI-assisted job-fit evaluation |
| 4 | Human review workflow, grounded resume tailoring, and document generation |
| 5 | Application tracking, end-to-end integration, automation, and hardening |

Phase 0 is limited to the scaffold and an import smoke test. All job processing,
storage, analysis, and document generation belong to later phases.

Phase 1 is complete. It provides source-independent acquisition contracts, a
synchronous Lever adapter, current-record SQLite persistence, and a manual
fetch-and-persist CLI workflow.

Phase 2 is complete. It adds source-posting lifecycle reconciliation, durable
logical-job identity, conservative deterministic deduplication, versioned
deterministic filtering, and a manual command that integrates those stages over
the active persisted catalog.

## Maintenance follow-ups

These are optional proposals, not additional Phase 3 prerequisites. Shared
screening combination/aggregation (formerly C1) and benchmark schema/write-safety
fixes (C7) are complete; their contracts live in architecture and dashboard docs.

| Area | Remaining work | Required safeguards |
| --- | --- | --- |
| Read projections (C2) | Define an internal projection contract instead of calling private repository row converters and duplicating SQL | Preserve schema v2/v3 compatibility, matching raw/retrieval provenance, transaction/error ownership and read-only access |
| Migration naming (C3) | Clarify helpers with v2 names that now produce schema v3 | Retain supported fresh/v1/v2/v3 paths, atomic rollback, idempotence and all source/link/lifecycle data |
| Retry headers (C4) | Consider sharing only Retry-After parsing with an explicit clock seam | Keep provider-specific transport classification, request boundaries, delay caps and malformed/large-header handling |
| Search contracts (C5) | Clarify shared query ownership between direct and indexed search | Keep FTS versus literal matching semantics, same-representation filters, all sorts and pagination/count parity |
| Cache invalidation (C6) | Define a version/dependency policy before narrowing whole-file implementation signatures | Rule changes must invalidate; preserve stale-query refusal, raw/policy/content/metadata handling and failed-sync rollback |

Further reviews should retain the direct-search regression oracle and supported
migrations until replacements and consumer compatibility are established. Review
legacy summary/reason formats before removal, and make version-control ownership
of documented but ignored scripts explicit. Do not remove public APIs or runnable
wrappers solely because static imports are absent. Profile full-scan sync costs,
broad-query metadata I/O and trigram storage before adding performance complexity.

Duplicate review flags and reuse of identical AI evaluation inputs are possible
future studies, not authorization to merge identities. Repeated content or shared
ATS-internal IDs can represent legitimate separate advertisements. Require
verified vacancy evidence for merging and a complete model/prompt/profile/job
input contract before claiming evaluation-cache savings.

Private retention is explicit: historical backups and completed exploratory
artifacts were retired by owner decision. Only approved mapping provenance and
compact evidence for unresolved duplicate questions remain. These are not runtime
dependencies; future operations must establish their own recovery plan.
