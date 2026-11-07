"""Complete-board acquisition from Greenhouse's public Job Board API."""

import json
import math
import re
import socket
import ssl
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from http.client import IncompleteRead
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit

from job_matcher.job_metadata import extract_metadata
from job_matcher.models import (
    AcquiredJob,
    ContractValidationError,
    NormalizedJob,
    RawSourceRecord,
    SourceJobKey,
    SourceSnapshot,
)
from job_matcher.ports import SourceAcquisitionError

DEFAULT_API_BASE_URL = "https://boards-api.greenhouse.io/v1/boards"
_RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}


class _TransientError(SourceAcquisitionError):
    def __init__(self, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class GreenhouseJobSource:
    """Return a snapshot only after the entire response has been validated."""

    def __init__(
        self,
        site: str,
        company: str,
        *,
        api_base_url: str = DEFAULT_API_BASE_URL,
        timeout_seconds: float = 20,
        max_attempts: int = 3,
        backoff_seconds: float = 1,
        max_retry_delay_seconds: float = 5,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if not isinstance(site, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", site):
            raise ValueError("site must be a Greenhouse board token")
        if not isinstance(company, str) or not company.strip():
            raise ValueError("company must be non-blank")
        if not isinstance(api_base_url, str):
            raise ValueError("api_base_url must be an HTTPS URL")
        parsed = urlsplit(api_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ValueError(
                "api_base_url must be an HTTPS URL without credentials, query or fragment"
            )
        for name, value in (
            ("timeout_seconds", timeout_seconds),
            ("backoff_seconds", backoff_seconds),
            ("max_retry_delay_seconds", max_retry_delay_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and greater than zero")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")
        self.site, self.company = site, company.strip()
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleeper = sleeper

    def fetch_snapshot(self) -> SourceSnapshot:
        retrieved_at = self.clock()
        if not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is not UTC:
            raise SourceAcquisitionError(
                "Greenhouse retrieval clock must use datetime.UTC"
            )
        try:
            payload = self._fetch_board()
            if not isinstance(payload, dict) or not isinstance(
                payload.get("jobs"), list
            ):
                raise ValueError("response must contain a jobs array")
            postings = payload["jobs"]
            # The documented full-board endpoint has no pagination contract.
            # meta.total is optional, not a prerequisite for a complete snapshot.
            meta = payload.get("meta")
            if meta is not None:
                if not isinstance(meta, dict):
                    raise ValueError("meta must be an object or null")
                if "total" in meta and (
                    type(meta["total"]) is not int or meta["total"] != len(postings)
                ):
                    raise ValueError("provided meta.total disagrees with jobs array")
            jobs = []
            seen = set()
            for index, posting in enumerate(postings):
                if (
                    not isinstance(posting, dict)
                    or type(posting.get("id")) is not int
                    or posting["id"] <= 0
                ):
                    raise ValueError(
                        f"posting at index {index} requires a positive integer id"
                    )
                source_id = str(posting["id"])
                if source_id in seen:
                    raise ValueError(f"duplicate posting id at index {index}")
                seen.add(source_id)
                jobs.append(self._normalize(posting, source_id, retrieved_at))
            return SourceSnapshot("greenhouse", self.site, retrieved_at, tuple(jobs))
        except SourceAcquisitionError as error:
            raise SourceAcquisitionError(
                f"Greenhouse board {self.site!r}: {error}"
            ) from error
        except (ValueError, TypeError, ContractValidationError) as error:
            # Never echo arbitrary upstream values or payloads.
            raise SourceAcquisitionError(
                f"Greenhouse board {self.site!r}: invalid response ({type(error).__name__})"
            ) from error

    def _fetch_board(self):
        url = f"{self.api_base_url}/{quote(self.site, safe='')}/jobs?content=true"
        for attempt in range(1, self.max_attempts + 1):
            try:
                return _request_json(url, self.timeout_seconds)
            except _TransientError as error:
                if attempt == self.max_attempts:
                    raise SourceAcquisitionError(
                        f"{error}; retries exhausted after {attempt} attempts"
                    ) from error
                delay = min(
                    self.backoff_seconds * 2 ** (attempt - 1),
                    self.max_retry_delay_seconds,
                )
                retry_after = _retry_after_seconds(error.retry_after)
                if retry_after is not None:
                    if retry_after > self.max_retry_delay_seconds:
                        raise SourceAcquisitionError(
                            f"{error}; Retry-After exceeds delay cap"
                        ) from error
                    delay = max(delay, retry_after)
                self.sleeper(delay)
        raise AssertionError("validated attempts")

    def _normalize(
        self, posting: dict, source_id: str, retrieved_at: datetime
    ) -> AcquiredJob:
        key = SourceJobKey("greenhouse", self.site, source_id)
        location = posting.get("location")
        if location is not None and not isinstance(location, dict):
            raise ValueError("location must be object or null")
        content = _text(posting, "content") or ""
        normalized = NormalizedJob(
            key=key,
            company=self.company,
            title=_text(posting, "title", required=True),
            description=_description(content),
            job_url=_text(posting, "absolute_url", required=True),
            location=_text(location or {}, "name"),
            apply_url=None,
            **extract_metadata("greenhouse", posting, retrieved_at),
            retrieved_at=retrieved_at,
        )
        return AcquiredJob(
            normalized,
            RawSourceRecord(
                key,
                retrieved_at,
                json.dumps(
                    posting, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            ),
        )


def _text(payload: dict, name: str, *, required: bool = False) -> str | None:
    value = payload.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"invalid {name}")
    return value.strip() or None


class _DescriptionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _description(content: str) -> str:
    # Decode escaped markup only when no actual markup is present. Preserve
    # entities representing literal operators inside already-real HTML.
    for _ in range(2):
        if re.search(
            r"</?(?:p|div|ul|ol|li|h[1-6]|br|span|strong|table)\b", content, re.I
        ):
            break
        decoded = unescape(content)
        if decoded == content:
            break
        content = decoded
    parser = _DescriptionParser()
    parser.feed(content)
    parser.close()
    return "\n".join(
        line
        for raw in "".join(parser.parts).splitlines()
        if (line := " ".join(raw.split()))
    )


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        return float(value) if len(value) < 16 else math.inf
    try:
        date = parsedate_to_datetime(value)
        return (
            max(0, (date - datetime.now(UTC)).total_seconds()) if date.tzinfo else None
        )
    except (ValueError, TypeError, OverflowError):
        return None


def _request_json(url: str, timeout: float):
    req = urllib_request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "job-matcher/0.0.1"}
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            body = response.read()
    except HTTPError as error:
        code = error.code
        retry_after = error.headers.get("Retry-After") if error.headers else None
        error.close()
        cls = _TransientError if code in _RETRYABLE_STATUSES else SourceAcquisitionError
        if cls is _TransientError:
            raise cls(f"HTTP status {code}", retry_after) from error
        raise cls(f"HTTP status {code}") from error
    except IncompleteRead as error:
        raise _TransientError("incomplete HTTP response") from error
    except (URLError, OSError) as error:
        reason = error.reason if isinstance(error, URLError) else error
        transient = (
            isinstance(reason, (TimeoutError, ConnectionError))
            or isinstance(reason, socket.gaierror)
            and reason.errno == socket.EAI_AGAIN
            or isinstance(reason, str)
            and reason.strip().casefold()
            in {"timed out", "connection reset", "connection refused"}
            or isinstance(reason, OSError)
            and reason.errno
            in {
                10050,
                10051,
                10052,
                10053,
                10054,
                10060,
                10061,
                10064,
                10065,
                32,
                101,
                104,
                110,
                111,
                113,
            }
        ) and not isinstance(reason, ssl.SSLError)
        code = getattr(reason, "errno", None)
        message = f"transport failure ({type(reason).__name__}, errno={code if isinstance(code, int) else 'unknown'}, timeout={timeout:g}s)"
        cls = _TransientError if transient else SourceAcquisitionError
        raise cls(message) from error
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceAcquisitionError("response was not valid JSON") from error
