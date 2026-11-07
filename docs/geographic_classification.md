# Strategy H US work-eligibility screening

`job_matcher.geographic_classification.classify_geography(job, raw=None)` is a
pure, source-local assessment. **Production `screen_job` applies its enforcement
adapter as the sole geographic authority (screening version 5).**
It reads a `NormalizedJob` and optional matching `RawSourceRecord`, returning a
frozen `GeographicAssessment`. No acquisition, database, clock, cache, or AI calls
occur. Mismatched source identity/retrieval timestamps raise
`ContractValidationError`.

The result contains `outcome`, `reason`, `confidence`, source identity/retrieval,
`evidence`, `us_exceptions`, `conflicts`, recognized `countries`/`regions`, and
structured `work_mode`, plus `foreign_restrictions` (employee clauses) and
`uncertainty_details` (diagnostic codes). Confidence describes evidence quality; it is not a
calibrated probability. Evidence identifies normalized location, raw country,
or the relevant description clause. A KEEP means geographic eligibility only.

## Decision order

1. Collect source-local work locations, employee requirements, US prohibitions,
   US office/remote options, worldwide work permission, and relocation **to** a
   recognized US location. Lever `categories.allLocations` are job options;
   Greenhouse employer `offices` are not. ISO codes are read only from Lever's
   `country` field.
2. Conflicting employee requirements/options, raw-country/work-location
   conflicts, and contradictory US title/foreign worksite evidence yield DEFER.
   An unconditional employee US work-authorization/US employment-eligibility
   requirement also yields DEFER when it conflicts with a foreign worksite
   inference. It is uncertainty evidence, not a US work-location permission.
   Explicit mandatory foreign residence, physical office/work arrangements,
   country-only remote boundaries and US employment prohibitions remain
   enforceable when authorization is the only US-related evidence. Actual US
   work options continue through the existing conflict/permission rules.
3. Explicit US employment prohibition yields EXCLUDE. Credible US/worldwide
   permission yields KEEP. An unbounded remote-country label paired with
   rates for workers in other countries yields DEFER rather than a country-only
   exclusion. Explicit foreign employee work requirements yield
   EXCLUDE. Conflicts between those signals were handled in step 2.
4. Remote policies undermining a country default, semantic hybrid roles,
   headquarters/recruiting-market fields, and possible country-market tags on
   contribution/freelance postings yield DEFER.
5. Explicitly bounded foreign remote labels (including remote EMEA/APAC/LATAM)
   yield EXCLUDE. Other foreign tags with unclear remote boundaries yield DEFER.
6. A named foreign country in an actual structured work-location field yields
   EXCLUDE without requiring duplicate prose. A foreign city corroborated by a
   matching source country also yields EXCLUDE. City-only namesakes, region-only
   fields, title-only geography, missing locations, and unknown evidence yield
   DEFER.

Customers, managers, teams, employer office lists, clearance, business hours,
legal conditions, travel and benefits do not establish employee eligibility.
The authorization conflict rule recognizes mandatory applicant requirements,
role-specific requirements, continued-employment conditions and unconditional
all-hire US eligibility verification. Conditional “for US-based roles” text,
US-applicant notices, E-Verify descriptions and export-control qualifications
do not independently establish a conflict. Inline employee requirement bullets
are scoped independently of the preceding duties. These are finite English
patterns, not a general legal or grammar parser.
The bounded vocabulary in `geography_terms.py` is intentionally not a geocoder.
US states shield names such as New Mexico and Turkey, Texas. A remote country
label without a clear restriction is intentionally less conclusive than a
normal named work location.

## Reason codes

| Code | Outcome | Confidence |
| --- | --- | --- |
| `conflicting_location_evidence` | DEFER | medium |
| `US_employment_prohibited` | EXCLUDE | high |
| `US_or_worldwide_work_option` | KEEP | high |
| `explicit_foreign_employee_location` | EXCLUDE | high |
| `remote_policy_or_role_ambiguity` | DEFER | low |
| `unreliable_location_semantics` | DEFER | low |
| `possible_country_market_tag` | DEFER | low |
| `structured_foreign_remote_boundary` | EXCLUDE | high |
| `foreign_tag_with_unclear_remote_boundary` | DEFER | medium |
| `structured_foreign_work_location` | EXCLUDE | high |
| `corroborated_foreign_city` | EXCLUDE | high |
| `insufficient_location_confidence` | DEFER | medium |
| `unknown_work_eligibility` | DEFER | low |

Additional uncertainty reasons are `work_clock_not_residence_boundary` and
`unresolved_Americas_work_option`. A remote time-zone label is not a residence
boundary. AMER/Americas must not be treated as foreign-only because another
location alternative names EMEA. Background-check exemptions and conditional
office attendance for people already living nearby do not mandate residence.
An explicitly remote opening considering candidates in any location supplies a
worldwide option; a bounded "any location within Canada" does not.
Explicit US commuting-distance requirements and a role's own US/Canada remote
option after a reporting-manager prelude are also retained as job-specific US
evidence. A manager's location, customer commuting requirements and background-
check exemptions remain incidental. Ordinary background-check requirements do
not erase an actual work-location requirement or permission in the same clause.
`US_work_authorization_vs_foreign_work_location` is a conflict detail under
`conflicting_location_evidence` (medium confidence). The authorization clause
is retained in evidence, without being added to `us_exceptions`. Its REVIEW_ONLY
action preserves the non-geographic Phase 2 outcome, including an occupational
or deterministic EXCLUDE.

