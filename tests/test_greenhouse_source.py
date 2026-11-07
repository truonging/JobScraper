"""Greenhouse contract, transport, and complete-snapshot regressions."""

import json
import ssl
from datetime import UTC, datetime
from email.message import Message
from http.client import IncompleteRead
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from job_matcher.ports import SourceAcquisitionError
from job_matcher.sources import greenhouse as gh

NOW = datetime(2026, 11, 3, tzinfo=UTC)


def posting(**changes):
    return {
        "id": 123,
        "internal_job_id": 999,
        "title": "Software Engineer",
        "absolute_url": "https://example.com/jobs/123",
        "content": "&lt;p&gt;Build services.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Write Python.&lt;/li&gt;&lt;/ul&gt;",
        "location": {"name": "US, Remote"},
        "first_published": "2026-10-01T12:00:00Z",
        **changes,
    }


def source(monkeypatch, payload, **options):
    monkeypatch.setattr(gh, "_request_json", lambda url, timeout: payload)
    return gh.GreenhouseJobSource("example", "Example", clock=lambda: NOW, **options)


def test_normalizes_posting_id_and_preserves_complete_raw(monkeypatch):
    raw = posting(
        offices=[{"name": "HQ", "location": None}],
        metadata=[{"name": "anything", "value": "raw"}],
    )
    job = source(monkeypatch, {"jobs": [raw]}).fetch_snapshot().jobs[0]
    assert job.normalized.key.source == "greenhouse"
    assert job.normalized.key.source_scope == "example"
    assert job.normalized.key.source_job_id == "123"
    assert job.normalized.description == "Build services.\n- Write Python."
    assert job.normalized.posted_at == datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert job.normalized.apply_url is None
    assert job.normalized.retrieved_at == job.raw.retrieved_at == NOW
    assert json.loads(job.raw.payload_json) == raw


@pytest.mark.parametrize(
    "envelope",
    [
        {"jobs": []},
        {"jobs": [], "meta": None},
        {"jobs": [], "meta": {}},
        {"jobs": [], "meta": {"total": 0}},
    ],
)
def test_complete_empty_array_does_not_require_total(monkeypatch, envelope):
    assert source(monkeypatch, envelope).fetch_snapshot().jobs == ()


@pytest.mark.parametrize(
    "content", [None, "", "   ", "<p> </p>", "&lt;p&gt;&lt;/p&gt;"]
)
def test_empty_descriptions_are_retained(monkeypatch, content):
    assert (
        source(monkeypatch, {"jobs": [posting(content=content)]})
        .fetch_snapshot()
        .jobs[0]
        .normalized.description
        == ""
    )


def test_missing_optional_fields_and_prospect_post(monkeypatch):
    p = posting(internal_job_id=None, updated_at="2026-11-02T12:00:00Z")
    for name in ["content", "location", "first_published"]:
        p.pop(name)
    j = source(monkeypatch, {"jobs": [p]}).fetch_snapshot().jobs[0]
    assert j.normalized.description == ""
    assert j.normalized.location is None
    assert j.normalized.posted_at is None


@pytest.mark.parametrize(
    "content,expected",
    [
        ("&amp;lt;p&amp;gt;Hello&amp;lt;/p&amp;gt;", "Hello"),
        ("<p>Use x &lt; y &amp;&amp; z &gt; 2.</p>", "Use x < y && z > 2."),
        ("<script>secret()</script><p>Build APIs.</p>", "Build APIs."),
    ],
)
def test_html_and_entities(content, expected):
    assert gh._description(content) == expected


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"jobs": None},
        {"jobs": {}},
        {"jobs": [None]},
        {"jobs": [posting(), posting()]},
        {"jobs": [posting()], "meta": {"total": 2}},
        {"jobs": [], "meta": {"total": False}},
        {"jobs": [], "meta": []},
    ],
)
def test_invalid_board_never_returns_partial_snapshot(monkeypatch, payload):
    with pytest.raises(SourceAcquisitionError):
        source(monkeypatch, payload).fetch_snapshot()


@pytest.mark.parametrize(
    "changes",
    [
        {"id": True},
        {"id": "123"},
        {"id": 0},
        {"id": -1},
        {"title": None},
        {"absolute_url": "not a URL"},
        {"location": []},
        {"location": {"name": 5}},
        {"content": 5},
    ],
)
def test_malformed_posting_rejects_entire_snapshot(monkeypatch, changes):
    with pytest.raises(SourceAcquisitionError):
        source(
            monkeypatch,
            {
                "jobs": [
                    posting(),
                    posting(id=456, **{k: v for k, v in changes.items() if k != "id"}),
                ]
                if "id" not in changes
                else [posting(**changes)]
            },
        ).fetch_snapshot()


