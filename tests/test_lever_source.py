"""Tests for the Lever public Postings API adapter."""

import errno
import json
import socket
import ssl
from datetime import UTC, datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from job_matcher.acquisition import acquire_and_persist
from job_matcher.catalog import SourceJobStatus
from job_matcher.identity import IdentityResolver
from job_matcher.models import SourceJobKey, SourceSnapshot
from job_matcher.ports import SourceAcquisitionError
from job_matcher.sources import lever
from job_matcher.sources.lever import LeverJobSource
from job_matcher.sqlite_repository import SQLiteJobRepository

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


def test_description_body_is_used_when_legacy_description_fields_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_posting = posting(
        descriptionPlain="",
        lists=[],
        additionalPlain="",
        descriptionBody=(
            "<div>Maintain critical building systems.</div>"
            "<ul><li>Coordinate repairs</li><li>Inspect equipment</li></ul>"
        ),
        descriptionBodyPlain="",
    )
    install_pages(monkeypatch, [[raw_posting]])

    job = (
        LeverJobSource("example", "Example Company", clock=lambda: RETRIEVED_AT)
        .fetch_snapshot()
        .jobs[0]
    )

    assert job.normalized.description == (
        "Maintain critical building systems.\n"
        "\n"
        "- Coordinate repairs\n"
        "- Inspect equipment"
    )


def test_completely_blank_description_is_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_pages(
        monkeypatch,
        [
            [
                posting(
                    descriptionPlain="",
                    lists=[],
                    additionalPlain="",
                    descriptionBodyPlain="",
                    descriptionBody="",
                )
            ]
        ],
    )

    snapshot = LeverJobSource(
        "example", "Example Company", clock=lambda: RETRIEVED_AT
    ).fetch_snapshot()

    assert len(snapshot.jobs) == 1
    assert snapshot.jobs[0].normalized.description == ""


def test_mixed_snapshot_preserves_descriptionless_posting_lifecycle_and_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    normal = posting(id="normal")
    blank = posting(
        id="blank",
        hostedUrl="https://jobs.lever.co/example/blank",
        applyUrl=None,
        descriptionPlain="",
        lists=[],
        additionalPlain="",
        descriptionBodyPlain="",
        descriptionBody="",
    )
    install_pages(monkeypatch, [[normal, blank], [blank]])
    repository = SQLiteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize_schema()
    observed_at = RETRIEVED_AT
    source = LeverJobSource("example", "Example Company", clock=lambda: observed_at)

    assert acquire_and_persist(source, repository) == 2
    resolver = IdentityResolver(repository, clock=lambda: observed_at)
    resolutions = resolver.resolve_unlinked()
    assert len(resolutions) == 2
    links = {
        result.link.source_key.source_job_id: result.link for result in resolutions
    }
    assert links["blank"].logical_job_id != links["normal"].logical_job_id
    stored_blank = repository.get(links["blank"].source_key)
    stored_normal = repository.get(links["normal"].source_key)
    assert stored_blank is not None
    assert stored_blank.normalized.description == ""
    assert json.loads(stored_blank.raw.payload_json) == blank
    assert stored_normal is not None
    assert "Build reliable systems." in stored_normal.normalized.description

    observed_at += timedelta(hours=1)
    assert acquire_and_persist(source, repository) == 1
    assert resolver.resolve_unlinked() == ()
    assert repository.get_link(links["blank"].source_key) == links["blank"]
    lifecycle = repository.get_lifecycle(links["blank"].source_key)
    assert lifecycle is not None
    assert lifecycle.status is SourceJobStatus.ACTIVE
    assert lifecycle.first_seen_at == RETRIEVED_AT
    assert lifecycle.last_seen_at == observed_at
    assert lifecycle.inactive_at is None
    stored_blank = repository.get(links["blank"].source_key)
    assert stored_blank is not None
    assert stored_blank.normalized.description == ""
    assert stored_blank.normalized.retrieved_at == observed_at
    assert repository.get_logical_job_activity(links["blank"].logical_job_id) is True
    normal_lifecycle = repository.get_lifecycle(links["normal"].source_key)
    assert normal_lifecycle is not None
    assert normal_lifecycle.status is SourceJobStatus.INACTIVE
    assert normal_lifecycle.inactive_at == observed_at
    assert repository.get_logical_job_activity(links["normal"].logical_job_id) is False


