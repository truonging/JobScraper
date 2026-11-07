"""Synchronous adapter for Lever's public Postings API."""

from __future__ import annotations

import errno
import json
import math
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit

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

DEFAULT_API_BASE_URL = "https://api.lever.co/v0/postings"
_RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
_RETRYABLE_ERRNOS = {
    errno.ETIMEDOUT,
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.ECONNREFUSED,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.EHOSTUNREACH,
    errno.EPIPE,
    # Winsock codes may appear directly in OSError.errno on Windows.
    10050,
    10051,
    10052,
    10053,
    10054,
    10060,
    10061,
    10064,
    10065,
}


class _RetryableRequestError(SourceAcquisitionError):
    def __init__(self, message: str, *, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LeverJobSource:
    """Fetch and normalize published jobs from one configured Lever site."""

    def __init__(
        self,
        site: str,
        company: str,
        *,
        api_base_url: str = DEFAULT_API_BASE_URL,
        timeout_seconds: float = 10.0,
        page_size: int = 100,
        clock: Callable[[], datetime] | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        max_retry_delay_seconds: float = 5.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._site = _configured_text(site, "site")
        self._company = _configured_text(company, "company")
        self._api_base_url = _api_base_url(api_base_url)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or timeout_seconds <= 0
            or not math.isfinite(timeout_seconds)
        ):
            raise ValueError("timeout_seconds must be greater than zero")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or page_size <= 0
        ):
            raise ValueError("page_size must be a positive integer")
        self._timeout_seconds = float(timeout_seconds)
        self._page_size = page_size
        self._clock = clock or _utc_now
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 3
        ):
            raise ValueError("max_attempts must be an integer between 1 and 3")
        for name, value in (
            ("backoff_seconds", backoff_seconds),
            ("max_retry_delay_seconds", max_retry_delay_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite number greater than zero")
        self._max_attempts = max_attempts
        self._backoff_seconds = float(backoff_seconds)
        self._max_retry_delay_seconds = float(max_retry_delay_seconds)
        self._sleeper = sleeper

    def fetch_snapshot(self) -> SourceSnapshot:
        """Return a complete snapshot of the configured Lever site."""
        retrieved_at = self._clock()
        if not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is not UTC:
            raise SourceAcquisitionError(
                "Lever retrieval clock must return a datetime using datetime.UTC"
            )

        jobs: list[AcquiredJob] = []
        seen_ids: set[str] = set()
        skip = 0

        while True:
            page_context = (
                f"Lever site {self._site!r}, skip={skip}, limit={self._page_size}"
            )
            try:
                payload = self._fetch_page(skip)
            except SourceAcquisitionError as error:
                raise SourceAcquisitionError(f"{page_context}: {error}") from error
            if not isinstance(payload, list):
                raise SourceAcquisitionError(
                    f"{page_context}: Lever postings response must be a JSON array"
                )

            for page_index, raw_posting in enumerate(payload):
                absolute_index = skip + page_index
                if not isinstance(raw_posting, dict) or not raw_posting:
                    raise SourceAcquisitionError(
                        f"Lever posting at index {absolute_index} must be a JSON object"
                    )

                try:
                    source_job_id = _required_text(raw_posting, "id")
                except ValueError as error:
                    raise SourceAcquisitionError(
                        f"Lever posting at index {absolute_index} is invalid: {error}"
                    ) from error
                if source_job_id in seen_ids:
                    raise SourceAcquisitionError(
                        f"Lever returned duplicate posting id {source_job_id!r}"
                    )
                seen_ids.add(source_job_id)

                try:
                    jobs.append(
                        self._normalize(
                            raw_posting,
                            source_job_id=source_job_id,
                            retrieved_at=retrieved_at,
                        )
                    )
                except (ContractValidationError, TypeError, ValueError) as error:
                    raise SourceAcquisitionError(
                        f"Lever posting {source_job_id!r} is invalid: {error}"
                    ) from error

            if len(payload) < self._page_size:
                break
            skip += self._page_size

        return SourceSnapshot(
            source="lever",
            source_scope=self._site,
            retrieved_at=retrieved_at,
            jobs=tuple(jobs),
        )

    def _page_url(self, skip: int) -> str:
        query = urlencode({"mode": "json", "skip": skip, "limit": self._page_size})
        return f"{self._api_base_url}/{quote(self._site, safe='')}?{query}"

    def _fetch_page(self, skip: int) -> Any:
        for attempt in range(1, self._max_attempts + 1):
            try:
                return _request_json(
                    self._page_url(skip), timeout_seconds=self._timeout_seconds
                )
            except _RetryableRequestError as error:
                if attempt == self._max_attempts:
                    raise SourceAcquisitionError(
                        f"{error}; retries exhausted after {attempt} attempts"
                    ) from error
                delay = min(
                    self._backoff_seconds * (2 ** (attempt - 1)),
                    self._max_retry_delay_seconds,
                )
                retry_after = _retry_after_seconds(error.retry_after)
                if retry_after is not None:
                    if retry_after > self._max_retry_delay_seconds:
                        raise SourceAcquisitionError(
                            f"{error}; retry stopped after {attempt} attempts: "
                            "Retry-After exceeds configured delay cap "
                            f"({self._max_retry_delay_seconds:g}s)"
                        ) from error
                    delay = max(delay, retry_after)
                self._sleeper(delay)
        raise AssertionError("unreachable: max_attempts is validated")

    def _normalize(
        self,
        raw_posting: dict[str, Any],
        *,
        source_job_id: str,
        retrieved_at: datetime,
    ) -> AcquiredJob:
        key = SourceJobKey(
            source="lever",
            source_scope=self._site,
            source_job_id=source_job_id,
        )
        normalized = NormalizedJob(
            key=key,
            company=self._company,
            title=_required_text(raw_posting, "text"),
            description=_description(raw_posting),
            location=_location(raw_posting),
            job_url=_required_text(raw_posting, "hostedUrl"),
            apply_url=_optional_text(raw_posting, "applyUrl"),
            **extract_metadata("lever", raw_posting, retrieved_at),
            retrieved_at=retrieved_at,
        )
        raw = RawSourceRecord(
            key=key,
            retrieved_at=retrieved_at,
            payload_json=json.dumps(
                raw_posting,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        return AcquiredJob(normalized=normalized, raw=raw)


def _request_json(url: str, *, timeout_seconds: float) -> Any:
    request = urllib_request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "job-matcher/0.0.1"},
        method="GET",
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
    except HTTPError as error:
        message = f"Lever request failed with HTTP status {error.code}"
        retry_after = error.headers.get("Retry-After") if error.headers else None
        error.close()
        if error.code in _RETRYABLE_HTTP_STATUSES:
            raise _RetryableRequestError(message, retry_after=retry_after) from error
        raise SourceAcquisitionError(message) from error
    except (URLError, TimeoutError, OSError) as error:
        detail = _transport_failure_detail(error, timeout_seconds)
        if _is_transient_transport(error):
            raise _RetryableRequestError(f"Lever request failed: {detail}") from error
        raise SourceAcquisitionError(f"Lever request failed: {detail}") from error

    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceAcquisitionError("Lever response was not valid JSON") from error


def _is_transient_transport(error: OSError) -> bool:
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLError):
        return False
    if isinstance(reason, socket.gaierror):
        return reason.errno == socket.EAI_AGAIN
    if isinstance(
        reason,
        (
            TimeoutError,
            ConnectionResetError,
            ConnectionAbortedError,
            ConnectionRefusedError,
            BrokenPipeError,
        ),
    ):
        return True
    if isinstance(reason, OSError):
        return reason.errno in _RETRYABLE_ERRNOS
    # Some urllib handlers provide string reasons. Only exact known causes qualify.
    return isinstance(reason, str) and reason.strip().casefold() in {
        "timed out",
        "connection refused",
        "connection reset",
    }


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        # Avoid converting unbounded integers from an external header.
        return float(value) if len(value) < 16 else math.inf
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            return None
        return max(0.0, (retry_at - _utc_now()).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _transport_failure_detail(error: OSError, timeout_seconds: float) -> str:
    """Expose categories and numeric codes without arbitrary exception text."""
    reason = error.reason if isinstance(error, URLError) else error
    # Some urllib handlers supply a string rather than an underlying exception.
    # Recognize common causes, but never echo URLs, credentials, or response data.
    message = str(reason).casefold()
    if isinstance(reason, TimeoutError) or "timed out" in message:
        category = f"timeout (configured timeout={timeout_seconds:g}s)"
    elif isinstance(reason, socket.gaierror):
        category = "DNS resolution error"
    elif isinstance(reason, ssl.SSLError):
        category = "TLS error"
    elif isinstance(reason, ConnectionError):
        category = "connection error"
    else:
        category = "transport error"
        for text, label in (
            ("connection refused", "connection refused"),
            ("connection reset", "connection reset"),
            ("name resolution", "DNS resolution error"),
            ("getaddrinfo failed", "DNS resolution error"),
            ("certificate verify failed", "TLS certificate verification error"),
        ):
            if text in message:
                category = label
                break
    cause = reason if isinstance(reason, BaseException) else error
    code = getattr(cause, "errno", None)
    code_detail = f", errno={code}" if isinstance(code, int) else ""
    return f"{category} ({type(cause).__name__}{code_detail})"


def _configured_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-blank string")
    return value.strip()


def _api_base_url(value: str) -> str:
    base_url = _configured_text(value, "api_base_url").rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError(
            "api_base_url must be an HTTPS URL without a query or fragment"
        )
    return base_url


def _required_text(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-blank string")
    return value.strip()


def _optional_text(payload: Mapping[str, Any], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or null")
    stripped = value.strip()
    return stripped or None


def _location(payload: Mapping[str, Any]) -> str | None:
    categories = payload.get("categories")
    if categories is None:
        return None
    if not isinstance(categories, dict):
        raise ValueError("categories must be an object or null")
    return _optional_text(categories, "location")


def _description(payload: Mapping[str, Any]) -> str:
    sections: list[str] = []
    description = _optional_text(payload, "descriptionPlain")
    if description:
        sections.append(_normalize_plain_text(description))

    lists = payload.get("lists")
    if lists is not None:
        if not isinstance(lists, list):
            raise ValueError("lists must be an array or null")
        for item in lists:
            if not isinstance(item, dict):
                raise ValueError("each lists item must be an object")
            heading = _optional_text(item, "text")
            content = _optional_text(item, "content")
            plain_content = _html_to_plain_text(content) if content else None
            section = "\n".join(part for part in (heading, plain_content) if part)
            if section:
                sections.append(section)

    additional = _optional_text(payload, "additionalPlain")
    if additional:
        sections.append(_normalize_plain_text(additional))

    combined = "\n\n".join(section for section in sections if section)
    if not combined:
        description_body_plain = _optional_text(payload, "descriptionBodyPlain")
        if description_body_plain:
            combined = _normalize_plain_text(description_body_plain)
        else:
            description_body = _optional_text(payload, "descriptionBody")
            if description_body:
                combined = _html_to_plain_text(description_body)
    return combined


class _LeverHTMLParser(HTMLParser):
    _BLOCK_TAGS = {
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _html_to_plain_text(value: str) -> str:
    parser = _LeverHTMLParser()
    parser.feed(value)
    parser.close()
    return _normalize_plain_text("".join(parser.parts))


def _normalize_plain_text(value: str) -> str:
    lines: list[str] = []
    previous_blank = True
    for raw_line in value.splitlines():
        line = " ".join(raw_line.split())
        if line:
            lines.append(line)
            previous_blank = False
        elif not previous_blank:
            lines.append("")
            previous_blank = True
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _utc_now() -> datetime:
    return datetime.now(UTC)
