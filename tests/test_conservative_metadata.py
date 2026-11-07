"""Reviewed exact aliases, source/field scope and safe offline persistence."""

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from job_matcher.job_metadata import extract_metadata
from job_matcher.metadata_backfill import backfill
from job_matcher.models import (
    AcquiredJob,
    NormalizedJob,
    RawSourceRecord,
    SourceJobKey,
    SourceSnapshot,
)
from job_matcher.screening import SCREENING_VERSION
from job_matcher.sources.greenhouse import GreenhouseJobSource
from job_matcher.sources.lever import LeverJobSource
from job_matcher.sqlite_repository import SQLiteJobRepository

NOW = datetime(2026, 11, 6, tzinfo=UTC)


def greenhouse(entries):
    return extract_metadata(
        "greenhouse",
        {"metadata": [{"name": name, "value": value} for name, value in entries]},
        NOW,
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Full Time: In The Field", "full_time"),
        ("Studio Support (Part-Time)", "part_time"),
        ("Full Time Salary", "full_time"),
        ("Independent Contractor", "contract"),
        ("Freelance", "contract"),
        ("Summer Internship", "internship"),
        ("Fixed Term Employee", "temporary"),
        (" Full-TIME ", "full_time"),
        ("Contract Full time", "contract"),
        ("Intern", "internship"),
        ("Seasonal", "temporary"),
        (None, None),
        ("", None),
        ("Remote", None),
        ("International", None),
        ("International Office Entity", None),
        ("Internal Transfer", None),
        ("Non-Contract", None),
        ("Hourly Classified (Non-Contract)", None),
        ("Regular", None),
        ("Permanent", None),
        ("Full Time/Part Time", None),
        ("Full or Part Time", None),
        ("Contract Part time", None),
        ("Intern (Full-time)", None),
        ("Temporary/Contract", None),
        ("Co-op", None),
        ("Temps plein- Permanent", None),
        ("Not Full Time Salary", None),
        ("Independent Contractor or Employee", None),
        ("Full Time: In The Field / Part-time", None),
    ],
)
def test_lever_reviewed_values_and_no_substring_inference(value, expected):
    assert (
        extract_metadata("lever", {"categories": {"commitment": value}}, NOW)[
            "employment_type"
        ]
        == expected
    )


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("Employment Type", "Full-Time: Experienced", "full_time"),
        ("Employment Type", "Exempt - Full Time, Salaried", "full_time"),
        ("Employment type", "Part-time, non-exempt", "part_time"),
        # Same established space/underscore key as the reviewed exempt alias.
        ("Employment Type", "Full-time_exempt", "full_time"),
        ("Employee Type", "Full-time = 80-100hrs/bi-weekly", "full_time"),
        ("Schedule (FT, PT)", "Full-Time", "full_time"),
        ("Worker Category", "F-FULLTIME", "full_time"),
        ("Employment Duration", "Intern", "internship"),
        ("Engagement type", "Part-time", "part_time"),
        ("Classification", "Full-Time", "full_time"),
        ("Full/Part-Time", "Part-time", "part_time"),
        ("Full-Time/Part-Time Status", "Full-Time", "full_time"),
        ("Hours", "Full-Time", "full_time"),
        ("Schedule", "Full-time", "full_time"),
        ("UKG Pro Employee Type", "Intern", "internship"),
        ("Worker Sub-Type", "Fixed Term (Fixed Term)", "temporary"),
        ("Employee Class", "Fixed Term", "temporary"),
        ("Other Employment Types", "Temporary", "temporary"),
        ("  EMPLOYEE   TYPE  ", " Full-TIME = 80-100hrs/bi-weekly ", "full_time"),
        ("Employment Type", "Temporary Employee", None),
        ("Worker Sub-Type", "Intern", None),
        ("Worker Sub-Type", "Temporary", None),
        ("Careers Page Job Type", "Intern", None),
        ("Early Career Time Type", "Internship", None),
        ("Employment Type", "Freelance", None),
        ("Worker Category", "Full-Time: Experienced", None),
        ("Career Level", "Part-time", None),
        ("Job Type", "Physical Therapist (PT)", None),
        ("Workday Job Profile", "Technology Intern", None),
        ("Employment Type", "Regular", None),
        ("Employee Class", "Permanent", None),
        ("Worker Type", "Employee", None),
        ("Employment Type", "Working Student", None),
        ("Employment Type", ["Full-time"], None),
        ("Employment Type", "Consultant", None),
    ],
)
def test_greenhouse_employment_aliases_are_field_scoped(field, value, expected):
    assert greenhouse([(field, value)])["employment_type"] == expected