def test_lists_and_additional_supply_description_when_description_plain_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_pages(
        monkeypatch,
        [
            [
                posting(
                    descriptionPlain="",
                    lists=[
                        {
                            "text": "Responsibilities",
                            "content": "<ul><li>Build services</li></ul>",
                        }
                    ],
                    additionalPlain="Work with a distributed team.",
                )
            ]
        ],
    )

    job = (
        LeverJobSource("example", "Example Company", clock=lambda: RETRIEVED_AT)
        .fetch_snapshot()
        .jobs[0]
    )

    assert job.normalized.description == (
        "Responsibilities\n- Build services\n\nWork with a distributed team."
    )


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


@pytest.mark.parametrize(
    ("raw_posting", "expected_message"),
    [
        (
            posting(id=""),
            "Lever posting at index 0 is invalid: id must be a non-blank string",
        ),
        (
            posting(categories="Remote"),
            (
                "Lever posting 'posting-123' is invalid: "
                "categories must be an object or null"
            ),
        ),
    ],
)
def test_malformed_posting_error_identifies_failed_invariant(
    monkeypatch: pytest.MonkeyPatch,
    raw_posting: dict[str, Any],
    expected_message: str,
) -> None:
    install_pages(monkeypatch, [[raw_posting]])

    with pytest.raises(SourceAcquisitionError, match=expected_message):
        LeverJobSource(
            "example", "Example Company", clock=lambda: RETRIEVED_AT
        ).fetch_snapshot()


