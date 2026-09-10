"""Tests for conservative source-independent identity resolution."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from job_matcher.catalog import (
    IdentityDisposition,
    IdentityEvidenceKind,
    LinkedSourceJob,
    LogicalJobId,
    SourceJobLink,
)
from job_matcher.identity import (
    IdentityResolver,
    canonicalize_identity_url,
    content_fingerprint,
)
from job_matcher.models import NormalizedJob, SourceJobKey

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
UUIDS = (
    UUID("00000000-0000-4000-8000-000000000001"),
    UUID("00000000-0000-4000-8000-000000000002"),
    UUID("00000000-0000-4000-8000-000000000003"),
)


def make_job(
    source_job_id: str,
    *,
    source: str = "lever",
    company: str = "Example Company",
    title: str = "Software Engineer",
    location: str | None = "Remote",
    description: str = "Build reliable software.",
    job_url: str | None = None,
    apply_url: str | None = None,
) -> NormalizedJob:
    return NormalizedJob(
        key=SourceJobKey(source, "example", source_job_id),
        company=company,
        title=title,
        location=location,
        description=description,
        job_url=job_url or f"https://jobs.example/{source_job_id}",
        apply_url=apply_url,
        retrieved_at=NOW,
    )


def linked_job(job: NormalizedJob, logical_uuid: UUID) -> LinkedSourceJob:
    return LinkedSourceJob(
        normalized=job,
        link=SourceJobLink(job.key, LogicalJobId(logical_uuid), NOW),
    )


class RecordingIdentityRepository:
    def __init__(
        self,
        unlinked: tuple[NormalizedJob, ...] = (),
        linked: tuple[LinkedSourceJob, ...] = (),
    ) -> None:
        self.unlinked = unlinked
        self.linked = list(linked)
        self.assignments: list[SourceJobLink] = []

    def list_unlinked_jobs(self) -> tuple[NormalizedJob, ...]:
        return self.unlinked

    def list_linked_jobs(self) -> tuple[LinkedSourceJob, ...]:
        return tuple(self.linked)

    def assign_logical_job(
        self,
        source_key: SourceJobKey,
        proposed_logical_job_id: LogicalJobId,
        linked_at: datetime,
    ) -> SourceJobLink:
        existing = next(
            (item.link for item in self.linked if item.link.source_key == source_key),
            None,
        )
        if existing is not None:
            return existing
        link = SourceJobLink(source_key, proposed_logical_job_id, linked_at)
        self.assignments.append(link)
        return link

    def get_link(self, source_key: SourceJobKey) -> SourceJobLink | None:
        return next(
            (item.link for item in self.linked if item.link.source_key == source_key),
            None,
        )

    def get_logical_job_activity(self, logical_job_id: LogicalJobId) -> bool | None:
        del logical_job_id
        return None


def resolver(
    repository: RecordingIdentityRepository,
    uuids: tuple[UUID, ...] = UUIDS,
) -> IdentityResolver:
    values = iter(uuids)
    return IdentityResolver(
        repository,
        uuid_factory=lambda: next(values),
        clock=lambda: NOW,
    )


def test_canonical_url_normalizes_only_conservative_components() -> None:
    assert (
        canonicalize_identity_url("HTTPS://Jobs.Example.COM:443/path/?b=2&a=1#details")
        == "https://jobs.example.com/path/?b=2&a=1"
    )
    assert canonicalize_identity_url("http://jobs.example.com:80/path") == (
        "http://jobs.example.com/path"
    )


def test_canonical_url_preserves_meaningful_differences() -> None:
    values = {
        canonicalize_identity_url("http://jobs.example/path"),
        canonicalize_identity_url("https://jobs.example/path"),
        canonicalize_identity_url("https://jobs.example/path/"),
        canonicalize_identity_url("https://jobs.example/path?a=1&b=2"),
        canonicalize_identity_url("https://jobs.example/path?b=2&a=1"),
    }

    assert len(values) == 5


def test_fingerprint_normalizes_case_and_whitespace() -> None:
    first = make_job("one")
    equivalent = make_job(
        "two",
        company="  EXAMPLE   COMPANY ",
        title="software\nengineer",
        location=" remote ",
        description="BUILD reliable\tsoftware.",
    )

    assert content_fingerprint(first) == content_fingerprint(equivalent)
    assert content_fingerprint(first) != content_fingerprint(
        make_job("three", location=None)
    )


def test_no_match_creates_new_logical_job() -> None:
    job = make_job("new")
    repository = RecordingIdentityRepository((job,))

    result = resolver(repository).resolve_unlinked()[0]

    assert result.link.logical_job_id == LogicalJobId(UUIDS[0])
    assert result.created_logical_job is True
    assert result.evidence == ()


def test_exact_job_url_and_company_link_despite_title_and_location_changes() -> None:
    existing = make_job(
        "existing",
        job_url="https://JOBS.example:443/opportunity#top",
    )
    incoming = make_job(
        "incoming",
        source="greenhouse",
        company=" example   COMPANY ",
        title="Senior Backend Engineer",
        location="California",
        job_url="https://jobs.example/opportunity",
    )
    repository = RecordingIdentityRepository(
        (incoming,), (linked_job(existing, UUIDS[0]),)
    )

    result = resolver(repository, (UUIDS[1],)).resolve_unlinked()[0]

    assert result.link.logical_job_id == LogicalJobId(UUIDS[0])
    assert result.created_logical_job is False
    assert result.evidence[0].kind is IdentityEvidenceKind.EXACT_CANONICAL_JOB_URL
    assert result.evidence[0].disposition is IdentityDisposition.AUTOMATIC_LINK


def test_exact_apply_url_and_company_link_automatically() -> None:
    shared = "https://apply.example/opportunity"
    existing = make_job("existing", apply_url=shared)
    incoming = make_job(
        "incoming",
        source="ashby",
        title="Senior Engineer",
        location="California",
        apply_url=shared,
    )
    repository = RecordingIdentityRepository(
        (incoming,), (linked_job(existing, UUIDS[0]),)
    )

    result = resolver(repository).resolve_unlinked()[0]

    assert result.link.logical_job_id == LogicalJobId(UUIDS[0])
    assert any(
        item.kind is IdentityEvidenceKind.EXACT_CANONICAL_APPLY_URL
        and item.disposition is IdentityDisposition.AUTOMATIC_LINK
        for item in result.evidence
    )


def test_job_url_is_not_compared_with_apply_url() -> None:
    shared = "https://apply.example/opportunity"
    existing = make_job("existing", apply_url=shared)
    incoming = make_job(
        "incoming",
        source="ashby",
        description="Different description.",
        job_url=shared,
        apply_url="https://apply.example/different",
    )
    repository = RecordingIdentityRepository(
        (incoming,), (linked_job(existing, UUIDS[0]),)
    )

    result = resolver(repository, (UUIDS[1],)).resolve_unlinked()[0]

    assert result.created_logical_job is True
    assert result.evidence == ()


@pytest.mark.parametrize("source", ["lever", "ashby", "greenhouse"])
def test_resolver_is_source_independent(source: str) -> None:
    job = make_job("new", source=source)
    repository = RecordingIdentityRepository((job,))

    result = resolver(repository).resolve_unlinked()[0]

    assert result.link.source_key.source == source


def test_company_mismatch_prevents_url_link() -> None:
    shared = "https://jobs.example/opportunity"
    existing = make_job("existing", company="First Company", job_url=shared)
    incoming = make_job(
        "incoming", source="ashby", company="Second Company", job_url=shared
    )
    repository = RecordingIdentityRepository(
        (incoming,), (linked_job(existing, UUIDS[0]),)
    )

    result = resolver(repository, (UUIDS[1],)).resolve_unlinked()[0]

    assert result.created_logical_job is True
    assert result.evidence == ()


def test_fingerprint_match_is_candidate_and_never_merges() -> None:
    existing = make_job("existing")
    incoming = make_job("incoming", source="greenhouse")
    repository = RecordingIdentityRepository(
        (incoming,), (linked_job(existing, UUIDS[0]),)
    )

    result = resolver(repository, (UUIDS[1],)).resolve_unlinked()[0]

    assert result.link.logical_job_id == LogicalJobId(UUIDS[1])
    assert result.created_logical_job is True
    assert len(result.evidence) == 1
    assert result.evidence[0].kind is IdentityEvidenceKind.EXACT_CONTENT_FINGERPRINT
    assert result.evidence[0].disposition is IdentityDisposition.DUPLICATE_CANDIDATE


def test_conflicting_url_matches_create_separate_logical_job() -> None:
    incoming = make_job(
        "incoming",
        source="greenhouse",
        job_url="https://jobs.example/shared",
        apply_url="https://apply.example/shared",
    )
    linked = (
        linked_job(
            make_job("job-match", job_url=incoming.job_url),
            UUIDS[0],
        ),
        linked_job(
            make_job("apply-match", apply_url=incoming.apply_url),
            UUIDS[1],
        ),
    )
    repository = RecordingIdentityRepository((incoming,), linked)

    result = resolver(repository, (UUIDS[2],)).resolve_unlinked()[0]

    assert result.link.logical_job_id == LogicalJobId(UUIDS[2])
    assert result.created_logical_job is True
    assert {item.matched_logical_job_id for item in result.evidence} >= {
        LogicalJobId(UUIDS[0]),
        LogicalJobId(UUIDS[1]),
    }
    assert all(
        item.disposition is IdentityDisposition.DUPLICATE_CANDIDATE
        for item in result.evidence
    )


def test_multiple_url_matches_to_one_logical_job_link_automatically() -> None:
    shared = "https://jobs.example/shared"
    incoming = make_job("incoming", source="greenhouse", job_url=shared)
    linked = (
        linked_job(make_job("one", job_url=shared), UUIDS[0]),
        linked_job(make_job("two", source="ashby", job_url=shared), UUIDS[0]),
    )

    result = resolver(
        RecordingIdentityRepository((incoming,), linked)
    ).resolve_unlinked()[0]

    assert result.link.logical_job_id == LogicalJobId(UUIDS[0])
    assert all(
        item.disposition is IdentityDisposition.AUTOMATIC_LINK
        for item in result.evidence
        if item.kind is IdentityEvidenceKind.EXACT_CANONICAL_JOB_URL
    )


def test_jobs_resolved_earlier_in_run_participate_in_later_matches() -> None:
    shared = "https://jobs.example/shared"
    first = make_job("one", source="ashby", job_url=shared)
    second = make_job("two", source="greenhouse", job_url=shared)
    repository = RecordingIdentityRepository((second, first))

    results = resolver(repository).resolve_unlinked()

    assert [result.link.source_key.source for result in results] == [
        "ashby",
        "greenhouse",
    ]
    assert results[0].link.logical_job_id == LogicalJobId(UUIDS[0])
    assert results[1].link.logical_job_id == LogicalJobId(UUIDS[0])


def test_clock_is_read_once_for_all_links() -> None:
    calls = 0
    repository = RecordingIdentityRepository((make_job("one"), make_job("two")))

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return NOW

    values = iter(UUIDS)
    results = IdentityResolver(
        repository,
        uuid_factory=lambda: next(values),
        clock=clock,
    ).resolve_unlinked()

    assert calls == 1
    assert all(result.link.linked_at is NOW for result in results)
