# Agent Instructions

## General
- Read relevant project documentation before editing.
- Keep changes scoped to the requested issue.
- Do not modify unrelated files.
- Prefer simple designs over premature abstractions.
- Explain significant architectural decisions.

## Quality
- Add or update tests for behavioral changes.
- Run `ruff check .`
- Run `ruff format --check .`
- Run `pytest`
- Do not declare work complete if checks fail.

## Architecture
- External job sources only fetch and normalize job data.
- Persistence is separate from source acquisition.
- Jobs are stored before AI analysis.
- Raw source data and AI-generated interpretations remain separate.
- Resume generation must only use verified candidate experience.
- Application orchestration coordinates stages; interfaces do not own source,
  persistence, evaluation, or resume-generation implementations.
- Source identity, logical job identity, job lifecycle, and application progress
  are separate concepts.
- AI output is untrusted derived data and must be validated before storage or use.
- Preserve provenance between source inputs, interpretations, and generated
  artifacts without allowing derived data to overwrite verified facts.

## Security / Privacy
- Never commit API keys, secrets, `.env` files, databases, real resumes, or private candidate-profile data.
