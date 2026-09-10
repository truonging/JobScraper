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
