"""Provider contributions and global outcomes for a shared persisted catalog."""

from collections import Counter, defaultdict

from job_matcher.catalog import IdentityDisposition
from job_matcher.filter_policy import FilterOutcome
from job_matcher.identity import content_fingerprint
from job_matcher.screening import (
    ScreeningOutcome,
    aggregate_screening,
    combine_decisions,
)


def catalog_report(repository, catalog, screened, *, source: str) -> dict:
    """Keep independent provider outcomes distinct from global logical outcomes."""
    all_jobs = repository.list_linked_jobs()
    active = catalog.active_linked_jobs
    active_keys = {j.normalized.key for j in active}
    filters = {f.source_key: f for f in screened.filter_decisions}
    screens = {s.source_key: s for s in screened.screening_decisions}
    effective = {key: combine_decisions(filters[key], s) for key, s in screens.items()}
    members = defaultdict(list)
    fingerprints = defaultdict(list)
    for linked in active:
        members[linked.link.logical_job_id].append(linked)
        fingerprints[content_fingerprint(linked.normalized)].append(linked)

    def contribution(jobs):
        current = [j for j in jobs if j.normalized.key in active_keys]
        keys = {j.normalized.key for j in current}
        ids = {j.link.logical_job_id for j in current}
        standalone = aggregate_screening(effective[k] for k in keys)
        return {
            "persisted_source_postings": len(jobs),
            "active_source_postings": len(current),
            "inactive_source_postings": len(jobs) - len(current),
            "active_logical_jobs_represented": len(ids),
            "filter_representations": Counter(filters[k].outcome.value for k in keys),
            "screening_representations_among_filter_eligible": Counter(
                screens[k].outcome.value
                for k in keys
                if filters[k].outcome is FilterOutcome.ELIGIBLE
            ),
            "independent_logical_outcomes": Counter(
                v.value for v in standalone.values()
            ),
            "global_logical_outcomes_of_represented_jobs": Counter(
                screened.logical_outcomes[i].value for i in ids
            ),
        }

    providers = sorted({j.normalized.key.source for j in all_jobs})
    contribution_by_source = {
        provider: contribution(
            [j for j in all_jobs if j.normalized.key.source == provider]
        )
        for provider in providers
    }
    source_jobs = [j for j in all_jobs if j.normalized.key.source == source]
    scopes = sorted({j.normalized.key.source_scope for j in source_jobs})
    resolutions = [
        r for r in catalog.identity_resolutions if r.link.source_key.source == source
    ]
    confirmed = {
        r.link.source_key
        for r in resolutions
        if any(
            e.disposition is IdentityDisposition.AUTOMATIC_LINK
            and e.matched_source_key.source != source
            for e in r.evidence
        )
    }
    candidate_evidence = {
        (r.link.source_key, e.matched_source_key, e.kind)
        for r in resolutions
        for e in r.evidence
        if e.matched_source_key.source != source
        and e.disposition is IdentityDisposition.DUPLICATE_CANDIDATE
        and e.matched_logical_job_id != r.link.logical_job_id
    }
    shared = {
        i
        for i, jobs in members.items()
        if source in {j.normalized.key.source for j in jobs}
        and len({j.normalized.key.source for j in jobs}) > 1
    }
    candidate_groups = [
        jobs
        for jobs in fingerprints.values()
        if source in {j.normalized.key.source for j in jobs}
        and len({j.normalized.key.source for j in jobs}) > 1
        and len({j.link.logical_job_id for j in jobs}) > 1
    ]
    without_source = aggregate_screening(
        s for key, s in effective.items() if key.source != source
    )
    ready_without = {i for i, v in without_source.items() if v is ScreeningOutcome.KEEP}
    ready_global = {
        i for i, v in screened.logical_outcomes.items() if v is ScreeningOutcome.KEEP
    }
    return {
        "global": {
            "persisted_source_postings": len(all_jobs),
            "active_source_postings": len(active),
            "active_unique_logical_jobs": len(members),
            "filter_eligible_logical_jobs": len(
                {
                    f.logical_job_id
                    for f in screened.filter_decisions
                    if f.outcome is FilterOutcome.ELIGIBLE
                }
            ),
            "logical_outcomes": Counter(
                v.value for v in screened.logical_outcomes.values()
            ),
        },
        "by_source": contribution_by_source,
        "selected_source": source,
        "selected_source_scopes": {
            scope: contribution(
                [j for j in source_jobs if j.normalized.key.source_scope == scope]
            )
            for scope in scopes
        },
        "selected_source_identity": {
            "resolved_source_postings_this_run": len(resolutions),
            "new_unique_logical_jobs_this_run": sum(
                r.created_logical_job for r in resolutions
            ),
            "confirmed_cross_source_links_this_run": len(confirmed),
            "shared_active_logical_jobs_with_other_sources": len(shared),
            "exclusive_active_logical_jobs": sum(
                source == next(iter({j.normalized.key.source for j in jobs}))
                and len({j.normalized.key.source for j in jobs}) == 1
                for jobs in members.values()
            ),
            "possible_cross_source_candidate_evidence_this_run": len(
                candidate_evidence
            ),
            "possible_cross_source_exact_content_groups": len(candidate_groups),
            "candidate_examples": [
                [
                    {
                        "source": j.normalized.key.source,
                        "scope": j.normalized.key.source_scope,
                        "source_job_id": j.normalized.key.source_job_id,
                        "logical_job_id": str(j.link.logical_job_id.value),
                        "title": j.normalized.title,
                    }
                    for j in jobs
                ]
                for jobs in candidate_groups[:10]
            ],
            "additional_ready_logical_jobs_vs_catalog_without_source": len(
                ready_global - ready_without
            ),
        },
    }
