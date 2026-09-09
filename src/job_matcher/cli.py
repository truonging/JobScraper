"""Manual command-line entry point for Job Matcher workflows."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from job_matcher import acquisition
from job_matcher.ports import PersistenceError, SourceAcquisitionError
from job_matcher.sources.lever import DEFAULT_API_BASE_URL, LeverJobSource
from job_matcher.sqlite_repository import SQLiteJobRepository


def build_parser() -> argparse.ArgumentParser:
    """Build the manual Phase 1 command-line interface."""
    parser = argparse.ArgumentParser(prog="job-matcher")
    commands = parser.add_subparsers(dest="command", required=True)

    acquire_lever = commands.add_parser(
        "acquire-lever",
        help="acquire one Lever site into SQLite",
    )
    acquire_lever.add_argument("--site", required=True, type=_non_blank)
    acquire_lever.add_argument("--company", required=True, type=_non_blank)
    acquire_lever.add_argument("--database", required=True, type=Path)
    acquire_lever.add_argument(
        "--api-base-url",
        default=DEFAULT_API_BASE_URL,
        type=_https_url_without_query,
    )
    acquire_lever.add_argument("--timeout-seconds", default=10.0, type=_positive_float)
    acquire_lever.add_argument("--page-size", default=100, type=_positive_int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested command and return its process exit code."""
    arguments = build_parser().parse_args(argv)

    if arguments.command == "acquire-lever":
        return _acquire_lever(arguments)
    raise AssertionError(f"unhandled command: {arguments.command}")


def _acquire_lever(arguments: argparse.Namespace) -> int:
    source = LeverJobSource(
        site=arguments.site,
        company=arguments.company,
        api_base_url=arguments.api_base_url,
        timeout_seconds=arguments.timeout_seconds,
        page_size=arguments.page_size,
    )
    repository = SQLiteJobRepository(arguments.database)

    try:
        repository.initialize_schema()
        count = acquisition.acquire_and_persist(source, repository)
    except (SourceAcquisitionError, PersistenceError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(
        f"Acquired and stored {count} Lever jobs from site {arguments.site!r} "
        f"in {arguments.database}"
    )
    return 0


def _non_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise argparse.ArgumentTypeError("value must not be blank")
    return stripped


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be a number") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _https_url_without_query(value: str) -> str:
    stripped = value.strip().rstrip("/")
    parsed = urlsplit(stripped)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError(
            "value must be an HTTPS URL without a query or fragment"
        )
    return stripped


if __name__ == "__main__":
    raise SystemExit(main())
