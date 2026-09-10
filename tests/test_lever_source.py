"""Tests for the Lever public Postings API adapter."""

import json
from datetime import UTC, datetime, timedelta, timezone
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from job_matcher.ports import SourceAcquisitionError
from job_matcher.sources import lever
from job_matcher.sources.lever import LeverJobSource

RETRIEVED_AT = datetime(2026, 9, 8, 12, 30, tzinfo=UTC)


def posting(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": "posting-123",
        "text": "Software Engineer",
        "categories": {"location": "Remote", "team": "Engineering"},
        "descriptionPlain": "Build reliable systems.",
        "lists": [
            {
                "text": "Requirements",
                "content": "<ul><li>Python &amp; SQL</li><li>API design</li></ul>",
            }
        ],
        "additionalPlain": "Candidates in all time zones are welcome.",
        "hostedUrl": "https://jobs.lever.co/example/posting-123",
        "applyUrl": "https://jobs.lever.co/example/posting-123/apply",
        "workplaceType": "remote",
    }
    values.update(changes)
    return values


def install_pages(
    monkeypatch: pytest.MonkeyPatch, pages: list[list[dict[str, Any]]]
) -> list[tuple[str, float]]:
    requests: list[tuple[str, float]] = []

    def fake_request_json(url: str, *, timeout_seconds: float) -> Any:
        requests.append((url, timeout_seconds))
        return pages[len(requests) - 1]

    monkeypatch.setattr(lever, "_request_json", fake_request_json)
    return requests


def test_fetch_snapshot_normalizes_complete_posting_and_preserves_raw_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_posting = posting()
    requests = install_pages(monkeypatch, [[raw_posting]])
    source = LeverJobSource(
        "example",
        "Example Company",
        timeout_seconds=7.5,
        clock=lambda: RETRIEVED_AT,
    )

    snapshot = source.fetch_snapshot()
    jobs = snapshot.jobs

    assert snapshot.source == "lever"
    assert snapshot.source_scope == "example"
    assert snapshot.retrieved_at is RETRIEVED_AT
    assert len(jobs) == 1
    job = jobs[0]
    assert job.normalized.key.source == "lever"
    assert job.normalized.key.source_scope == "example"
    assert job.normalized.key.source_job_id == "posting-123"
    assert job.normalized.company == "Example Company"
    assert job.normalized.title == "Software Engineer"
    assert job.normalized.location == "Remote"
    assert job.normalized.job_url == raw_posting["hostedUrl"]
    assert job.normalized.apply_url == raw_posting["applyUrl"]
    assert job.normalized.posted_at is None
    assert job.normalized.retrieved_at is RETRIEVED_AT
    assert job.raw.retrieved_at is RETRIEVED_AT
    assert json.loads(job.raw.payload_json) == raw_posting
    assert job.normalized.description == (
        "Build reliable systems.\n\n"
        "Requirements\n"
        "- Python & SQL\n"
        "- API design\n\n"
        "Candidates in all time zones are welcome."
    )

    request_url, timeout = requests[0]
    parsed = urlsplit(request_url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "api.lever.co"
    assert parsed.path == "/v0/postings/example"
    assert parse_qs(parsed.query) == {
        "mode": ["json"],
        "skip": ["0"],
        "limit": ["100"],
    }
    assert timeout == 7.5


def test_fetch_snapshot_uses_one_retrieval_time_for_every_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        [posting(id="posting-1"), posting(id="posting-2")],
        [posting(id="posting-3")],
    ]
    requests = install_pages(monkeypatch, pages)
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return RETRIEVED_AT

    snapshot = LeverJobSource(
        "example", "Example Company", page_size=2, clock=clock
    ).fetch_snapshot()
    jobs = snapshot.jobs

    assert [job.normalized.key.source_job_id for job in jobs] == [
        "posting-1",
        "posting-2",
        "posting-3",
    ]
    assert all(job.normalized.retrieved_at is RETRIEVED_AT for job in jobs)
    assert clock_calls == 1
    assert [parse_qs(urlsplit(url).query)["skip"] for url, _ in requests] == [
        ["0"],
        ["2"],
    ]