@pytest.mark.parametrize(
    "entries,expected",
    [
        ([("Employment Type", "Full-time"), ("Time Type", "FULL_TIME")], "full_time"),
        (
            [("Employee Type", "Full-time"), ("Schedule (FT, PT)", "Full-Time")],
            "full_time",
        ),
        ([("Employment Type", "Fixed Term"), ("Time Type", "Full time")], None),
        ([("Employment Type", "Intern"), ("Time Type", "Full time")], None),
        ([("Full/Part-Time", "Part-time"), ("Hours", "Full-time")], None),
        # Audit-vetoed additions do not introduce a new conflict on existing facts.
        (
            [("Employment Type", "Temporary Employee"), ("Time Type", "Full time")],
            "full_time",
        ),
        ([("Worker Sub-Type", "Intern"), ("Time Type", "Full time")], "full_time"),
    ],
)
def test_employment_consensus_without_dimension_precedence(entries, expected):
    assert greenhouse(entries)["employment_type"] == expected


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("Work Location Type", "On-site", "onsite"),
        ("Work Arrangement Type", ["Office"], "onsite"),
        ("Flexible Work Arrangement", "On-Site Essential", "onsite"),
        ("Work Style", "In Office", "onsite"),
        ("Work Type", ["Remote"], "remote"),
        ("Telework", "Position requires all work done on-site", "onsite"),
        ("Job Location Type", "Hybrid", "hybrid"),
        ("Work Designation", "Onsite", "onsite"),
        ("No. of days in office", "Hybrid – 3 days on-site/week", "hybrid"),
        ("Work Site Type", ["On-site"], "onsite"),
        ("Work Status", "Remote", "remote"),
        ("Job Model", "Office", "onsite"),
        ("Location type:", ["Remote"], "remote"),
        ("Remote Options", "Remote within single state", "remote"),
        ("Office Type", ["Hybrid"], "hybrid"),
        ("Remote Type", "Partially Remote/Hybrid", "hybrid"),
        ("Work Arrangement", ["Hybrid"], "hybrid"),
        ("Work Arrangement", ["Hybrid", "HYBRID"], "hybrid"),
        ("Work Arrangement", [], None),
        ("Work Arrangement", ["Hybrid", "Remote"], None),
        ("Work Site Type", ["Remote", "On-site"], None),
        ("Work Arrangement", ["Hybrid", "unreviewed"], None),
        ("Work Arrangement", [{"value": "Hybrid"}], None),
        ("Work Arrangement", {"value": "Hybrid"}, None),
        ("Working Model Eligibility", ["1-In Office"], None),
        ("Remote", True, None),
        ("Remote", False, None),
        ("Position open to remote", True, None),
        ("Careers Page Remote Eligible", "No", None),
        ("Work Location", "Remote", None),
        ("Location", "Remote", None),
        ("Job Location Type", "Full Flex", None),
        ("Type", "Hybrid", None),
        ("Job Type", ["Onsite"], None),
        ("Remote Type", "Remote - US Only", None),
    ],
)
def test_reviewed_workplace_aliases_lists_and_eligibility(field, value, expected):
    assert greenhouse([(field, value)])["workplace_type"] == expected


def test_workplace_conflicts_are_not_hidden_by_another_field():
    assert (
        greenhouse(
            [("Workplace Type", "Remote"), ("Work Arrangement", ["Hybrid", "Remote"])]
        )["workplace_type"]
        is None
    )
    assert (
        greenhouse([("Workplace Type", "Remote"), ("Work Style", "In Office")])[
            "workplace_type"
        ]
        is None
    )
    assert (
        greenhouse([("Workplace Type", "Remote"), ("Work Type", ["Remote"])])[
            "workplace_type"
        ]
        == "remote"
    )


