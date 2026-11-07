"""Shared registry loading and batch orchestration for source validation."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from job_matcher import acquisition
from job_matcher.catalog import LinkedSourceJob
from job_matcher.filtering import DeterministicFilterEvaluator
from job_matcher.identity import IdentityResolver
from job_matcher.pipeline import CatalogProcessingResult, process_persisted_catalog
from job_matcher.ports import JobSource, SourceAcquisitionError
from job_matcher.sqlite_repository import SQLiteJobRepository

WriteLine = Callable[[str], None]
SourceFactory = Callable[["RegistryCompany"], JobSource]


class ValidationConfigurationError(ValueError):
    """Raised when local validation configuration cannot be used."""


@dataclass(frozen=True, slots=True)
class RegistryCompany:
    """One validated source scope from the local registry."""

    site: str
    company: str


@dataclass(frozen=True, slots=True)
class RegistryIssue:
    """One registry entry that cannot safely produce normalized source data."""

    index: int
    site: str | None
    message: str


@dataclass(frozen=True, slots=True)
class RegistryLoadResult:
    """Validated registry entries and nonfatal entry-level issues."""

    total_entries: int
    companies: tuple[RegistryCompany, ...]
    issues: tuple[RegistryIssue, ...]


@dataclass(frozen=True, slots=True)
class AcquisitionFailure:
    """A source scope that did not produce a successful snapshot."""

    company: RegistryCompany
    message: str


@dataclass(frozen=True, slots=True)
class ValidationRunResult:
    """Acquisition and catalog-processing results for one validation run."""

    attempted_count: int
    successful_count: int
    total_source_postings_fetched: int
    failures: tuple[AcquisitionFailure, ...]
    catalog: CatalogProcessingResult
    all_linked_jobs: tuple[LinkedSourceJob, ...]

    @property
    def failed_count(self) -> int:
        """Return source scopes that failed acquisition."""
        return len(self.failures)


@dataclass(frozen=True, slots=True)
class AcquisitionBatchResult:
    """Acquisition-only metrics, before shared catalog processing."""

    attempted_count: int
    successful_count: int
    total_source_postings_fetched: int
    failures: tuple[AcquisitionFailure, ...]

    @property
    def failed_count(self) -> int:
        return len(self.failures)


@dataclass(frozen=True, slots=True)
class CombinedValidationRunResult:
    lever: AcquisitionBatchResult
    greenhouse: AcquisitionBatchResult
    combined: ValidationRunResult


def load_verified_companies(path: str | Path) -> RegistryLoadResult:
    """Load valid unique source scopes while retaining entry-level issues."""
    registry_path = Path(path)
    try:
        document = json.loads(registry_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValidationConfigurationError(
            f"could not read verified source registry: {registry_path}"
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationConfigurationError(
            f"verified source registry must be valid UTF-8 JSON: {registry_path}"
        ) from error

    if not isinstance(document, list):
        raise ValidationConfigurationError(
            "verified source registry must contain a JSON array"
        )

    companies: list[RegistryCompany] = []
    issues: list[RegistryIssue] = []
    seen_sites: set[str] = set()
    for index, entry in enumerate(document):
        if not isinstance(entry, dict):
            issues.append(RegistryIssue(index, None, "entry must be a JSON object"))
            continue

        raw_site = entry.get("site")
        raw_company = entry.get("company")
        site = raw_site.strip() if isinstance(raw_site, str) else None
        company = raw_company.strip() if isinstance(raw_company, str) else None
        issue_site = site or None
        if not site:
            issues.append(RegistryIssue(index, issue_site, "site must be non-blank"))
            continue
        if not company:
            issues.append(RegistryIssue(index, issue_site, "company must be non-blank"))
            continue
        if site in seen_sites:
            issues.append(RegistryIssue(index, site, "site is duplicated"))
            continue

        seen_sites.add(site)
        companies.append(RegistryCompany(site=site, company=company))

    return RegistryLoadResult(
        total_entries=len(document),
        companies=tuple(companies),
        issues=tuple(issues),
    )


def select_companies(
    companies: Sequence[RegistryCompany], limit: int | None
) -> tuple[RegistryCompany, ...]:
    """Apply the configured development limit to validated companies."""
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValidationConfigurationError(
            "COMPANY_LIMIT must be None or a non-negative integer"
        )
    selected = companies if limit is None else companies[:limit]
    return tuple(selected)


def acquire_validation_batch(
    companies: Sequence[RegistryCompany],
    repository: SQLiteJobRepository,
    *,
    source_factory: SourceFactory,
    delay_seconds: float,
    sleeper: Callable[[float], None] = time.sleep,
    write_line: WriteLine = print,
) -> AcquisitionBatchResult:
    """Acquire and reconcile successful scopes without processing the catalog."""
    if (
        isinstance(delay_seconds, bool)
        or not isinstance(delay_seconds, int | float)
        or delay_seconds < 0
    ):
        raise ValidationConfigurationError(
            "DELAY_SECONDS must be a non-negative number"
        )

    selected = tuple(companies)
    successful_count = 0
    total_fetched = 0
    failures: list[AcquisitionFailure] = []
    for number, company in enumerate(selected, start=1):
        try:
            acquired_count = acquisition.acquire_and_persist(
                source_factory(company), repository
            )
        except SourceAcquisitionError as error:
            failures.append(AcquisitionFailure(company, str(error)))
            write_line(f"[{number}/{len(selected)}] {company.site}: FAILED - {error}")
        else:
            successful_count += 1
            total_fetched += acquired_count
            write_line(
                f"[{number}/{len(selected)}] {company.site}: "
                f"fetched {acquired_count} postings"
            )

        if number < len(selected) and delay_seconds:
            sleeper(float(delay_seconds))

    return AcquisitionBatchResult(
        len(selected), successful_count, total_fetched, tuple(failures)
    )


def _finish_validation(
    repository: SQLiteJobRepository,
    filter_evaluator: DeterministicFilterEvaluator,
    batches: Sequence[AcquisitionBatchResult],
) -> ValidationRunResult:
    catalog = process_persisted_catalog(
        repository,
        IdentityResolver(repository),
        filter_evaluator,
    )
    return ValidationRunResult(
        attempted_count=sum(b.attempted_count for b in batches),
        successful_count=sum(b.successful_count for b in batches),
        total_source_postings_fetched=sum(
            b.total_source_postings_fetched for b in batches
        ),
        failures=tuple(f for b in batches for f in b.failures),
        catalog=catalog,
        all_linked_jobs=tuple(repository.list_linked_jobs()),
    )


def run_validation(
    companies: Sequence[RegistryCompany],
    repository: SQLiteJobRepository,
    filter_evaluator: DeterministicFilterEvaluator,
    *,
    source_factory: SourceFactory,
    delay_seconds: float,
    sleeper: Callable[[float], None] = time.sleep,
    write_line: WriteLine = print,
) -> ValidationRunResult:
    """Acquire one provider batch, then process the complete catalog once."""
    batch = acquire_validation_batch(
        companies,
        repository,
        source_factory=source_factory,
        delay_seconds=delay_seconds,
        sleeper=sleeper,
        write_line=write_line,
    )
    return _finish_validation(repository, filter_evaluator, (batch,))


def run_combined_validation(
    lever_companies: Sequence[RegistryCompany],
    greenhouse_companies: Sequence[RegistryCompany],
    repository: SQLiteJobRepository,
    filter_evaluator: DeterministicFilterEvaluator,
    *,
    lever_source_factory: SourceFactory,
    greenhouse_source_factory: SourceFactory,
    delay_seconds: float,
    sleeper: Callable[[float], None] = time.sleep,
    write_line: WriteLine = print,
) -> CombinedValidationRunResult:
    """Persist Lever first, then Greenhouse; resolve/filter only after both."""
    lever = acquire_validation_batch(
        lever_companies,
        repository,
        source_factory=lever_source_factory,
        delay_seconds=delay_seconds,
        sleeper=sleeper,
        write_line=lambda line: write_line(f"Lever: {line}"),
    )
    greenhouse = acquire_validation_batch(
        greenhouse_companies,
        repository,
        source_factory=greenhouse_source_factory,
        delay_seconds=delay_seconds,
        sleeper=sleeper,
        write_line=lambda line: write_line(f"Greenhouse: {line}"),
    )
    return CombinedValidationRunResult(
        lever,
        greenhouse,
        _finish_validation(repository, filter_evaluator, (lever, greenhouse)),
    )