@pytest.mark.parametrize("field", ["id", "text", "hostedUrl"])
def test_missing_required_field_still_fails_acquisition(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    raw_posting = posting()
    del raw_posting[field]
    install_pages(monkeypatch, [[raw_posting]])

    with pytest.raises(
        SourceAcquisitionError, match=f"{field} must be a non-blank string"
    ):
        LeverJobSource(
            "example", "Example Company", clock=lambda: RETRIEVED_AT
        ).fetch_snapshot()


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


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError(), "timeout (configured timeout=4s) (TimeoutError)"),
        (URLError(TimeoutError()), "timeout (configured timeout=4s) (TimeoutError)"),
        (URLError("timed out"), "timeout (configured timeout=4s) (URLError)"),
        (
            URLError(ConnectionRefusedError(10061, "private endpoint")),
            "connection error (ConnectionRefusedError, errno=10061)",
        ),
        (
            ConnectionResetError(10054, "private endpoint"),
            "connection error (ConnectionResetError, errno=10054)",
        ),
        (
            URLError(socket.gaierror(-2, "private host")),
            "DNS resolution error (gaierror, errno=-2)",
        ),
        (
            URLError(ssl.SSLError(1, "private certificate")),
            "TLS error (SSLError, errno=1)",
        ),
        (OSError(101, "private path"), "transport error (OSError, errno=101)"),
        (
            URLError(
                "connection refused https://user:secret@example/?token=secret\nINJECT"
            ),
            "connection refused (URLError)",
        ),
    ],
)
def test_transport_diagnostics_are_useful_and_sanitized(
    monkeypatch: pytest.MonkeyPatch, error: OSError, expected: str
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(lever.urllib_request, "urlopen", fail)

    with pytest.raises(SourceAcquisitionError) as caught:
        lever._request_json("https://api.example/jobs", timeout_seconds=4.0)

    assert str(caught.value) == f"Lever request failed: {expected}"
    assert caught.value.__cause__ is error


@pytest.mark.parametrize(
    "failure", ["404", "429", "503", "timeout", "connection", "json", "shape"]
)
def test_later_page_failure_preserves_previous_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    request_json = lever._request_json
    install_pages(monkeypatch, [[posting(id="previous")]])
    repository = SQLiteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize_schema()
    initial = LeverJobSource(
        "example", "Example Company", clock=lambda: RETRIEVED_AT
    ).fetch_snapshot()
    repository.reconcile_snapshot(initial)
    key = initial.jobs[0].normalized.key
    previous_lifecycle = repository.get_lifecycle(key)
    monkeypatch.setattr(lever, "_request_json", request_json)
    calls = 0

    def fake_urlopen(request: Any, *, timeout: float) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeResponse(json.dumps([posting(id="new")]).encode())
        assert parse_qs(urlsplit(request.full_url).query)["skip"] == ["1"]
        if failure in {"404", "429", "503"}:
            raise HTTPError(
                request.full_url, int(failure), "private response", Message(), None
            )
        if failure == "timeout":
            raise URLError(TimeoutError())
        if failure == "connection":
            raise URLError(ConnectionRefusedError(10061, "private host"))
        return FakeResponse(b"not JSON" if failure == "json" else b"{}")

    monkeypatch.setattr(lever.urllib_request, "urlopen", fake_urlopen)
    delays: list[float] = []
    source = LeverJobSource(
        "example",
        "Example Company",
        page_size=1,
        clock=lambda: RETRIEVED_AT + timedelta(hours=1),
        sleeper=delays.append,
    )

    with pytest.raises(SourceAcquisitionError) as caught:
        acquire_and_persist(source, repository)

    assert "Lever site 'example', skip=1, limit=1" in str(caught.value)
    expected_detail = {
        "404": "HTTP status 404",
        "429": "HTTP status 429",
        "503": "HTTP status 503",
        "timeout": "timeout (configured timeout=10s)",
        "connection": "connection error (ConnectionRefusedError, errno=10061)",
        "json": "not valid JSON",
        "shape": "must be a JSON array",
    }[failure]
    assert expected_detail in str(caught.value)
    assert "private" not in str(caught.value)
    transient = failure in {"429", "503", "timeout", "connection"}
    assert calls == (4 if transient else 2)
    assert delays == ([1.0, 2.0] if transient else [])
    if transient:
        assert "retries exhausted after 3 attempts" in str(caught.value)
    assert repository.get(key) == initial.jobs[0]
    assert repository.get_lifecycle(key) == previous_lifecycle
    assert repository.get(SourceJobKey("lever", "example", "new")) is None
    # An empty snapshot at the failed attempt's timestamp must still succeed:
    # a partial acquisition must not advance the source-scope checkpoint.
    repository.reconcile_snapshot(
        SourceSnapshot("lever", "example", RETRIEVED_AT + timedelta(hours=1), ())
    )
    lifecycle = repository.get_lifecycle(key)
    assert lifecycle is not None
    assert lifecycle.status is SourceJobStatus.INACTIVE


def http_error(status: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(
        "https://api.example/jobs", status, "private response", headers, None
    )


def install_http_outcomes(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[bytes | Exception]
) -> list[tuple[str, float]]:
    requests: list[tuple[str, float]] = []

    def fake_urlopen(request: Any, *, timeout: float) -> FakeResponse:
        outcome = outcomes[len(requests)]
        requests.append((request.full_url, timeout))
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    monkeypatch.setattr(lever.urllib_request, "urlopen", fake_urlopen)
    return requests


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        URLError(TimeoutError()),
        URLError("timed out"),
        URLError(ConnectionRefusedError(10061, "private")),
        ConnectionResetError(),
        ConnectionAbortedError(),
        BrokenPipeError(),
        OSError(errno.ENETUNREACH, "private"),
        URLError(socket.gaierror(socket.EAI_AGAIN, "private")),
        *[http_error(status) for status in (408, 429, 500, 502, 503, 504)],
    ],
)
def test_transient_page_failure_recovers_on_retry(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    requests = install_http_outcomes(monkeypatch, [error, b"[]"])
    delays: list[float] = []

    snapshot = LeverJobSource(
        "example",
        "Example Company",
        timeout_seconds=20,
        clock=lambda: RETRIEVED_AT,
        sleeper=delays.append,
    ).fetch_snapshot()

    assert snapshot.jobs == ()
    assert delays == [1.0]
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert requests[0][1] == 20


@pytest.mark.parametrize("recover", [True, False])
def test_page_retry_limit_and_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch, recover: bool
) -> None:
    requests = install_http_outcomes(
        monkeypatch,
        [TimeoutError(), TimeoutError(), b"[]" if recover else TimeoutError()],
    )
    delays: list[float] = []
    source = LeverJobSource(
        "example",
        "Example Company",
        clock=lambda: RETRIEVED_AT,
        sleeper=delays.append,
    )

    if recover:
        assert source.fetch_snapshot().jobs == ()
    else:
        with pytest.raises(SourceAcquisitionError) as caught:
            source.fetch_snapshot()
        message = str(caught.value)
        assert "Lever site 'example', skip=0, limit=100" in message
        assert "timeout (configured timeout=10s) (TimeoutError)" in message
        assert "retries exhausted after 3 attempts" in message
    assert len(requests) == 3
    assert delays == [1.0, 2.0]


