"""Acquire Lever, then Greenhouse, and process one shared catalog from an IDE."""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from job_matcher.catalog import LinkedSourceJob, LogicalJobId
from job_matcher.filter_policy import (
    FilterDecision,
    FilterOutcome,
    FilterPolicyError,
    FilterReason,
    load_filter_policy,
)
from job_matcher.filtering import DeterministicFilterEvaluator
from job_matcher.models import SourceJobKey
from job_matcher.ports import JobSource, PersistenceError
from job_matcher.screening_pipeline import screen_catalog
from job_matcher.screening_sqlite import read_active_screening_inputs
from job_matcher.sources.greenhouse import GreenhouseJobSource
from job_matcher.sources.lever import DEFAULT_API_BASE_URL, LeverJobSource
from job_matcher.sqlite_repository import SQLiteJobRepository
from job_matcher.validation import (
    RegistryCompany as LeverCompany,
)
from job_matcher.validation import (
    RegistryIssue as RegistryIssue,
)
from job_matcher.validation import (
    RegistryLoadResult,
    ValidationConfigurationError,
    ValidationRunResult,
    WriteLine,
    run_combined_validation,
)
from job_matcher.validation import (
    load_verified_companies as load_verified_companies,
)
from job_matcher.validation import (
    run_validation as run_validation,
)
from job_matcher.validation import (
    select_companies as select_companies,
)
from job_matcher.validation_reporting import catalog_report

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Lever limit; None retains the existing full-Lever selection.
VERIFIED_LEVER_REGISTRY_PATH = PROJECT_ROOT / "job_sources" / "lever_verified.json"
VERIFIED_GREENHOUSE_REGISTRY_PATH = (
    PROJECT_ROOT / "job_sources" / "greenhouse_verified.json"
)
DATABASE_PATH = PROJECT_ROOT / "phase2_validation.sqlite3"
FILTER_POLICY_PATH = PROJECT_ROOT / "examples" / "filter_policy.example.toml"
COMPANY_LIMIT: int | None = 1
# None processes every valid Greenhouse registry entry, after the Lever batch.
GREENHOUSE_COMPANY_LIMIT: int | None = None
DELAY_SECONDS = 0.8
TIMEOUT_SECONDS = 20.0
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 1.0
MAX_RETRY_DELAY_SECONDS = 5.0
PAGE_SIZE = 100
SAMPLE_LIMIT = 50
TOP_REJECTION_REASON_LIMIT = 50
API_BASE_URL = DEFAULT_API_BASE_URL


