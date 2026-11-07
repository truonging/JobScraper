# Structured job metadata

Metadata enrichment is independent of Phase 2 screening version 5. Strategy H,
filter rules, identity resolution and lifecycle reconciliation are unchanged.

## Conservative mapping additions (2026-11-06)

The subsequent coverage audit supplies exact reviewed aliases in
`LEVER_EMPLOYMENT_ADDITIONS`, `GH_EMPLOYMENT_ADDITIONS`, and
`GH_WORKPLACE_ADDITIONS` in the existing `job_metadata.py` helper. Only audit rows
marked `conservative_global_extension_allowed=True` were included. The original
base maps retain their meanings. Extra aliases stay provider- and field-specific:
recognizing a value in Employee Type does not authorize that value in every
employment field. This avoids the existing-value conflicts identified by the
audit. Values still use exact keys after the established space/underscore/hyphen
normalization; punctuation is not indiscriminately stripped or substring-matched.

Greenhouse accepts reviewed singleton or unanimous workplace lists. Different
alternatives or unknown list members remain unresolved, including when another
field supplies a recognizable mode. There is no precedence between Fixed Term
and Full time, and no workplace inference from location strings, descriptions or
remote eligibility booleans. New acquisitions and historical backfill use the
same extraction functions. Timestamp extraction is unchanged.

The authoritative-only improvement backfill uses
`backfill(database, fields=("employment_type", "workplace_type"),
require_unchanged_existing=True)`. Strict mode rolls back if any selected existing
classification would change or become NULL. It never fills `posted_at` in this
mode. All retained postings, including inactive records, are visited.

The improvement task deliberately leaves `job_search.sqlite3` and its sidecars
untouched. Its metadata/cache will remain old until a separately authorized
rebuild. The dashboard does not synchronize automatically; because it validates
the catalog freshness stamp, it can report a stale-index error after the catalog
backfill instead of displaying the changed catalog. This is an expected temporary
limitation. No dashboard startup or search synchronization is required for this
backfill. The future explicit rebuild command is documented below; do not run it
as part of an authoritative-only metadata update.

`NormalizedJob` now carries nullable `employment_type`, `workplace_type` and the
existing aware `posted_at`. Authoritative schema v3 adds only two nullable TEXT
columns; v1/v2 databases migrate explicitly through `initialize_schema()`.
Current raw JSON remains byte-for-byte preserved. There is no historical version
archive: backfill covers every retained current record, including inactive ones.

## Extraction and provenance

`job_metadata.py` owns shared live/backfill extraction. Values are case-folded,
trimmed and normalized for spaces, underscores and hyphens, using explicit maps.

| Input | Normalized value |
| --- | --- |
| Full-time, Full Time Employee, Full-time Employment, Regular Full Time, Clinical Full Time, Full Time Hybrid, Full Time On-Site | full_time |
| Part-time, Clinical Part Time | part_time |
| Intern, Internship | internship |
| Contract, Contractor, Contract Full Time, Fixed Term Contract | contract |
| Temporary, Seasonal, Fixed Term | temporary |
| Other | other |
| Unknown, Unspecified | unknown |
| Remote, Fully Remote | remote (workplace) |
| Hybrid, Partially Remote/Hybrid | hybrid (workplace) |
| Onsite, On-site, On Site, Fully Onsite, In-Office | onsite |

The code maps are the exhaustive list. Unexpected or ambiguous inputs (including
Full Time/Part Time, Regular, Employee and Remote as an employment commitment)
remain NULL, with the original values retained in raw JSON. No title, description
or geographic inference runs.

Lever uses `categories.commitment`, `workplaceType`, and `createdAt`. The last is
numeric Unix **milliseconds**, creation time rather than guaranteed publication
time. Numeric strings, booleans, seconds, nonfinite/zero/negative values are
rejected. Dates must be from 2000 onward and no more than one day after retrieval.

Greenhouse uses only timezone-aware `first_published` for dates, converted to UTC.
`updated_at` is modification time and is never a fallback. The top-level
`employment` is an application employment-history requirement, not job type.

Greenhouse custom metadata uses exact case-insensitive field-name allowlists:

- Employment Type, employmentStatus, Employment Status, Employee Time Type,
  Time Type, Full or Part Time, Full Time/Part Time, Full-time/ Part-time,
  TH: Employment Type, ADP Employment Type.
- Workplace Type, Workplace, Remote Type,
  Job Location Type (Hybrid, onsite, remote), Work Arrangement.

Only recognized scalar values establish facts. Conflicting recognized values
within one posting yield NULL. Generic Job Type, remote-eligibility booleans and
office associations are not reliable equivalents. No extra API calls are made.

## Backfill and rollback

Stop dashboard/acquisition/index users first. Take SQLite `Connection.backup()`
copies of both databases and verify `integrity_check` and `foreign_key_check`.
Preserve the current code, including uncommitted/untracked files. Then explicitly
initialize schema and call `job_matcher.metadata_backfill.backfill(database)`.
Backfill is one atomic transaction: it fills only NULL metadata, preserves valid
existing values and retrieval timestamps, and is safe to retry after interruption.
Existing non-NULL dates retain their original offset representation in SQLite;
indexing and display convert them to UTC. Re-running must enrich zero records.

Rebuild with the supported command:

```powershell
.\.venv\Scripts\python.exe -m job_matcher.search_index rebuild
```

Compare the old/new outcomes, reasons and evidence by full source key, not merely
aggregate counts. Verify all other catalog fields and tables, index membership,
logical aggregation, SQLite/FTS integrity and dashboard behavior before restart.
Historical database/code backups and exploratory migration artifacts were retired
by owner decision. They are no longer a rollback option. The approved mapping
provenance remains at
`private/conservative_metadata_2026-11-06/approved_mapping_rows.csv`; it is not a
runtime dependency. For future changes, any rollback requires a newly prepared
matching database/code recovery set. Reacquisition can fetch current listings,
but cannot reproduce old snapshots, lifecycle timestamps or logical UUIDs.

## Search and dashboard

Search schema v2 stores metadata per source posting, outside FTS text. Employment
and workplace filters must match the same representation as other search filters;
they select search results and never change cached screening outcomes.

Logical jobs retain existing identity/outcome/representative behavior. For date
sorting only, their date is the earliest known posting date among active siblings.
This describes first known publication/creation, not a selected provider's newest
repost. Unknown dates sort last in both directions, UUID breaks ties. Conflicting
employment/workplace facts are not collapsed: every source value is displayed.
Inactive-only logical jobs have no active posting date.

The default discovery sort, pagination and existing filters remain. Dashboard
posting dates are human-readable UTC dates; Lever dates are labeled creation time.
Discovery timestamps retain their separate existing PST labels.

Metadata is included in the lightweight index metadata hash, not the screening
input hash or FTS text hash. A normalized metadata-only sync therefore performs
zero screening/FTS work. The conservative code fingerprint invalidates the old
implementation once; the rebuild re-evaluates outcomes under unchanged policy
version 5. Changes to raw payloads still conservatively invalidate screening.
