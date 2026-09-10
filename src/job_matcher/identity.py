"""Conservative source-independent logical identity resolution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from job_matcher.catalog import (
    IdentityDisposition,
    IdentityEvidence,
    IdentityEvidenceKind,
    IdentityResolution,
    LinkedSourceJob,
    LogicalJobId,
)
from job_matcher.models import ContractValidationError, NormalizedJob
from job_matcher.ports import IdentityRepository


def normalize_identity_text(value: str) -> str:
    """Normalize text for exact deterministic identity comparisons."""
    if not isinstance(value, str):
        raise ValueError("identity text must be a string")
    return " ".join(value.split()).casefold()


def canonicalize_identity_url(value: str) -> str:
    """Canonicalize a URL without provider-specific or lossy transformations."""
    if not isinstance(value, str):
        raise ValueError("identity URL must be a string")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("identity URL must be a valid HTTP or HTTPS URL") from error
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if scheme not in {"http", "https"} or hostname is None:
        raise ValueError("identity URL must be a valid HTTP or HTTPS URL")

    host = hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    user_info = ""
    if parsed.username is not None:
        user_info = parsed.username
        if parsed.password is not None:
            user_info += f":{parsed.password}"
        user_info += "@"
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    port_suffix = "" if port is None or default_port else f":{port}"
    netloc = f"{user_info}{host}{port_suffix}"
    return urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))


def content_fingerprint(job: NormalizedJob) -> str:
    """Return an exact normalized company/title/location/description digest."""
    if not isinstance(job, NormalizedJob):
        raise ValueError("job must be a NormalizedJob")
    fields = [
        normalize_identity_text(job.company),
        normalize_identity_text(job.title),
        None if job.location is None else normalize_identity_text(job.location),
        normalize_identity_text(job.description),
    ]
    encoded = json.dumps(
        fields,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class IdentityResolver:
    """Assign unlinked source postings using conservative deterministic evidence."""

    def __init__(
        self,
        repository: IdentityRepository,
        *,
        uuid_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._uuid_factory = uuid_factory
        self._clock = clock or _utc_now

    def resolve_unlinked(self) -> tuple[IdentityResolution, ...]:
        """Resolve every currently unlinked posting in deterministic key order."""
        linked_at = self._clock()
        if not isinstance(linked_at, datetime) or linked_at.tzinfo is not UTC:
            raise ContractValidationError(
                "identity resolution clock must return a datetime using datetime.UTC"
            )

        unlinked_jobs = sorted(
            self._repository.list_unlinked_jobs(), key=_normalized_job_sort_key
        )
        linked_jobs = sorted(
            self._repository.list_linked_jobs(), key=_linked_job_sort_key
        )
        resolutions: list[IdentityResolution] = []

        for job in unlinked_jobs:
            resolution = self._resolve_job(job, linked_jobs, linked_at)
            resolutions.append(resolution)
            linked_jobs.append(LinkedSourceJob(job, resolution.link))
            linked_jobs.sort(key=_linked_job_sort_key)

        return tuple(resolutions)

    def _resolve_job(
        self,
        job: NormalizedJob,
        linked_jobs: Sequence[LinkedSourceJob],
        linked_at: datetime,
    ) -> IdentityResolution:
        url_matches = _url_matches(job, linked_jobs)
        matched_logical_ids = {
            evidence.matched_logical_job_id for evidence in url_matches
        }
        automatically_linked = len(matched_logical_ids) == 1
        if automatically_linked:
            proposed_id = next(iter(matched_logical_ids))
            url_disposition = IdentityDisposition.AUTOMATIC_LINK
        else:
            proposed_id = LogicalJobId(self._uuid_factory())
            url_disposition = IdentityDisposition.DUPLICATE_CANDIDATE

        evidence = tuple(
            _with_disposition(item, url_disposition) for item in url_matches
        ) + _fingerprint_matches(job, linked_jobs)
        link = self._repository.assign_logical_job(job.key, proposed_id, linked_at)
        created_logical_job = (
            not automatically_linked and link.logical_job_id == proposed_id
        )
        return IdentityResolution(
            link=link,
            created_logical_job=created_logical_job,
            evidence=evidence,
        )


def _url_matches(
    job: NormalizedJob, linked_jobs: Sequence[LinkedSourceJob]
) -> tuple[IdentityEvidence, ...]:
    company = normalize_identity_text(job.company)
    canonical_job_url = canonicalize_identity_url(job.job_url)
    canonical_apply_url = (
        canonicalize_identity_url(job.apply_url) if job.apply_url is not None else None
    )
    matches: list[IdentityEvidence] = []
    for candidate in linked_jobs:
        if normalize_identity_text(candidate.normalized.company) != company:
            continue
        if canonicalize_identity_url(candidate.normalized.job_url) == canonical_job_url:
            matches.append(
                IdentityEvidence(
                    source_key=job.key,
                    matched_source_key=candidate.normalized.key,
                    matched_logical_job_id=candidate.link.logical_job_id,
                    kind=IdentityEvidenceKind.EXACT_CANONICAL_JOB_URL,
                    disposition=IdentityDisposition.DUPLICATE_CANDIDATE,
                    value=canonical_job_url,
                )
            )
        if (
            canonical_apply_url is not None
            and candidate.normalized.apply_url is not None
            and canonicalize_identity_url(candidate.normalized.apply_url)
            == canonical_apply_url
        ):
            matches.append(
                IdentityEvidence(
                    source_key=job.key,
                    matched_source_key=candidate.normalized.key,
                    matched_logical_job_id=candidate.link.logical_job_id,
                    kind=IdentityEvidenceKind.EXACT_CANONICAL_APPLY_URL,
                    disposition=IdentityDisposition.DUPLICATE_CANDIDATE,
                    value=canonical_apply_url,
                )
            )
    return tuple(matches)


def _fingerprint_matches(
    job: NormalizedJob, linked_jobs: Sequence[LinkedSourceJob]
) -> tuple[IdentityEvidence, ...]:
    fingerprint = content_fingerprint(job)
    return tuple(
        IdentityEvidence(
            source_key=job.key,
            matched_source_key=candidate.normalized.key,
            matched_logical_job_id=candidate.link.logical_job_id,
            kind=IdentityEvidenceKind.EXACT_CONTENT_FINGERPRINT,
            disposition=IdentityDisposition.DUPLICATE_CANDIDATE,
            value=fingerprint,
        )
        for candidate in linked_jobs
        if content_fingerprint(candidate.normalized) == fingerprint
    )


def _with_disposition(
    evidence: IdentityEvidence, disposition: IdentityDisposition
) -> IdentityEvidence:
    return IdentityEvidence(
        source_key=evidence.source_key,
        matched_source_key=evidence.matched_source_key,
        matched_logical_job_id=evidence.matched_logical_job_id,
        kind=evidence.kind,
        disposition=disposition,
        value=evidence.value,
    )


def _normalized_job_sort_key(job: NormalizedJob) -> tuple[str, str, str]:
    return (job.key.source, job.key.source_scope, job.key.source_job_id)


def _linked_job_sort_key(job: LinkedSourceJob) -> tuple[str, str, str]:
    return _normalized_job_sort_key(job.normalized)


def _utc_now() -> datetime:
    return datetime.now(UTC)