def print_summary(
    registry: RegistryLoadResult,
    result: ValidationRunResult,
    *,
    sample_limit: int,
    top_reason_limit: int,
    write_line: WriteLine = print,
) -> None:
    """Print bounded samples and reliable run and catalog metrics."""
    _require_non_negative_integer(sample_limit, "SAMPLE_LIMIT")
    _require_non_negative_integer(top_reason_limit, "TOP_REJECTION_REASON_LIMIT")

    decisions = result.catalog.filter_decisions
    active_jobs = result.catalog.active_linked_jobs
    active_logical_ids = {decision.logical_job_id for decision in decisions}
    eligible_logical_ids = set(result.catalog.eligible_logical_job_ids)
    rejected_logical_ids = active_logical_ids - eligible_logical_ids
    rejected_decisions = tuple(
        decision for decision in decisions if decision.outcome is FilterOutcome.REJECTED
    )
    eligible_representation_count = sum(
        decision.outcome is FilterOutcome.ELIGIBLE for decision in decisions
    )
    all_logical_ids = {linked.link.logical_job_id for linked in result.all_linked_jobs}
    inactive_linked_count = len(result.all_linked_jobs) - len(active_jobs)

    write_line("")
    write_line("Phase 0-2 validation summary")
    write_line(f"  Verified registry entries loaded: {registry.total_entries}")
    write_line(f"  Valid registry entries: {len(registry.companies)}")
    write_line(f"  Invalid registry entries skipped: {len(registry.issues)}")
    write_line("Current-run acquisition counts")
    write_line(f"  Companies attempted: {result.attempted_count}")
    write_line(f"  Companies successfully fetched: {result.successful_count}")
    write_line(f"  Companies failed: {result.failed_count}")
    write_line(
        f"  Source postings fetched this run: {result.total_source_postings_fetched}"
    )
    write_line("")
    write_line("Entire-database catalog counts (including previous runs)")
    write_line(
        "  Failed or unattempted scopes can retain previous active postings; "
        "only successful snapshots reconcile their scope."
    )
    write_line(
        "  Filtering, rejection reasons, and job samples use the entire active catalog."
    )
    write_line(f"  Active linked source postings: {len(active_jobs)}")
    write_line(f"  Inactive linked source postings: {inactive_linked_count}")
    write_line(f"  Persisted unique logical jobs: {len(all_logical_ids)}")
    write_line(f"  Active unique logical jobs: {len(active_logical_ids)}")
    write_line(f"  Newly resolved source postings: {result.catalog.resolved_count}")
    write_line(
        "  Newly created logical jobs: "
        f"{sum(item.created_logical_job for item in result.catalog.identity_resolutions)}"
    )
    write_line(f"  Source postings evaluated: {result.catalog.evaluated_count}")
    write_line(f"  Eligible source representations: {eligible_representation_count}")
    write_line(f"  Rejected source representations: {len(rejected_decisions)}")
    write_line(f"  Eligible logical jobs: {len(eligible_logical_ids)}")
    write_line(f"  Rejected logical jobs: {len(rejected_logical_ids)}")
    write_line(
        "  Eligible logical-job percentage: "
        f"{_percentage(len(eligible_logical_ids), len(active_logical_ids)):.1f}%"
    )
    write_line(
        "  Rejected logical-job percentage: "
        f"{_percentage(len(rejected_logical_ids), len(active_logical_ids)):.1f}%"
    )

    if registry.issues:
        write_line("")
        write_line("Invalid registry entries")
        for issue in registry.issues:
            label = issue.site or f"entry {issue.index}"
            write_line(f"  - {label}: {issue.message}")

    if result.failures:
        write_line("")
        write_line("Acquisition failures")
        for failure in result.failures:
            write_line(f"  - {failure.company.site}: {failure.message}")

    reason_counts = Counter(
        _reason_label(reason)
        for decision in rejected_decisions
        for reason in decision.reasons
    )
    if reason_counts and top_reason_limit:
        write_line("")
        write_line(f"Top {top_reason_limit} rejection reasons")
        for reason, count in reason_counts.most_common(top_reason_limit):
            write_line(f"  - {reason}: {count}")

    active_by_key = {job.normalized.key: job for job in active_jobs}
    _print_eligible_sample(
        decisions,
        active_by_key,
        sample_limit,
        write_line,
    )
    _print_rejected_sample(
        decisions,
        active_by_key,
        rejected_logical_ids,
        sample_limit,
        write_line,
    )


def _print_eligible_sample(
    decisions: Sequence[FilterDecision],
    active_by_key: dict[SourceJobKey, LinkedSourceJob],
    limit: int,
    write_line: WriteLine,
) -> None:
    if not limit:
        return
    sample: list[LinkedSourceJob] = []
    seen: set[LogicalJobId] = set()
    for decision in decisions:
        if (
            decision.outcome is FilterOutcome.ELIGIBLE
            and decision.logical_job_id not in seen
        ):
            sample.append(active_by_key[decision.source_key])
            seen.add(decision.logical_job_id)
    if not sample:
        return
    sample = random.sample(sample, min(limit, len(sample)))
    write_line("")
    write_line(f"Eligible job sample (up to {limit})")
    for linked in sample:
        write_line(f"  - {_job_label(linked)}")


def _print_rejected_sample(
    decisions: Sequence[FilterDecision],
    active_by_key: dict[SourceJobKey, LinkedSourceJob],
    rejected_logical_ids: set[LogicalJobId],
    limit: int,
    write_line: WriteLine,
) -> None:
    if not limit:
        return
    sample: list[tuple[LinkedSourceJob, FilterDecision]] = []
    seen: set[LogicalJobId] = set()
    for decision in decisions:
        if (
            decision.logical_job_id in rejected_logical_ids
            and decision.logical_job_id not in seen
        ):
            sample.append((active_by_key[decision.source_key], decision))
            seen.add(decision.logical_job_id)
    if not sample:
        return
    sample = random.sample(sample, min(limit, len(sample)))
    write_line("")
    write_line(f"Rejected job sample (up to {limit})")
    for linked, decision in sample:
        reasons = ", ".join(_reason_label(reason) for reason in decision.reasons)
        write_line(f"  - {_job_label(linked)} - {reasons}")


def _job_label(linked: LinkedSourceJob) -> str:
    normalized = linked.normalized
    title = " ".join(normalized.title.split())
    company = " ".join(normalized.company.split())
    return (
        f"{company} - {title} [{normalized.key.source}/{normalized.key.source_scope}]"
    )


def _reason_label(reason: FilterReason) -> str:
    label = f"{reason.field.value}/{reason.kind.value}"
    if reason.matched_term is not None:
        return f"{label} ({reason.matched_term!r})"
    return label


def _percentage(count: int, total: int) -> float:
    return 0.0 if total == 0 else count / total * 100


