"""Application orchestration for the complete Phase 2 job pipeline."""

from dataclasses import dataclass

from job_matcher import acquisition
from job_matcher.catalog import IdentityResolution, LinkedSourceJob, LogicalJobId
from job_matcher.filter_policy import FilterDecision
from job_matcher.filtering import (
    DeterministicFilterEvaluator,
    eligible_logical_job_ids,
)
from job_matcher.identity import IdentityResolver
from job_matcher.ports import JobSource, PipelineRepository


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Transient results and explicit counts from one Phase 2 pipeline run."""

    acquired_count: int
    identity_resolutions: tuple[IdentityResolution, ...]
    filter_decisions: tuple[FilterDecision, ...]
    eligible_logical_job_ids: tuple[LogicalJobId, ...]

    @property
    def resolved_count(self) -> int:
        """Return source postings resolved during this run."""
        return len(self.identity_resolutions)

    @property
    def evaluated_count(self) -> int:
        """Return active catalog representations evaluated during this run."""
        return len(self.filter_decisions)

    @property
    def eligible_count(self) -> int:
        """Return unique logical jobs eligible after aggregation."""
        return len(self.eligible_logical_job_ids)


@dataclass(frozen=True, slots=True)
class CatalogProcessingResult:
    """Transient results from processing the current persisted catalog."""

    identity_resolutions: tuple[IdentityResolution, ...]
    active_linked_jobs: tuple[LinkedSourceJob, ...]
    filter_decisions: tuple[FilterDecision, ...]
    eligible_logical_job_ids: tuple[LogicalJobId, ...]

    @property
    def resolved_count(self) -> int:
        """Return source postings resolved during this processing pass."""
        return len(self.identity_resolutions)

    @property
    def evaluated_count(self) -> int:
        """Return active catalog representations evaluated during this pass."""
        return len(self.filter_decisions)

    @property
    def eligible_count(self) -> int:
        """Return unique logical jobs eligible after aggregation."""
        return len(self.eligible_logical_job_ids)


def process_persisted_catalog(
    repository: PipelineRepository,
    identity_resolver: IdentityResolver,
    filter_evaluator: DeterministicFilterEvaluator,
) -> CatalogProcessingResult:
    """Resolve identity and filter the current active persisted catalog."""
    identity_resolutions = identity_resolver.resolve_unlinked()
    active_linked_jobs = tuple(repository.list_active_linked_jobs())
    filter_decisions = filter_evaluator.evaluate(active_linked_jobs)
    return CatalogProcessingResult(
        identity_resolutions=identity_resolutions,
        active_linked_jobs=active_linked_jobs,
        filter_decisions=filter_decisions,
        eligible_logical_job_ids=eligible_logical_job_ids(filter_decisions),
    )


def run_job_pipeline(
    source: JobSource,
    repository: PipelineRepository,
    identity_resolver: IdentityResolver,
    filter_evaluator: DeterministicFilterEvaluator,
) -> PipelineResult:
    """Acquire, persist, resolve, and filter the current active catalog."""
    acquired_count = acquisition.acquire_and_persist(source, repository)
    catalog_result = process_persisted_catalog(
        repository,
        identity_resolver,
        filter_evaluator,
    )
    return PipelineResult(
        acquired_count=acquired_count,
        identity_resolutions=catalog_result.identity_resolutions,
        filter_decisions=catalog_result.filter_decisions,
        eligible_logical_job_ids=catalog_result.eligible_logical_job_ids,
    )