def test_live_adapters_preserve_raw_facts_and_dates():
    lever = dict(
        id="1",
        text="Engineer",
        hostedUrl="https://example.com/1",
        categories={"commitment": "Independent Contractor"},
        createdAt=1767225600000,
    )
    job = LeverJobSource("example", "Example")._normalize(
        lever, source_job_id="1", retrieved_at=NOW
    )
    assert job.normalized.employment_type == "contract"
    assert job.normalized.posted_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert json.loads(job.raw.payload_json) == lever
    raw = dict(
        id=2,
        title="Engineer",
        absolute_url="https://example.com/2",
        first_published="2026-01-01T02:00:00+02:00",
        metadata=[{"name": "Work Location Type", "value": "Hybrid"}],
    )
    job = GreenhouseJobSource("example", "Example")._normalize(raw, "2", NOW)
    assert job.normalized.workplace_type == "hybrid"
    assert job.normalized.posted_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert json.loads(job.raw.payload_json) == raw


def synthetic_job(number, payload, **metadata):
    key = SourceJobKey("lever", "example", str(number))
    normalized = NormalizedJob(
        key,
        "Example",
        "Engineer",
        "Original description",
        "https://example.com/job",
        NOW,
        **metadata,
    )
    return AcquiredJob(normalized, RawSourceRecord(key, NOW, json.dumps(payload)))


def test_historical_backfill_preserves_dates_raw_identity_and_lifecycle(tmp_path):
    db = tmp_path / "authoritative.sqlite3"
    repo = SQLiteJobRepository(db)
    repo.initialize_schema()
    job = synthetic_job(
        1,
        {
            "categories": {"commitment": "Independent Contractor"},
            "createdAt": 1767225600000,
        },
    )
    recognized = synthetic_job(
        2, {"categories": {"commitment": "Full-time"}}, employment_type="full_time"
    )
    repo.reconcile_snapshot(SourceSnapshot("lever", "example", NOW, (job, recognized)))
    repo.reconcile_snapshot(
        SourceSnapshot("lever", "example", NOW + timedelta(days=1), ())
    )
    with sqlite3.connect(db) as c:
        before = {
            table: c.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "raw_source_jobs",
                "source_job_lifecycle",
                "source_scope_state",
                "source_job_links",
                "logical_jobs",
            )
        }
    count = backfill(
        db,
        fields=("employment_type", "workplace_type"),
        require_unchanged_existing=True,
    )
    assert count["lever"]["employment_type_enriched"] == 1
    assert repo.get(job.normalized.key).normalized == replace(
        job.normalized, employment_type="contract"
    )
    assert repo.get(recognized.normalized.key) == recognized
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT posted_at FROM normalized_jobs").fetchall() == [
            (None,),
            (None,),
        ]
        for table, rows in before.items():
            assert c.execute(f"SELECT * FROM {table}").fetchall() == rows
    rerun = backfill(
        db,
        fields=("employment_type", "workplace_type"),
        require_unchanged_existing=True,
    )
    assert not any(k.endswith("_enriched") for v in rerun.values() for k in v)
    assert SCREENING_VERSION == "5"


@pytest.mark.parametrize("raw_type", ["Part-time", "Remote"])
def test_strict_backfill_rolls_back_if_existing_classification_would_change(
    tmp_path, raw_type
):
    db = tmp_path / "authoritative.sqlite3"
    repo = SQLiteJobRepository(db)
    repo.initialize_schema()
    first = synthetic_job(1, {"categories": {"commitment": "Independent Contractor"}})
    second = synthetic_job(
        2, {"categories": {"commitment": raw_type}}, employment_type="full_time"
    )
    repo.reconcile_snapshot(SourceSnapshot("lever", "example", NOW, (first, second)))
    with pytest.raises(ValueError, match="existing classification"):
        backfill(db, fields=("employment_type",), require_unchanged_existing=True)
    assert repo.get(first.normalized.key) == first
    assert repo.get(second.normalized.key) == second
