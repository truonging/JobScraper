"""Exercise the independent implementation; none of these activate screening."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from job_matcher.catalog import LinkedSourceJob, LogicalJobId, SourceJobLink
from job_matcher.geographic_classification import classify_geography
from job_matcher.models import (
    ContractValidationError,
    NormalizedJob,
    RawSourceRecord,
    SourceJobKey,
)
from job_matcher.screening import screen_job

NOW = datetime(2026, 11, 5, tzinfo=UTC)


def job(
    location=None, description="Build backend services.", title="Software Engineer"
):
    return NormalizedJob(
        SourceJobKey("greenhouse", "fixture", "1"),
        "Fixture",
        title,
        description,
        "https://example.com/jobs/1",
        NOW,
        location,
    )


@pytest.mark.parametrize(
    ("location", "description", "expected"),
    [
        # Stored role clauses, copied without customer/company boilerplate.
        (
            "Costa Rica",
            "This role is based in our Costa Rica office in Heredia, with required office days every week.",
            "exclude",
        ),
        ("Bengaluru, India", "Build backend services.", "exclude"),
        ("Remote — India only", "Build backend services.", "exclude"),
        ("Remote — EMEA", "Build backend services.", "exclude"),
        ("Remote — APAC", "Build backend services.", "exclude"),
        ("Remote — LATAM", "Build backend services.", "exclude"),
        ("Remote — South Africa", "Build backend services.", "defer"),
        (
            "Remote - Argentina / India / Mexico",
            "Location: Remote — Mexico, Brazil, Argentina, Eastern Europe, or other international locations",
            "defer",
        ),
        (
            "Ireland",
            "Candidates based in Europe are preferred, while remote candidates able to work regularly during European business hours will also be considered.",
            "defer",
        ),
        (
            "Remote - UK",
            "This position is open to candidates based in the United Kingdom only. Candidates based in the United States or Canada can apply to this posting: Site Reliability Engineer, Infrastructure Platforms - AMER (Intermediate to Senior Staff).",
            "defer",
        ),
        ("Remote — Canada", "Candidates must reside in Canada.", "exclude"),
        ("Remote — United States", "Build backend services.", "keep"),
        (
            "Remote — Worldwide",
            "This role is remote worldwide, including the US.",
            "keep",
        ),
        ("Remote — US/Canada", "Build backend services.", "keep"),
        (
            "Remote — North America",
            "This role is open to applicants in the US and Canada.",
            "keep",
        ),
        ("London / New York", "This position may be based in either office.", "keep"),
        (
            "Amsterdam",
            "This role is based out of our Amsterdam or San Francisco office. We do not offer remote-only roles.",
            "keep",
        ),
        (
            "Montreal, Quebec, Canada",
            "This role is open to multiple locations in North America (excluding CA, NY, & WA).",
            "keep",
        ),
        (
            "Arizona - Remote; Colorado - Remote; New Mexico - Remote; Texas - Remote",
            "Build backend services.",
            "keep",
        ),
        (
            None,
            "Our company headquarters is in Germany. Build backend services.",
            "defer",
        ),
        (None, "Our customers must reside in Germany.", "defer"),
        (
            "Greece",
            "This is a hybrid role at the intersection of engineering, data science, and product.",
            "defer",
        ),
        ("Berlin", "This role has a hybrid office schedule in Berlin.", "exclude"),
        ("India", "You will work US business hours.", "exclude"),
        (
            "Melbourne, Australia",
            "Eligible to obtain and maintain an active United States Secret security clearance.",
            "exclude",
        ),
        (
            "Remote",
            "This role is remote worldwide, except it is not available in the US.",
            "exclude",
        ),
        (
            "Remote — Canada",
            "Candidates must reside in Canada. This role is open to applicants in the US.",
            "defer",
        ),
        (
            "New York",
            "You support customers in Germany and international teammates.",
            "keep",
        ),
        ("India", "This role offers relocation assistance to San Francisco.", "keep"),
        (None, "Build backend services.", "defer"),
        (
            "Remote",
            "Our customers must reside in Germany. Our company has US offices.",
            "defer",
        ),
        ("Remote — India", "This role is remote within India.", "exclude"),
        ("Toronto", "Location: Remote (US / Canada)", "keep"),
        ("Toronto", "Location: Toronto or New York", "keep"),
        ("Bogota, Colombia", "Work from anywhere during US business hours.", "keep"),
        (
            "EMEA",
            "This is a fully remote role, open to candidates in the US or Europe who work full US business hours.",
            "keep",
        ),
        (
            "India",
            "Fully remote work: Work from anywhere in the world within the role's European time zone requirements.",
            "keep",
        ),
        (
            "Berlin, Germany",
            "Rate: 18 USD per hour (rates vary per country. If you are based elsewhere, the rates will be different).",
            "defer",
        ),
        ("Vancouver, Canada", "Remote working location", "defer"),
        (
            "Noida, India",
            "Location: Noida, Uttar Pradesh (office based, covering the US working day). You support US customers and report to a US manager.",
            "exclude",
        ),
        (
            "Barcelona, Spain",
            "This role is onsite in Barcelona. We can support visa sponsorship from any location.",
            "exclude",
        ),
        ("Remote", "Candidates are not required to reside in Germany.", "defer"),
        ("Paris, TX", "Build backend services.", "keep"),
        ("Turkey, Texas", "Build backend services.", "keep"),
        ("Mexico, Missouri", "Build backend services.", "keep"),
        (
            "Lebanon, PA 17042 | 40.319322582 | -76.424455279",
            "Build backend services.",
            "keep",
        ),
        ("North Wales, PA", "Build backend services.", "keep"),
        ("South Jordan, UT", "Build backend services.", "keep"),
        (
            "Jordan Creek, West Des Moines, IA (10178)",
            "Build backend services.",
            "keep",
        ),
        ("Rieti, RI, Italy", "Build backend services.", "exclude"),
        (
            None,
            "What else would you expect from a business with 900 based out of our Malta HQ alone!",
            "defer",
        ),
        ("London, Kentucky", "Build backend services.", "keep"),
        ("London", "Build backend services.", "defer"),
        ("Georgia", "Build backend services.", "defer"),
        ("Baja California, Mexico", "Build backend services.", "exclude"),
        ("India recruiting market", "Build backend services.", "defer"),
        (
            "Remote — Canada",
            "We're a fully remote team spread across North America, which means you can work from wherever you do your best thinking.",
            "defer",
        ),
        (
            "Amsterdam",
            "This role is based in our Amsterdam office. This role is based in our San Francisco office.",
            "defer",
        ),
        ("United States", "Applicants must reside in Germany.", "defer"),
        ("Brazil", "Candidates must reside in Germany.", "defer"),
        ("India", "Relocation is not available to the United States.", "exclude"),
        ("Remote Worldwide (except US)", "Build services.", "exclude"),
        ("Remote", "US work prohibited.", "exclude"),
        ("Remote Worldwide except us", "Build services.", "keep"),
        (None, "This role supports onsite systems deployed in Germany.", "defer"),
        (
            "Lisbon",
            "The base salary range for this position is EUR 50,000 for candidates based in Lisbon.",
            "defer",
        ),
        (
            "Brazil",
            "Remote role. Competitive compensation: For candidates based in Canada, the salary is CAD 140,000.",
            "defer",
        ),
        (
            "Toronto",
            "As such, the location for this role is based in Toronto, Canada, and includes about 30% of traveling across the North American region.",
            "exclude",
        ),
        (
            None,
            "This requirement does not apply to employees working in CA, MN, MT, ND, OK, or where otherwise prohibited by law.",
            "defer",
        ),
        ("US headquarters", "Build services.", "defer"),
        (None, "Work with us.", "defer"),
        (None, "You must work in a team serving clients in Germany.", "defer"),
        (
            "New York",
            "You report to the Director of Product, who is based in Berlin.",
            "keep",
        ),
        (
            None,
            "You report to the Director of Product, who is based in Berlin.",
            "defer",
        ),
        (
            "Austria",
            "Join as an independent contractor for language annotation.",
            "defer",
        ),
        (
            "Remote",
            "This role is remote worldwide. Candidates must reside in Germany.",
            "defer",
        ),
    ],
)
def test_policy(location, description, expected):
    assessment = classify_geography(job(location, description))
    assert assessment.outcome == expected
    assert assessment.reason
    assert assessment.source_retrieved_at == NOW
    if expected == "exclude":
        assert assessment.confidence == "high"
    if expected == "keep":
        assert assessment.us_exceptions


def test_title_alone_needs_corroboration():
    assert classify_geography(job(title="Software Engineer (India)")).outcome == "defer"


def test_raw_provenance_and_alternate_job_locations():
    key = SourceJobKey("lever", "fixture", "2")
    posting = NormalizedJob(
        key,
        "Fixture",
        "Engineer",
        "Build services.",
        "https://example.com/2",
        NOW,
        "Toronto, Canada",
    )
    raw = RawSourceRecord(
        key,
        NOW,
        json.dumps(
            {
                "country": "CA",
                "categories": {"allLocations": ["Toronto, Canada", "New York, NY"]},
            }
        ),
    )
    assert classify_geography(posting, raw).outcome == "keep"
    with pytest.raises(ContractValidationError, match="match key and retrieval"):
        classify_geography(
            posting, RawSourceRecord(key, NOW + timedelta(seconds=1), '{"id":1}')
        )
    with pytest.raises(ContractValidationError, match="match key and retrieval"):
        classify_geography(job(), raw)


def test_employer_offices_do_not_offer_a_worksite():
    posting = job("Bengaluru, India")
    raw = RawSourceRecord(
        posting.key, NOW, json.dumps({"offices": [{"name": "New York"}]})
    )
    assert classify_geography(posting, raw).outcome == "exclude"


def test_generic_remote_country_code_is_not_a_work_boundary():
    key = SourceJobKey("lever", "fixture", "3")
    posting = NormalizedJob(
        key,
        "Fixture",
        "Engineer",
        "Build services.",
        "https://example.com/3",
        NOW,
        "Remote",
    )
    raw = RawSourceRecord(key, NOW, '{"country":"CA","workplaceType":"remote"}')
    assert classify_geography(posting, raw).outcome == "defer"


def test_us_country_conflicting_with_foreign_remote_region():
    key = SourceJobKey("lever", "fixture", "4")
    posting = NormalizedJob(
        key,
        "Fixture",
        "Engineer",
        "Build services.",
        "https://example.com/4",
        NOW,
        "Remote - EMEA",
    )
    raw = RawSourceRecord(key, NOW, '{"country":"US","workplaceType":"remote"}')
    assert classify_geography(posting, raw).outcome == "defer"


def test_deterministic_and_inputs_are_immutable():
    posting = job("India")
    before = repr(posting)
    assert classify_geography(posting) == classify_geography(posting)
    assert repr(posting) == before


def test_zuora_assessment_is_applied_by_the_shared_screen():
    """Stored source identity and role clause exercise the activated classifier."""
    posting = NormalizedJob(
        SourceJobKey("greenhouse", "zuora", "8247021"),
        "Zuora",
        "Technical Account Manager (Costa Rica)",
        "This role is based in our Costa Rica office in Heredia, with required office days every week.",
        "https://boards.greenhouse.io/zuora/jobs/8247021",
        NOW,
        "Costa Rica",
    )
    assert classify_geography(posting).outcome == "exclude"
    linked = LinkedSourceJob(
        posting, SourceJobLink(posting.key, LogicalJobId(UUID(int=1)), NOW)
    )

    decision = screen_job(linked)
    assert decision.outcome == "exclude"
    assert "geo_h:exclude:explicit_foreign_employee_location" in decision.reasons
