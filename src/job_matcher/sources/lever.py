"""Synchronous adapter for Lever's public Postings API."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit

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
    ) -> None:
        self._site = _configured_text(site, "site")
        self._company = _configured_text(company, "company")
        self._api_base_url = _api_base_url(api_base_url)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or timeout_seconds <= 0
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
            payload = _request_json(
                self._page_url(skip), timeout_seconds=self._timeout_seconds
            )
            if not isinstance(payload, list):
                raise SourceAcquisitionError(
                    "Lever postings response must be a JSON array"
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
                        f"Lever posting at index {absolute_index} is invalid"
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
                        f"Lever posting {source_job_id!r} is invalid"
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
            posted_at=None,
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
        raise SourceAcquisitionError(
            f"Lever request failed with HTTP status {error.code}"
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise SourceAcquisitionError("Lever request failed") from error

    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceAcquisitionError("Lever response was not valid JSON") from error


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
        raise ValueError("Lever posting description must not be blank")
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