@pytest.mark.parametrize(
    "outcome",
    [
        *[http_error(status) for status in (400, 401, 403, 404, 501, 505)],
        URLError(ssl.SSLCertVerificationError(1, "private certificate")),
        URLError(socket.gaierror(socket.EAI_NONAME, "private host")),
        URLError("unknown cause"),
        OSError(errno.EACCES, "private path"),
        b"invalid JSON",
        b"{}",
        b"[null]",
        json.dumps([posting(id="")]).encode(),
        json.dumps([posting(text=None)]).encode(),
        json.dumps([posting(hostedUrl="bad-url")]).encode(),
    ],
)
def test_permanent_page_failures_are_not_retried(
    monkeypatch: pytest.MonkeyPatch, outcome: bytes | Exception
) -> None:
    requests = install_http_outcomes(monkeypatch, [outcome])
    delays: list[float] = []

    with pytest.raises(SourceAcquisitionError):
        LeverJobSource(
            "example",
            "Example Company",
            clock=lambda: RETRIEVED_AT,
            sleeper=delays.append,
        ).fetch_snapshot()

    assert len(requests) == 1
    assert delays == []


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize(
    ("header", "expected_delay"),
    [
        (None, 1.0),
        ("0", 1.0),
        ("4", 4.0),
        ("5", 5.0),
        ("Tue, 08 Sep 2026 12:30:04 GMT", 4.0),
        ("Tue, 08 Sep 2026 12:29:00 GMT", 1.0),
        ("invalid", 1.0),
        ("-1", 1.0),
        ("6", None),
        ("Tue, 08 Sep 2026 12:30:06 GMT", None),
        ("9" * 100, None),
    ],
)
def test_retry_after_respects_server_wait_and_delay_cap(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    header: str | None,
    expected_delay: float | None,
) -> None:
    monkeypatch.setattr(lever, "_utc_now", lambda: RETRIEVED_AT)
    requests = install_http_outcomes(monkeypatch, [http_error(status, header), b"[]"])
    delays: list[float] = []
    source = LeverJobSource("example", "Example Company", sleeper=delays.append)

    if expected_delay is None:
        with pytest.raises(SourceAcquisitionError) as caught:
            source.fetch_snapshot()
        message = str(caught.value)
        assert "retry stopped after 1 attempts" in message
        assert "Retry-After exceeds configured delay cap (5s)" in message
        assert f"HTTP status {status}" in message
        assert len(requests) == 1
        assert delays == []
    else:
        assert source.fetch_snapshot().jobs == ()
        assert len(requests) == 2
        assert delays == [expected_delay]


def test_backoff_obeys_configured_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    install_http_outcomes(monkeypatch, [TimeoutError(), TimeoutError(), b"[]"])
    delays: list[float] = []

    LeverJobSource(
        "example",
        "Example Company",
        backoff_seconds=4,
        max_retry_delay_seconds=5,
        sleeper=delays.append,
    ).fetch_snapshot()

    assert delays == [4.0, 5.0]