An assessment DEFER means uncertain geographic evidence, **not a proposed final
Phase 2 DEFER**. English grammar and a finite vocabulary cannot establish
population recall. Explicit requirements and structured inference remain
distinct reasons.

## Independent assessment-to-enforcement adapter

`job_matcher.geographic_enforcement.geographic_enforcement(assessment)` returns a
frozen `GeographicEnforcement`, retaining the complete source-local assessment:

| Action | Meaning | Effect on the existing effective source outcome |
| --- | --- | --- |
| `EXCLUDE` | High-confidence, validated foreign restriction with no US exception, conflict or uncertainty | EXCLUDE |
| `PASS` | Geographic eligibility supports US/worldwide work | Preserve existing outcome |
| `REVIEW_ONLY` | Uncertain/conflicting evidence or an unvalidated exclusion reason | Preserve existing outcome |

Only `US_employment_prohibited`, `explicit_foreign_employee_location`,
`structured_foreign_work_location`, `structured_foreign_remote_boundary` and
`corroborated_foreign_city` can enforce EXCLUDE. High confidence alone is not
sufficient. Future exclusion reason codes default to REVIEW_ONLY until validated.

`apply_geographic_enforcement(existing_outcome, enforcement)` applies this rule
without modifying a decision object, source facts or storage. Pass the existing
**non-geographic outcome for the same posting**, after deterministic/occupational
and description checks. Call before logical aggregation. REVIEW_ONLY never
turns KEEP into DEFER. PASS never restores an existing EXCLUDE or clears an
insufficient-description DEFER. Old cached geographic exclusions are not inputs
to the adapter: every source posting is reevaluated from its source facts.

There is no review workflow or new persistent store. The assessment and its
source-key/retrieval provenance are available to callers for debugging and later
review. The independent classifier validates matching normalized/raw provenance.

## Production integration and rebuilding the search cache

The readiness audit additionally found remote country labels paired with
opening-specific rates for workers in other countries. Those labels alone do
not establish a foreign-only boundary. When the only employee-location clauses
are unbounded remote labels, country-dependent rates produce REVIEW_ONLY.
Mandatory foreign residence, office requirements, explicit country-only
boundaries and US employment prohibitions remain enforceable. This correction
does not establish US eligibility; it preserves uncertainty.

US namesake disambiguation also accepts a five-digit ZIP code (and optional
four-digit extension) after the full state name, such as Jamaica, New York
11412. A foreign country-shaped city name does not override that US worksite.
The observed joined spelling NewYork is normalized for this same recognition.
An invitation to apply for this specific role outside its primary location is
recorded as uncertainty, unless a separate explicit employee country restriction
still establishes ineligibility. It does not itself establish a US work option.

`screen_job` computes the non-geographic outcome, calls H for the same normalized/raw
source, and clamps only validated EXCLUDE. Both `screen_catalog` and the search
index builder already use that shared path. Deterministic rejection still
overrides in their existing orchestration. PASS and REVIEW_ONLY preserve the
non-geographic outcome, including occupational/demo exclusions and insufficient-
description DEFER. The `geography` enum is derived only from H's action:
EXCLUDE maps to `foreign_only`, PASS to `us_remote` for remote work or `us`, and
REVIEW_ONLY to `uncertain`. It is a compatibility summary for existing reports;
the detailed `geo_h.*` evidence explains the assessment and enforcement.
Active logical aggregation remains KEEP > DEFER > EXCLUDE. No evidence is
combined across source representations. The Lever inline geography classifier
and `greenhouse_geography.py` have been removed. Country and workplace parsing
needed by H remains in H; source normalization, search and display are unchanged.

The version 1 TOML schema retains its required `[location]` section, but both
arrays must be empty. Nonempty location rules raise `FilterPolicyError`; the
deterministic evaluator no longer matches location text. This keeps the existing
policy contract without allowing a second geographic enforcement mechanism.
Title, description, company and occupational rules remain effective regardless
of H PASS or REVIEW_ONLY. Credential exclusions are non-geographic requirements.

Reasons append `geo_h:<action>:<reason>`. Existing evidence serialization stores
`geo_h.action`, assessment, reason, confidence, work mode, matched location
evidence, countries/regions, US exceptions, foreign restrictions, conflicts and
uncertainty details. Existing source identity/retrieval provenance is retained.
Local imports in `screen_job` avoid a cycle with H's screening-contract imports.
There is no feature flag, shadow system, review database or new schema.

`SCREENING_VERSION` is `5`. The implementation fingerprint includes
`geographic_classification.py`, `geography_terms.py` and
`geographic_enforcement.py`, so cached decisions cannot remain silently stale
when H changes. The authoritative acquisition catalog is never reset to refresh
derived screening. The existing rebuild command recreates the complete FTS and
screening cache from that catalog:

```powershell
.\.venv\Scripts\python.exe -m job_matcher.search_index rebuild --catalog phase2_validation.sqlite3 --index job_search.sqlite3 --policy examples/filter_policy.example.toml
```

For an intentional delete/recreate, first stop index readers/writers, verify the
catalog and reconstructability, and take a consistent SQLite online backup of
the old search database outside its deletion target. Delete only the verified
search main/WAL/SHM files with all connections closed; never delete the catalog.
The existing builder creates a temporary database, checks it, and publishes it
atomically. Future ordinary refreshes can use the existing `sync` command.
Rollback requires restoring both the matching old screening code/fingerprint
and a consistent old index backup. Do not restore only the index under new code
or roll back authoritative acquisition data.