def test_exact_page_multiple_makes_final_empty_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = install_pages(
        monkeypatch,
        [[posting(id="posting-1"), posting(id="posting-2")], []],
    )

    jobs = (
        LeverJobSource(
            "example", "Example Company", page_size=2, clock=lambda: RETRIEVED_AT
        )
        .fetch_snapshot()
        .jobs
    )

    assert len(jobs) == 2
    assert len(requests) == 2


def test_optional_location_and_apply_url_can_be_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_pages(
        monkeypatch,
        [[posting(categories=None, applyUrl=None, lists=None, additionalPlain="")]],
    )

    job = (
        LeverJobSource("example", "Example Company", clock=lambda: RETRIEVED_AT)
        .fetch_snapshot()
        .jobs[0]
    )

    assert job.normalized.location is None
    assert job.normalized.apply_url is None
    assert job.normalized.description == "Build reliable systems."


def test_empty_postings_response_returns_successful_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_pages(monkeypatch, [[]])

    snapshot = LeverJobSource(
        "example", "Example Company", clock=lambda: RETRIEVED_AT
    ).fetch_snapshot()

    assert snapshot.jobs == ()
    assert snapshot.source == "lever"
    assert snapshot.source_scope == "example"
    assert snapshot.retrieved_at is RETRIEVED_AT


@pytest.mark.parametrize(
    "pages",
    [
        [[posting(), posting()]],
        [[posting(id="posting-1")], [posting(id="posting-1")]],
    ],
)
def test_duplicate_posting_id_fails_acquisition(
    monkeypatch: pytest.MonkeyPatch, pages: list[list[dict[str, Any]]]
) -> None:
    install_pages(monkeypatch, pages)
    page_size = len(pages[0])
    source = LeverJobSource(
        "example",
        "Example Company",
        page_size=page_size,
        clock=lambda: RETRIEVED_AT,
    )

    with pytest.raises(SourceAcquisitionError, match="duplicate posting id"):
        source.fetch_snapshot()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [None],
        [posting(id="")],
        [posting(text=None)],
        [posting(descriptionPlain="", lists=[], additionalPlain="")],
        [posting(categories="Remote")],
        [posting(hostedUrl="not-a-url")],
    ],
)
def test_malformed_postings_fail_acquisition(
    monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    monkeypatch.setattr(lever, "_request_json", lambda *args, **kwargs: payload)
    source = LeverJobSource("example", "Example Company", clock=lambda: RETRIEVED_AT)

    with pytest.raises(SourceAcquisitionError):
        source.fetch_snapshot()


def test_non_utc_clock_fails_even_for_an_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_pages(monkeypatch, [[]])
    non_utc = datetime(2026, 9, 8, 5, 30, tzinfo=timezone(timedelta(hours=-7)))
    source = LeverJobSource("example", "Example Company", clock=lambda: non_utc)

    with pytest.raises(SourceAcquisitionError, match="datetime.UTC"):
        source.fetch_snapshot()


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_request_json_sends_json_headers_and_decodes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, *, timeout: float) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(b'[{"id":"posting-123"}]')

    monkeypatch.setattr(lever.urllib_request, "urlopen", fake_urlopen)

    result = lever._request_json("https://api.example/jobs", timeout_seconds=4.0)

    assert result == [{"id": "posting-123"}]
    assert captured["request"].get_header("Accept") == "application/json"
    assert captured["request"].get_method() == "GET"
    assert captured["timeout"] == 4.0


@pytest.mark.parametrize(
    "error",
    [
        HTTPError("https://api.example", 503, "unavailable", Message(), None),
        URLError("connection refused"),
        TimeoutError(),
    ],
)
def test_request_failures_are_source_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(lever.urllib_request, "urlopen", fail)

    with pytest.raises(SourceAcquisitionError, match="Lever request failed"):
        lever._request_json("https://api.example/jobs", timeout_seconds=4.0)


def test_invalid_json_response_is_a_source_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lever.urllib_request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"no"),
    )

    with pytest.raises(SourceAcquisitionError, match="not valid JSON"):
        lever._request_json("https://api.example/jobs", timeout_seconds=4.0)