@pytest.mark.parametrize("max_attempts", [1, 2])
def test_configurable_attempt_limit(
    monkeypatch: pytest.MonkeyPatch, max_attempts: int
) -> None:
    requests = install_http_outcomes(monkeypatch, [TimeoutError()] * max_attempts)
    delays: list[float] = []
    with pytest.raises(SourceAcquisitionError, match=f"after {max_attempts} attempts"):
        LeverJobSource(
            "example",
            "Example Company",
            max_attempts=max_attempts,
            sleeper=delays.append,
        ).fetch_snapshot()
    assert len(requests) == max_attempts
    assert delays == [1.0] * (max_attempts - 1)


@pytest.mark.parametrize(
    "options",
    [
        {"max_attempts": 4},
        {"max_attempts": 0},
        {"max_attempts": True},
        {"max_attempts": 1.5},
        {"backoff_seconds": 0},
        {"backoff_seconds": float("nan")},
        {"max_retry_delay_seconds": float("inf")},
        {"max_retry_delay_seconds": -1},
        {"timeout_seconds": float("nan")},
    ],
)
def test_retry_configuration_is_bounded(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        LeverJobSource("example", "Example Company", **options)


def test_later_page_recovery_persists_complete_snapshot_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request_json = lever._request_json
    install_pages(monkeypatch, [[posting(id="previous")]])
    repository = SQLiteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize_schema()
    initial = LeverJobSource(
        "example", "Example Company", clock=lambda: RETRIEVED_AT
    ).fetch_snapshot()
    repository.reconcile_snapshot(initial)
    monkeypatch.setattr(lever, "_request_json", request_json)
    requests = install_http_outcomes(
        monkeypatch,
        [
            json.dumps([posting(id="first"), posting(id="second")]).encode(),
            http_error(503),
            json.dumps([posting(id="last")]).encode(),
        ],
    )
    delays: list[float] = []
    observed_at = RETRIEVED_AT + timedelta(hours=1)
    source = LeverJobSource(
        "example",
        "Example Company",
        page_size=2,
        clock=lambda: observed_at,
        sleeper=delays.append,
    )

    assert acquire_and_persist(source, repository) == 3

    assert [parse_qs(urlsplit(url).query)["skip"] for url, _ in requests] == [
        ["0"],
        ["2"],
        ["2"],
    ]
    assert delays == [1.0]
    for source_id in ("first", "second", "last"):
        key = SourceJobKey("lever", "example", source_id)
        stored = repository.get(key)
        assert stored is not None
        assert stored.normalized.retrieved_at == observed_at
        lifecycle = repository.get_lifecycle(key)
        assert lifecycle is not None
        assert lifecycle.status is SourceJobStatus.ACTIVE
        assert lifecycle.last_seen_at == observed_at
    previous = repository.get_lifecycle(initial.jobs[0].normalized.key)
    assert previous is not None
    assert previous.status is SourceJobStatus.INACTIVE


def test_retry_budget_resets_per_page(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = install_http_outcomes(
        monkeypatch,
        [
            TimeoutError(),
            TimeoutError(),
            json.dumps([posting(id="one")]).encode(),
            TimeoutError(),
            TimeoutError(),
            json.dumps([posting(id="two")]).encode(),
            b"[]",
        ],
    )
    delays: list[float] = []

    snapshot = LeverJobSource(
        "example",
        "Example Company",
        page_size=1,
        sleeper=delays.append,
        clock=lambda: RETRIEVED_AT,
    ).fetch_snapshot()

    assert [job.normalized.key.source_job_id for job in snapshot.jobs] == ["one", "two"]
    assert [parse_qs(urlsplit(url).query)["skip"] for url, _ in requests] == [
        ["0"],
        ["0"],
        ["0"],
        ["1"],
        ["1"],
        ["1"],
        ["2"],
    ]
    assert delays == [1.0, 2.0, 1.0, 2.0]
