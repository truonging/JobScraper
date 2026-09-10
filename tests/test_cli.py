"""Tests for the manual acquisition CLI."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest

from job_matcher import cli
from job_matcher.catalog import SourceJobStatus
from job_matcher.models import SourceJobKey
from job_matcher.sources import lever
from job_matcher.sqlite_repository import SQLiteJobRepository


def posting(*, title: str = "Software Engineer") -> dict[str, Any]:
    return {
        "id": "posting-123",
        "text": title,
        "categories": {"location": "Remote"},
        "descriptionPlain": "Build reliable software.",
        "lists": [],
        "additionalPlain": "",
        "hostedUrl": "https://jobs.lever.co/example/posting-123",
        "applyUrl": "https://jobs.lever.co/example/posting-123/apply",
    }


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def command(database_path: Path) -> list[str]:
    return [
        "acquire-lever",
        "--site",
        "example",
        "--company",
        "Example Company",
        "--database",
        str(database_path),
    ]


def table_count(database_path: Path, table_name: str) -> int:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return int(row[0])


def test_cli_acquires_into_sqlite_and_rerun_updates_current_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    responses = [[posting()], [posting(title="Senior Software Engineer")]]
    retrieval_times = iter(
        [
            datetime(2026, 9, 8, 12, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 13, 30, tzinfo=UTC),
        ]
    )

    def fake_urlopen(*_args: Any, **_kwargs: Any) -> FakeResponse:
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr(lever.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(lever, "_utc_now", lambda: next(retrieval_times))

    assert cli.main(command(database_path)) == 0
    assert cli.main(command(database_path)) == 0

    repository = SQLiteJobRepository(database_path)
    stored = repository.get(SourceJobKey("lever", "example", "posting-123"))
    assert stored is not None
    assert stored.normalized.title == "Senior Software Engineer"
    assert table_count(database_path, "raw_source_jobs") == 1
    assert table_count(database_path, "normalized_jobs") == 1
    assert capsys.readouterr().out.count("Acquired and stored 1 Lever jobs") == 2


def test_cli_successful_empty_snapshot_inactivates_missing_posting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    responses = [[posting()], []]
    retrieval_times = iter(
        [
            datetime(2026, 9, 8, 12, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 13, 30, tzinfo=UTC),
        ]
    )

    def fake_urlopen(*_args: Any, **_kwargs: Any) -> FakeResponse:
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr(lever.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(lever, "_utc_now", lambda: next(retrieval_times))

    assert cli.main(command(database_path)) == 0
    assert cli.main(command(database_path)) == 0

    lifecycle = SQLiteJobRepository(database_path).get_lifecycle(
        SourceJobKey("lever", "example", "posting-123")
    )
    assert lifecycle is not None
    assert lifecycle.status is SourceJobStatus.INACTIVE
    assert lifecycle.inactive_at == datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
    assert "Acquired and stored 0 Lever jobs" in capsys.readouterr().out


def test_cli_treats_zero_jobs_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        lever.urllib_request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse([]),
    )

    result = cli.main(command(tmp_path / "jobs.sqlite3"))

    captured = capsys.readouterr()
    assert result == 0
    assert "Acquired and stored 0 Lever jobs" in captured.out
    assert captured.err == ""


def test_cli_translates_source_error_to_exit_code_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise URLError("unavailable")

    monkeypatch.setattr(lever.urllib_request, "urlopen", fail)

    result = cli.main(command(tmp_path / "jobs.sqlite3"))

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "error: Lever request failed\n"


def test_cli_translates_schema_initialization_error_to_exit_code_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = cli.main(command(tmp_path / "missing" / "jobs.sqlite3"))

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "database directory does not exist" in captured.err


def test_cli_does_not_translate_unexpected_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("programming error")

    monkeypatch.setattr(cli.acquisition, "acquire_and_persist", fail)

    with pytest.raises(RuntimeError, match="programming error"):
        cli.main(command(tmp_path / "jobs.sqlite3"))