class Response(BytesIO):
    pass


@pytest.mark.parametrize(
    "status,attempts", [(429, 3), (500, 3), (503, 3), (404, 1), (403, 1)]
)
def test_http_retry_policy_and_sanitized_errors(monkeypatch, status, attempts):
    calls = []
    delays = []

    def request(req, timeout):
        calls.append(req.full_url)
        raise HTTPError(
            req.full_url, status, "SECRET BODY", Message(), BytesIO(b"SECRET BODY")
        )

    monkeypatch.setattr(gh.urllib_request, "urlopen", request)
    with pytest.raises(SourceAcquisitionError) as e:
        gh.GreenhouseJobSource(
            "example", "Example", sleeper=delays.append
        ).fetch_snapshot()
    assert len(calls) == attempts
    assert delays == ([1, 2] if attempts == 3 else [])
    assert "SECRET" not in str(e.value)
    assert "content=true" in calls[0]


def test_retry_after_cap_and_success_after_timeout(monkeypatch):
    headers = Message()
    headers["Retry-After"] = "6"
    calls = []

    def fail(req, timeout):
        calls.append(req)
        raise HTTPError(req.full_url, 429, "secret", headers, BytesIO())

    monkeypatch.setattr(gh.urllib_request, "urlopen", fail)
    with pytest.raises(SourceAcquisitionError, match="cap"):
        gh.GreenhouseJobSource("example", "Example").fetch_snapshot()
    assert len(calls) == 1
    calls.clear()
    delays = []

    def recover(req, timeout):
        calls.append(req)
        if len(calls) == 1:
            raise URLError(TimeoutError("secret host"))
        return Response(b'{"jobs":[]}')

    monkeypatch.setattr(gh.urllib_request, "urlopen", recover)
    assert (
        gh.GreenhouseJobSource("example", "Example", sleeper=delays.append)
        .fetch_snapshot()
        .jobs
        == ()
    )
    assert delays == [1]


def test_retry_after_within_cap_is_honored(monkeypatch):
    calls = []
    delays = []
    headers = Message()
    headers["Retry-After"] = "2"

    def request(req, timeout):
        calls.append(req)
        if len(calls) < 3:
            raise HTTPError(req.full_url, 429, "secret", headers, BytesIO())
        return Response(b'{"jobs":[]}')

    monkeypatch.setattr(gh.urllib_request, "urlopen", request)
    assert (
        gh.GreenhouseJobSource("example", "Example", sleeper=delays.append)
        .fetch_snapshot()
        .jobs
        == ()
    )
    assert delays == [2, 2]


@pytest.mark.parametrize(
    "error", [ssl.SSLError("secret"), URLError(ssl.SSLError("secret"))]
)
def test_tls_not_retried(monkeypatch, error):
    calls = []

    def fail(req, timeout):
        calls.append(req)
        raise error

    monkeypatch.setattr(gh.urllib_request, "urlopen", fail)
    with pytest.raises(SourceAcquisitionError) as e:
        gh.GreenhouseJobSource("example", "Example").fetch_snapshot()
    assert len(calls) == 1 and "secret" not in str(e.value)


def test_invalid_json_is_not_retried(monkeypatch):
    calls = []

    def request(req, timeout):
        calls.append(req)
        return Response(b'{"jobs":[')

    monkeypatch.setattr(gh.urllib_request, "urlopen", request)
    with pytest.raises(SourceAcquisitionError, match="JSON"):
        gh.GreenhouseJobSource("example", "Example").fetch_snapshot()
    assert len(calls) == 1


def test_incomplete_http_body_retries_without_returning_partial_jobs(monkeypatch):
    calls = []

    class Partial(Response):
        def read(self, *args):
            raise IncompleteRead(b'{"jobs":[', 100)

    def request(req, timeout):
        calls.append(req)
        return Partial()

    monkeypatch.setattr(gh.urllib_request, "urlopen", request)
    with pytest.raises(SourceAcquisitionError, match="incomplete HTTP response"):
        gh.GreenhouseJobSource(
            "example", "Example", sleeper=lambda _: None
        ).fetch_snapshot()
    assert len(calls) == 3


@pytest.mark.parametrize(
    "options",
    [
        {"max_attempts": 0},
        {"max_attempts": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"backoff_seconds": -1},
        {"max_retry_delay_seconds": 0},
        {"api_base_url": "http://example.com"},
    ],
)
def test_configuration(options):
    with pytest.raises(ValueError):
        gh.GreenhouseJobSource("example", "Example", **options)
