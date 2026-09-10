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


def pipeline_command(database_path: Path, policy_path: Path) -> list[str]:
    return [
        "run-lever-pipeline",
        "--site",
        "example",
        "--company",
        "Example Company",
        "--database",
        str(database_path),
        "--filter-policy",
        str(policy_path),
    ]


def write_policy(path: Path) -> None:
    path.write_text(
        """\
version = 1

[title]
include_any = ["software"]
exclude_any = []

[location]
include_any = ["remote"]
exclude_any = []

[company]
exclude = []

[description]
include_any = ["software"]
exclude_any = []
""",
        encoding="utf-8",
    )


def table_count(database_path: Path, table_name: str) -> int:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return int(row[0])


def persisted_state(
    database_path: Path,
) -> tuple[int, dict[str, list[tuple[Any, ...]]]]:
    tables = (
        "raw_source_jobs",
        "normalized_jobs",
        "source_scope_state",
        "source_job_lifecycle",
        "logical_jobs",
        "source_job_links",
    )
    with sqlite3.connect(database_path) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        rows = {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in tables
        }
    return version, rows


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


def test_pipeline_cli_runs_end_to_end_and_rerun_preserves_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    policy_path = tmp_path / "filter_policy.toml"
    write_policy(policy_path)
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

    assert cli.main(pipeline_command(database_path, policy_path)) == 0
    first_output = capsys.readouterr().out
    assert "acquired 1 postings" in first_output
    assert "resolved 1 source postings" in first_output
    assert "evaluated 1 active catalog records" in first_output
    assert "eligible 1 logical jobs" in first_output

    assert cli.main(pipeline_command(database_path, policy_path)) == 0
    second_output = capsys.readouterr().out
    assert "acquired 1 postings" in second_output
    assert "resolved 0 source postings" in second_output
    assert "evaluated 1 active catalog records" in second_output
    assert "eligible 1 logical jobs" in second_output
    assert table_count(database_path, "raw_source_jobs") == 1
    assert table_count(database_path, "normalized_jobs") == 1
    assert table_count(database_path, "source_job_links") == 1
    assert table_count(database_path, "logical_jobs") == 1


def test_pipeline_cli_treats_an_empty_snapshot_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy_path = tmp_path / "filter_policy.toml"
    write_policy(policy_path)
    monkeypatch.setattr(
        lever.urllib_request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse([]),
    )

    result = cli.main(pipeline_command(tmp_path / "jobs.sqlite3", policy_path))

    captured = capsys.readouterr()
    assert result == 0
    assert "acquired 0 postings" in captured.out
    assert "resolved 0 source postings" in captured.out
    assert "evaluated 0 active catalog records" in captured.out
    assert "eligible 0 logical jobs" in captured.out
    assert captured.err == ""


def test_pipeline_cli_empty_snapshot_inactivates_job_and_removes_it_from_filtering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    policy_path = tmp_path / "filter_policy.toml"
    write_policy(policy_path)
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

    assert cli.main(pipeline_command(database_path, policy_path)) == 0
    capsys.readouterr()

    assert cli.main(pipeline_command(database_path, policy_path)) == 0

    captured = capsys.readouterr()
    repository = SQLiteJobRepository(database_path)
    key = SourceJobKey("lever", "example", "posting-123")
    lifecycle = repository.get_lifecycle(key)
    assert lifecycle is not None
    assert lifecycle.status is SourceJobStatus.INACTIVE
    assert repository.list_active_linked_jobs() == ()
    assert len(repository.list_linked_jobs()) == 1
    assert "acquired 0 postings" in captured.out
    assert "evaluated 0 active catalog records" in captured.out
    assert "eligible 0 logical jobs" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("second_outcome", "expected_error"),
    [
        (URLError("unavailable"), "error: Lever request failed\n"),
        ({}, "error: Lever postings response must be a JSON array\n"),
    ],
    ids=("request-failure", "malformed-response"),
)
def test_pipeline_cli_acquisition_failure_preserves_all_persisted_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    second_outcome: Any,
    expected_error: str,
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    policy_path = tmp_path / "filter_policy.toml"
    write_policy(policy_path)
    call_count = 0
    retrieval_times = iter(
        [
            datetime(2026, 9, 8, 12, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 13, 30, tzinfo=UTC),
        ]
    )

    def fake_urlopen(*_args: Any, **_kwargs: Any) -> FakeResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return FakeResponse([posting()])
        if isinstance(second_outcome, BaseException):
            raise second_outcome
        return FakeResponse(second_outcome)

    monkeypatch.setattr(lever.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(lever, "_utc_now", lambda: next(retrieval_times))

    assert cli.main(pipeline_command(database_path, policy_path)) == 0
    capsys.readouterr()
    state_before = persisted_state(database_path)

    assert cli.main(pipeline_command(database_path, policy_path)) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == expected_error
    assert persisted_state(database_path) == state_before


def test_pipeline_cli_loads_policy_before_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requested = False

    def fake_urlopen(*_args: Any, **_kwargs: Any) -> FakeResponse:
        nonlocal requested
        requested = True
        return FakeResponse([])

    monkeypatch.setattr(lever.urllib_request, "urlopen", fake_urlopen)

    result = cli.main(
        pipeline_command(
            tmp_path / "jobs.sqlite3",
            tmp_path / "missing_policy.toml",
        )
    )

    captured = capsys.readouterr()
    assert result == 1
    assert requested is False
    assert captured.out == ""
    assert "could not read filter policy" in captured.err
    assert not (tmp_path / "jobs.sqlite3").exists()


def test_pipeline_cli_reports_malformed_toml_before_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    policy_path = tmp_path / "filter_policy.toml"
    policy_path.write_text("version = [1", encoding="utf-8")
    requested = False

    def fake_urlopen(*_args: Any, **_kwargs: Any) -> FakeResponse:
        nonlocal requested
        requested = True
        return FakeResponse([])

    monkeypatch.setattr(lever.urllib_request, "urlopen", fake_urlopen)

    result = cli.main(pipeline_command(database_path, policy_path))

    captured = capsys.readouterr()
    assert result == 1
    assert requested is False
    assert captured.out == ""
    assert captured.err == "error: filter policy must be valid UTF-8 TOML\n"
    assert not database_path.exists()


def test_pipeline_cli_translates_source_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy_path = tmp_path / "filter_policy.toml"
    write_policy(policy_path)

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise URLError("unavailable")

    monkeypatch.setattr(lever.urllib_request, "urlopen", fail)

    result = cli.main(pipeline_command(tmp_path / "jobs.sqlite3", policy_path))

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "error: Lever request failed\n"


def test_pipeline_cli_translates_persistence_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy_path = tmp_path / "filter_policy.toml"
    write_policy(policy_path)

    result = cli.main(
        pipeline_command(tmp_path / "missing" / "jobs.sqlite3", policy_path)
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "database directory does not exist" in captured.err


def test_pipeline_cli_does_not_translate_unexpected_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "filter_policy.toml"
    write_policy(policy_path)

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("programming error")

    monkeypatch.setattr(cli.pipeline, "run_job_pipeline", fail)

    with pytest.raises(RuntimeError, match="programming error"):
        cli.main(pipeline_command(tmp_path / "jobs.sqlite3", policy_path))