def _require_non_negative_integer(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValidationConfigurationError(f"{name} must be a non-negative integer")


def _source_factory(company: LeverCompany) -> JobSource:
    try:
        return LeverJobSource(
            site=company.site,
            company=company.company,
            api_base_url=API_BASE_URL,
            timeout_seconds=TIMEOUT_SECONDS,
            page_size=PAGE_SIZE,
            max_attempts=MAX_ATTEMPTS,
            backoff_seconds=BACKOFF_SECONDS,
            max_retry_delay_seconds=MAX_RETRY_DELAY_SECONDS,
        )
    except ValueError as error:
        raise ValidationConfigurationError(
            f"invalid Lever source configuration for {company.site!r}: {error}"
        ) from error


def main() -> int:
    """Run the configured real-data validation and return a process exit code."""
    try:
        registry = load_verified_companies(VERIFIED_LEVER_REGISTRY_PATH)
        selected = select_companies(registry.companies, COMPANY_LIMIT)
        greenhouse_registry = load_verified_companies(VERIFIED_GREENHOUSE_REGISTRY_PATH)
        greenhouse_selected = select_companies(
            greenhouse_registry.companies, GREENHOUSE_COMPANY_LIMIT
        )
        try:
            policy = load_filter_policy(FILTER_POLICY_PATH)
        except OSError as error:
            raise ValidationConfigurationError(
                f"could not read filter policy: {FILTER_POLICY_PATH}"
            ) from error

        database_existed = DATABASE_PATH.exists()
        repository = SQLiteJobRepository(DATABASE_PATH)
        repository.initialize_schema()
        print(
            f"Loaded Lever: {registry.total_entries} registry entries; "
            f"{len(registry.companies)} valid; {len(registry.issues)} invalid."
        )
        print(
            f"Loaded Greenhouse: {greenhouse_registry.total_entries} registry entries; "
            f"{len(greenhouse_registry.companies)} valid; {len(greenhouse_registry.issues)} invalid."
        )
        print(
            f"Attempting {len(selected)} Lever and {len(greenhouse_selected)} Greenhouse boards with a "
            f"{DELAY_SECONDS:.1f}s inter-company delay."
        )
        print(f"Validation database: {DATABASE_PATH}")
        print(f"Database existed before run: {'yes' if database_existed else 'no'}")
        print(f"Filter policy: {FILTER_POLICY_PATH}")
        evaluator = DeterministicFilterEvaluator(policy)
        print(
            f"Acquisition order: Lever ({len(selected)} boards), then Greenhouse ({len(greenhouse_selected)} boards)"
        )
        result = run_combined_validation(
            selected,
            greenhouse_selected,
            repository,
            evaluator,
            lever_source_factory=_source_factory,
            greenhouse_source_factory=_greenhouse_source_factory,
            delay_seconds=DELAY_SECONDS,
        )
        print_summary(
            RegistryLoadResult(
                registry.total_entries + greenhouse_registry.total_entries,
                registry.companies + greenhouse_registry.companies,
                registry.issues + greenhouse_registry.issues,
            ),
            result.combined,
            sample_limit=SAMPLE_LIMIT,
            top_reason_limit=TOP_REJECTION_REASON_LIMIT,
        )
        screened = screen_catalog(
            read_active_screening_inputs(DATABASE_PATH), evaluator
        )
        report = catalog_report(
            repository, result.combined.catalog, screened, source="greenhouse"
        )
        report["acquisition"] = {
            source: {
                "attempted": batch.attempted_count,
                "successful": batch.successful_count,
                "failed": batch.failed_count,
                "postings_fetched": batch.total_source_postings_fetched,
                "failures": [
                    {"scope": f.company.site, "error": f.message}
                    for f in batch.failures
                ],
            }
            for source, batch in (
                ("lever", result.lever),
                ("greenhouse", result.greenhouse),
            )
        }
        print(json.dumps(report, indent=2))
        return 1 if result.combined.failures else 0
    except (ValidationConfigurationError, FilterPolicyError, PersistenceError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _greenhouse_source_factory(company: LeverCompany) -> JobSource:
    try:
        return GreenhouseJobSource(
            site=company.site,
            company=company.company,
            timeout_seconds=TIMEOUT_SECONDS,
            max_attempts=MAX_ATTEMPTS,
            backoff_seconds=BACKOFF_SECONDS,
            max_retry_delay_seconds=MAX_RETRY_DELAY_SECONDS,
        )
    except ValueError as error:
        raise ValidationConfigurationError(
            f"invalid Greenhouse source configuration for {company.site!r}: {error}"
        ) from error


if __name__ == "__main__":
    raise SystemExit(main())
