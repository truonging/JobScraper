"""Reusable, source-local US work-eligibility assessment for Phase 2 screening.

The shared screen applies the enforcement adapter to this derived evidence.
Assessment uncertainty does not replace deterministic or occupational decisions.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from job_matcher.geography_terms import (
    COUNTRIES,
    ISO_COUNTRIES,
    Places,
    places,
    starts_with_place,
)
from job_matcher.models import (
    ContractValidationError,
    NormalizedJob,
    RawSourceRecord,
    SourceJobKey,
)
from job_matcher.screening import Evidence, ScreeningOutcome


class LocationConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class GeographicAssessment:
    source_key: SourceJobKey
    source_retrieved_at: datetime
    outcome: ScreeningOutcome
    reason: str
    confidence: LocationConfidence
    evidence: tuple[Evidence, ...]
    us_exceptions: tuple[Evidence, ...]
    conflicts: tuple[str, ...]
    countries: tuple[str, ...]
    regions: tuple[str, ...]
    work_mode: str
    foreign_restrictions: tuple[Evidence, ...] = ()
    uncertainty_details: tuple[str, ...] = ()


_REMOTE = re.compile(r"\bremote\b|work (?:from|at) home|home.based", re.I)
_WORLD = re.compile(
    r"\b(?:worldwide|globally remote|global remote|anywhere in the world|"
    r"work from anywhere|work from any country|remote anywhere)\b",
    re.I,
)
_NORTH = re.compile(r"\bNorth America(?:n)?\b", re.I)
_CLOCK = re.compile(r"\btime[ -]?zones?\b|\bbusiness hours\b|\bUTC[+-]", re.I)
_INCIDENTAL = re.compile(
    r"\b(?:customers?|clients?|headquarters?|partners?|teammates?|colleagues|"
    r"travel\w*|benefits?|equal.opportunity|privacy|discriminat\w*)\b",
    re.I,
)
_LEGAL = re.compile(
    r"equal.opportunity|discriminat|privacy|reasonable accommodation|"
    r"pursuant to|export.control|for (?:roles|positions|applicants) (?:based|located) in|"
    r"if you (?:reside|live)|depending on your (?:role|location)|"
    r"prohibited by law|this requirement does not apply|"
    r"salary|compensation|pay (?:range|band)|gross annually|hourly rate|"
    r"excluded from (?:undergoing )?background checks?|exempt (?:from )?background checks?",
    re.I,
)
_ACTOR = re.compile(
    r"\b(?:(?:this|the|your) (?:role|position|job)|you(?:'ll| will| must| can)?|"
    r"candidates?|applicants?|employees?|successful candidate)\b",
    re.I,
)
_LABEL = re.compile(
    r"^(?:work(?:ing)? location|location|where you(?: will)? work|eligible locations|"
    r"eligible countries|remote eligibility|work arrangement|workplace)\s*:",
    re.I,
)
_BASE = re.compile(
    r"\b(?:based|located|reside|residing|live|work(?:ing)?|performed)\s+"
    r"(?:(?:remotely|anywhere|only|exclusively)\s+)*(?:in|within|from|at|out of)\s+"
    r"(?:(?:the|our|either|an?)\s+)*",
    re.I,
)
_MUST = re.compile(
    r"\b(?:must|need to|required to|have to)\s+(?:currently\s+)?"
    r"(?:be\s+(?:(?:physically|currently)\s+)?(?:based|located|resident|residing)|"
    r"reside|live|work)\s+(?:in|within|from)\s+(?:the\s+)?",
    re.I,
)
_REMOTE_IN = re.compile(
    r"\b(?:remote\s+(?:(?:work|working)\s+)?(?:(?:only|exclusively)\s+)?"
    r"(?:in|within|from)|eligible\s+anywhere\s+(?:within|in)|"
    r"(?:only|exclusively)\s+(?:open|available) to (?:candidates|applicants) (?:in|from))\s+(?:the\s+)?",
    re.I,
)
_OFFER = re.compile(
    r"\b(?:open to|available to|accepting|hiring (?:for this role )?(?:across|in)|"
    r"eligible (?:locations|countries)|remote)\s*[:(—–-]?\s*"
    r"(?:(?:candidates|applicants|residents|people)\s+)?"
    r"(?:(?:based|located|living|residing)\s+)?(?:(?:in|from|across)\s+)?(?:the\s+)?",
    re.I,
)
_PROHIBIT = re.compile(
    r"\b(?:not (?:open|available|eligible)(?:\s+(?:for|to|in))?|"
    r"cannot (?:work|be based) (?:in|from)|do not (?:hire|accept)|"
    r"(?:excluding|except|excludes)|no (?:candidates|applicants) (?:in|from))"
    r"\s+(?:(?:candidates|applicants|residents)\s+)?(?:(?:in|from)\s+)?(?:the\s+)?"
    r"(?:United States|USA|(?-i:US))(?!\w)|"
    r"\b(?:(?-i:US)|United States) (?:work|employment|applicants?) (?:is |are )?(?:prohibited|not (?:permitted|allowed|eligible))\b",
    re.I,
)
_RELOCATE = re.compile(
    r"\brelocat(?:e|ing|ion)(?:\s+[\w'-]+){0,8}?\s+to\s+(?:the\s+)?", re.I
)
_NEG = re.compile(r"\b(?:not|never|no|cannot|can't|without)\b", re.I)
_SEMANTIC = re.compile(
    r"\bhybrid (?:role (?:at the intersection|combining|blending)|"
    r"engineering|data.science|cloud|architecture)",
    re.I,
)
_OFFICE_MODE = re.compile(
    r"\bon[ -]?site\b|\bhybrid (?:work(?:ing)? (?:model|schedule|arrangement)|"
    r"schedule|arrangement|office)\b|\boffice.based\b|"
    r"required office days|office.{0,35}days (?:per|a|every|each) week",
    re.I,
)
_UNCERTAIN_FIELD = re.compile(
    r"\b(?:headquarters?|HQ|recruiting|market|territory|any city|any location)\b", re.I
)
_SOFT = re.compile(
    r"\b(?:prefer(?:red|ably|ence)?|should|would like|future|after graduation)\b", re.I
)
_LOCATION_BRIDGE = re.compile(
    r"\s*(?:(?:is|are|will|must|can|may|be|currently|physically|primarily|remotely|"
    r"expected|required|to|only|also|able|need|needs|have|has)\s+)*$",
    re.I,
)
_WORK_SITE = re.compile(
    r"\b(?:hybrid|on[ -]?site)\s+(?:(?:work|working|office)\s+)?"
    r"(?:schedule|arrangement|model|role|position|job)\s+(?:in|at)\s+"
    r"|\b(?:role|position|job)\s+(?:is|will be)\s+(?:fully\s+)?"
    r"(?:on[ -]?site|office.based)\s+(?:in|at)\s+",
    re.I,
)
_ANY_CANDIDATES = re.compile(
    r"\b(?:consider|accept|welcome|hire) (?:remote )?(?:candidates|applicants) "
    r"(?:based |located )?(?:in|from) any (?:location|country)\b",
    re.I,
)
_CONDITIONAL_ATTENDANCE = re.compile(
    r"\b(?:candidates|applicants|employees) (?:currently )?(?:residing|based|located) in "
    r"[^.!?]{0,100}\b(?:must|will|are required to) (?:attend|visit|come to)\b",
    re.I,
)
_ROLE_REMOTE_OPTION = re.compile(
    r"\b(?:this|the|your) (?:role|position|job)\b[^.!?]{0,150}?"
    r"\b(?:is|will be|can be|may be)\s+(?:fully\s+)?remote\s+"
    r"(?:across|within|in|from)\s+(?:the\s+)?",
    re.I,
)
_COMMUTE = re.compile(
    r"\b(?:reside|live|be based|be located)\s+within\s+(?:a\s+)?"
    r"(?:reasonable\s+)?commut(?:ing|able)\s+distance\s+(?:of|from|to)\s+(?:our\s+)?",
    re.I,
)
_COUNTRY_RATES = re.compile(
    r"\brates (?:vary|differ).{0,40}country\b|\bif you are based elsewhere\b",
    re.I,
)
_US_AUTH_REQUIREMENT = re.compile(
    r"(?:\b(?:applicants?|candidates?|you|successful candidate)\s+"
    r"(?:must|need to|are required to)\s+|(?:^|[•●])\s*must\s+)"
    r"(?:currently\s+)?(?:be\s+)?(?:(?:legally|currently|permanently)\s+)*"
    r"(?:authorized|authorised|eligible)\s+"
    r"(?:to work(?: for any employer)?|for employment)\s+in\s+(?:the\s+)?"
    r"(?:United States|USA|(?-i:US))\b|"
    r"\b(?:(?:this|the) (?:role|position|job) requires|"
    r"(?:applicants?|candidates?|you) (?:must have|need|are required to have)|^must have)\s+"
    r"(?:(?:valid|permanent|current|unrestricted|legal)\s+)*"
    r"(?:United States|USA|(?-i:US)) (?:work authorization|work authorisation|employment eligibility)\b|"
    r"\b(?:continued )?employment in this (?:position|role|job) is contingent "
    r"(?:on|upon) (?:obtaining|having) (?:valid )?"
    r"(?:United States|USA|(?-i:US)) work authori[sz]ation\b|"
    r"\ball (?:persons|employees) hired (?:will be|are) required to verify "
    r"(?:identity and )?eligibility to work in (?:the )?(?:United States|USA|(?-i:US))\b",
    re.I,
)


def _authorization_requirements(clause: str):
    """Employee requirements, excluding conditional US-applicant boilerplate."""
    for match in _US_AUTH_REQUIREMENT.finditer(clause):
        prefix = (
            ""
            if match.group().lstrip().startswith(("•", "●"))
            else clause[: match.start()]
        )
        if (
            _INCIDENTAL.search(prefix)
            or _NEG.search(prefix)
            or re.search(
                r"\b(?:if|when|for)\b.{0,70}\b(?:US|United States|USA)\b|"
                r"^(?:US|United States|USA)[ -](?:based )?(?:applicants?|candidates?|roles?|positions?)\b",
                prefix,
                re.I,
            )
        ):
            continue
        yield match


def _clauses(description: str):
    for line in description.replace("U.S.", "United States").splitlines():
        for clause in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            clause = " ".join(clause.strip(" -•\t").split())
            if clause:
                yield clause


def _target(value: str) -> str:
    # Restrict the evidence window to the employee's place, not their customers,
    # business hours, colleagues, or a subsequent independent clause.
    return re.split(
        r"[;.!?]|\b(?:office|serving|supporting|collaborat\w*|reporting|customers?|"
        r"clients?|with|for our|to serve|business hours|time zones?|travel\w*|"
        r"and includes|and involves)\b",
        value,
        flags=re.I,
    )[0][:160]


def _immediate(value: str) -> Places | None:
    target = _target(value)
    # Unparsed intervening duties cannot turn a later country into a worksite.
    found = places(target)
    if not starts_with_place(target) or not (
        found.us
        or found.countries
        or found.regions
        or found.city_countries
        or _NORTH.search(target)
    ):
        return None
    if re.search(
        r"\b(?:hours|time.?zone|customers?|clients?|team|business|authorization|clearance)\b",
        target,
        re.I,
    ):
        return None
    return found


def classify_geography(
    job: NormalizedJob, raw: RawSourceRecord | None = None
) -> GeographicAssessment:
    """Assess one posting without side effects or cross-representation evidence.

    Confidence describes the decisive location evidence, not a calibrated
    probability of correctness. Unknown or conflicting eligibility is DEFER.
    """
    if raw is not None and (raw.key != job.key or raw.retrieved_at != job.retrieved_at):
        raise ContractValidationError(
            "geography raw evidence must match key and retrieval"
        )
    payload = json.loads(raw.payload_json) if raw else {}
    locations = [job.location] if job.location else []
    iso = workplace = None
    if job.key.source == "lever":
        iso = payload.get("country")
        iso = iso.upper() if isinstance(iso, str) else None
        workplace = payload.get("workplaceType")
        categories = payload.get("categories")
        alternates = (
            categories.get("allLocations", []) if isinstance(categories, dict) else []
        )
        if isinstance(alternates, list):
            locations.extend(x for x in alternates if isinstance(x, str) and x.strip())
    # Greenhouse offices are employer associations, not offered work locations.
    locations = list(dict.fromkeys(locations))
    field = " ; ".join(locations)
    structured = places(field)
    title_qualifier = " ; ".join(
        re.findall(r"\(([^()]*)\)", job.title) + re.split(r"\s[-—–|]\s", job.title)[1:]
    )
    title_places = places(title_qualifier)
    mode = (
        "remote"
        if _REMOTE.search(field) or workplace == "remote"
        else "hybrid"
        if re.search(r"\bhybrid\b", field, re.I) or workplace == "hybrid"
        else "onsite"
        if _OFFICE_MODE.search(field) or workplace == "onsite"
        else "unspecified"
    )
    evidence = [Evidence("location", x) for x in locations]
    if iso:
        evidence.append(Evidence("raw.country", iso))
    if isinstance(workplace, str):
        evidence.append(Evidence("raw.workplaceType", workplace))
    if title_places.us or title_places.countries or title_places.city_countries:
        evidence.append(Evidence("title.qualifier", title_qualifier))
    positive, foreign, prohibited, ambiguity, authorization = [], [], [], [], []
    foreign_sets = []
    fixed_us = []
    for clause in _clauses(job.description):
        if _LEGAL.search(clause):
            continue
        actor, label = _ACTOR.search(clause), _LABEL.search(clause)
        incidental = _INCIDENTAL.search(clause)
        context = bool(
            actor
            or label
            or re.match(r"^(?:Remote|Work from|Must |Eligible )", clause, re.I)
        )
        ev = Evidence("description", clause)
        auth_matches = tuple(_authorization_requirements(clause))
        if auth_matches:
            authorization.append(ev)
        # A reporting-manager prelude does not change the subject of a later
        # "the role ... and is remote across the US" predicate. Relative
        # clauses about the manager remain outside this narrow protection.
        for option in _ROLE_REMOTE_OPTION.finditer(clause):
            if _NEG.search(option.group()) or re.search(
                r"\b(?:who|whose|which|that)\b", option.group(), re.I
            ):
                continue
            target = _immediate(clause[option.end() :])
            if (
                target
                and (target.us or _NORTH.search(_target(clause[option.end() :])))
                and not _PROHIBIT.search(clause)
            ):
                positive.append(ev)
        for commute in _COMMUTE.finditer(clause):
            prefix = clause[: commute.start()]
            subjects = list(_ACTOR.finditer(prefix))
            scoped = prefix[subjects[-1].start() :] if subjects else prefix
            if not context or _INCIDENTAL.search(scoped) or _NEG.search(scoped[-35:]):
                continue
            target = _immediate(clause[commute.end() :])
            if target and target.us and not _PROHIBIT.search(clause):
                positive.append(ev)
        if _CONDITIONAL_ATTENDANCE.search(clause):
            ambiguity.append(ev)
            continue
        any_candidates = _ANY_CANDIDATES.search(clause)
        if any_candidates and (mode == "remote" or _REMOTE.search(clause)):
            tail = clause[any_candidates.end() :]
            bounded = re.search(r"^\s+(?:in|within|across)\s+", tail, re.I)
            if (
                not bounded
                and not _PROHIBIT.search(clause)
                and not _NEG.search(clause[: any_candidates.start()][-35:])
            ):
                positive.append(ev)
            elif bounded and not _NEG.search(clause[: any_candidates.start()][-35:]):
                target = _immediate(tail[bounded.end() :])
                if target and target.us:
                    positive.append(ev)
                elif target and (target.countries or target.regions):
                    foreign.append(ev)
                    foreign_sets.append(set(target.countries or target.regions))
        if _SEMANTIC.search(clause):
            ambiguity.append(ev)
        if re.search(
            r"rates (?:vary|differ).{0,40}country|if you are based elsewhere|work from wherever|remote working location|"
            r"work where you.{0,25}(?:productive|best)|"
            r"if you are applying for this role outside of the primary location",
            clause,
            re.I,
        ):
            ambiguity.append(ev)
        if (
            (
                context
                or re.match(
                    r"^(?:US|United States) (?:work|employment|applicants?)", clause
                )
            )
            and _PROHIBIT.search(clause)
            and not (
                incidental and incidental.start() < _PROHIBIT.search(clause).start()
            )
        ):
            prohibited.append(ev)
        if _WORLD.search(clause) and (
            _REMOTE.search(clause) or re.search(r"work from", clause, re.I)
        ):
            bounded = re.search(r"anywhere (?:in|within)\s+(?!the world)", clause, re.I)
            limited = re.search(
                r"\b(?:days per year|days of the year|up to|\d+ days)\b", clause, re.I
            )
            if (
                not bounded
                and not limited
                and not _PROHIBIT.search(clause)
                and not _NEG.search(clause[: _WORLD.search(clause).start()][-35:])
            ):
                if context and not incidental:
                    positive.append(ev)
                else:
                    ambiguity.append(ev)
        for relocation in _RELOCATE.finditer(clause):
            if not _NEG.search(relocation.group()) and not _NEG.search(
                clause[max(0, relocation.start() - 35) : relocation.start()]
            ):
                target = _immediate(clause[relocation.end() :])
                if target and target.us:
                    positive.append(ev)
        if not context:
            continue
        if label:
            option = places(_target(clause[label.end() :]))
            if (
                option.us or _NORTH.search(clause[label.end() :])
            ) and not _PROHIBIT.search(clause):
                positive.append(ev)
        for pattern in (_BASE, _MUST, _REMOTE_IN, _OFFER):
            for match in pattern.finditer(clause):
                # Legal permission to work is not a physical US work option.
                if any(a.start() <= match.start() < a.end() for a in auth_matches):
                    continue
                prefix = clause[: match.start()]
                if (
                    _NEG.search(prefix[-35:])
                    or _SOFT.search(prefix)
                    or _INCIDENTAL.search(prefix)
                    or re.search(
                        r"\b(?:who|whose|manager|director|team|department)\b",
                        prefix,
                        re.I,
                    )
                ):
                    continue
                if pattern is _BASE and not label:
                    subjects = list(_ACTOR.finditer(prefix))
                    if not subjects or not _LOCATION_BRIDGE.fullmatch(
                        prefix[subjects[-1].end() :]
                    ):
                        continue
                if pattern is _OFFER and not label and incidental:
                    continue
                target_text = _target(clause[match.end() :])
                target = _immediate(clause[match.end() :])
                if not target:
                    continue
                north = bool(_NORTH.search(target_text))
                if (target.us or north) and not _PROHIBIT.search(clause):
                    positive.append(ev)
                    if pattern is _BASE and not re.search(
                        r"\b(?:can|may|either|or)\b", clause, re.I
                    ):
                        fixed_us.append(ev)
                elif target.countries or target.regions or target.city_countries:
                    if _SOFT.search(clause) or re.search(
                        r"\b(?:or other|and other|other eligible) (?:international|global|remote) (?:locations|countries)\b",
                        clause,
                        re.I,
                    ):
                        ambiguity.append(ev)
                        continue
                    if pattern is _OFFER and not _REMOTE.search(clause):
                        continue
                    foreign.append(ev)
                    foreign_sets.append(
                        set(target.countries or target.city_countries or target.regions)
                    )
        if label and _OFFICE_MODE.search(clause) and not _SEMANTIC.search(clause):
            target = places(clause)
            if not target.us and (target.countries or target.city_countries):
                foreign.append(ev)
                foreign_sets.append(set(target.countries or target.city_countries))
        for work_site in _WORK_SITE.finditer(clause):
            if (
                incidental
                or _SEMANTIC.search(clause)
                or _NEG.search(clause[: work_site.start()][-35:])
            ):
                continue
            target = _immediate(clause[work_site.end() :])
            if target and target.us:
                positive.append(ev)
            elif target and (target.countries or target.city_countries):
                foreign.append(ev)
                foreign_sets.append(set(target.countries or target.city_countries))
    positive = list(dict.fromkeys(positive))
    foreign = list(dict.fromkeys(foreign))
    prohibited = list(dict.fromkeys(prohibited))
    evidence.extend(dict.fromkeys(foreign + prohibited + ambiguity))
    field_clock = bool(_CLOCK.search(field))
    field_prohibited = bool(_PROHIBIT.search(field) and not field_clock)
    if field_prohibited:
        prohibited.append(Evidence("location", field))
    field_reliable = not _UNCERTAIN_FIELD.search(field)
    field_us = bool(
        field_reliable
        and not field_prohibited
        and not field_clock
        and (structured.us or _NORTH.search(field) or iso == "US")
    )
    field_world = bool(
        field_reliable
        and not field_prohibited
        and (
            _WORLD.search(field)
            or (re.search(r"\b(?:global|anywhere)\b", field, re.I) and mode == "remote")
        )
    )
    if field_us or field_world:
        positive.insert(0, Evidence("location", field or f"country={iso}"))
    conflicts = []
    iso_conflict = bool(
        (
            iso == "US"
            and (
                structured.countries or structured.regions or structured.city_countries
            )
        )
        or (iso in ISO_COUNTRIES and structured.us and not structured.countries)
    )
    if iso_conflict:
        conflicts.append("raw_country_vs_work_location")
    foreign_worksite = bool(
        foreign
        or structured.countries
        or structured.regions
        or (structured.city_countries and iso in ISO_COUNTRIES)
    )
    definitive_foreign = bool(
        any(
            _MUST.search(ev.value)
            or _OFFICE_MODE.search(ev.value)
            or (
                _REMOTE.search(ev.value)
                and re.search(
                    r"\b(?:only|exclusively|within|restricted to)\b", ev.value, re.I
                )
            )
            for ev in foreign
        )
        or (
            (structured.countries or structured.regions)
            and _REMOTE.search(field)
            and re.search(r"\b(?:only|restricted to|within)\b", field, re.I)
        )
    )
    if (
        authorization
        and foreign_worksite
        and not positive
        and not prohibited
        and not field_us
        and not field_world
        and not definitive_foreign
    ):
        conflicts.append("US_work_authorization_vs_foreign_work_location")
        evidence.extend(authorization)
    if prohibited and positive:
        conflicts.append("US_permission_vs_prohibition")
    if foreign and positive:
        conflicts.append("employee_location_requirements_vs_US_option")
    if (
        positive
        and not field_us
        and not field_world
        and not field_clock
        and (structured.countries or structured.regions)
        and re.search(r"\b(?:only|restricted to|within)\b", field, re.I)
    ):
        conflicts.append("bounded_foreign_field_vs_US_option")
    if (
        title_places.us
        and not field_us
        and (structured.countries or structured.regions)
    ):
        conflicts.append("US_title_vs_foreign_location")
    if fixed_us and foreign_sets:
        conflicts.append("contradictory_fixed_offices")
    names = set().union(*foreign_sets)
    canonical_countries = set(COUNTRIES.values())
    if (
        structured.countries
        and names & canonical_countries
        and not set(structured.countries) & names
    ):
        conflicts.append("structured_vs_employee_country")
    inferred = {ISO_COUNTRIES[iso]} if iso in ISO_COUNTRIES else set()
    countries = tuple(
        sorted(set(structured.countries) | (names & canonical_countries) | inferred)
    )
    regions = tuple(sorted(set(structured.regions) | (names - canonical_countries)))

    def result(outcome, reason, confidence):
        details = ()
        if outcome is ScreeningOutcome.DEFER:
            details = tuple(dict.fromkeys((reason, *conflicts)))
        return GeographicAssessment(
            job.key,
            job.retrieved_at,
            outcome,
            reason,
            confidence,
            tuple(dict.fromkeys(evidence)),
            tuple(dict.fromkeys(positive)),
            tuple(conflicts),
            countries,
            regions,
            mode,
            tuple(foreign + prohibited),
            details,
        )

    if conflicts:
        return result(
            ScreeningOutcome.DEFER,
            "conflicting_location_evidence",
            LocationConfidence.MEDIUM,
        )
    if prohibited:
        return result(
            ScreeningOutcome.EXCLUDE,
            "US_employment_prohibited",
            LocationConfidence.HIGH,
        )
    if positive:
        return result(
            ScreeningOutcome.KEEP,
            "US_or_worldwide_work_option",
            LocationConfidence.HIGH,
        )
    # A remote location label is not a confirmed country boundary when this
    # opening also quotes rates for workers elsewhere. Keep genuine mandatory
    # residence, office, and foreign-only restrictions independently effective.
    if (
        foreign
        and any(_COUNTRY_RATES.search(ev.value) for ev in ambiguity)
        and not re.search(r"\b(?:only|restricted to|within)\b", field, re.I)
        and all(
            _REMOTE.search(ev.value)
            and not re.search(
                r"\b(?:must|need|needs|required|only|exclusively|restricted|office)\b",
                ev.value,
                re.I,
            )
            for ev in foreign
        )
    ):
        return result(
            ScreeningOutcome.DEFER,
            "remote_policy_or_role_ambiguity",
            LocationConfidence.LOW,
        )
    if foreign:
        return result(
            ScreeningOutcome.EXCLUDE,
            "explicit_foreign_employee_location",
            LocationConfidence.HIGH,
        )
    if ambiguity:
        return result(
            ScreeningOutcome.DEFER,
            "remote_policy_or_role_ambiguity",
            LocationConfidence.LOW,
        )
    # Clock overlap does not establish physical residence. AMER is not a
    # foreign-only region: a mixed AMER/EMEA label may include US employment.
    # Direct employee restrictions above remain independently enforceable.
    if field_clock:
        return result(
            ScreeningOutcome.DEFER,
            "work_clock_not_residence_boundary",
            LocationConfidence.LOW,
        )
    if re.search(r"\b(?:AMER|Americas)\b", field, re.I):
        return result(
            ScreeningOutcome.DEFER,
            "unresolved_Americas_work_option",
            LocationConfidence.MEDIUM,
        )
    if _UNCERTAIN_FIELD.search(field):
        return result(
            ScreeningOutcome.DEFER,
            "unreliable_location_semantics",
            LocationConfidence.LOW,
        )
    if structured.countries and re.search(
        r"\b(?:freelance|freelancer|independent contractor|contributors?|language experts?|data annotat\w*)\b",
        job.title + " " + job.description,
        re.I,
    ):
        return result(
            ScreeningOutcome.DEFER,
            "possible_country_market_tag",
            LocationConfidence.LOW,
        )
    # A remote country tag may be a pay/recruiting market, not a boundary. A
    # country-only default applies to clear work locations, not broad remote text.
    if (structured.countries or structured.regions or iso in ISO_COUNTRIES) and (
        mode == "remote" or _REMOTE.search(job.description)
    ):
        if (
            re.search(r"\b(?:only|restricted to|within)\b", field, re.I)
            or (mode == "remote" and structured.regions and _REMOTE.search(field))
        ) and (structured.countries or structured.regions):
            return result(
                ScreeningOutcome.EXCLUDE,
                "structured_foreign_remote_boundary",
                LocationConfidence.HIGH,
            )
        return result(
            ScreeningOutcome.DEFER,
            "foreign_tag_with_unclear_remote_boundary",
            LocationConfidence.MEDIUM,
        )
    if structured.countries:
        return result(
            ScreeningOutcome.EXCLUDE,
            "structured_foreign_work_location",
            LocationConfidence.HIGH,
        )
    if (
        structured.city_countries
        and iso in ISO_COUNTRIES
        and ISO_COUNTRIES[iso] in structured.city_countries
    ):
        countries = (ISO_COUNTRIES[iso],)
        return result(
            ScreeningOutcome.EXCLUDE,
            "corroborated_foreign_city",
            LocationConfidence.HIGH,
        )
    if (
        structured.city_countries
        or structured.regions
        or title_places.countries
        or title_places.city_countries
        or iso in ISO_COUNTRIES
    ):
        return result(
            ScreeningOutcome.DEFER,
            "insufficient_location_confidence",
            LocationConfidence.MEDIUM,
        )
    return result(
        ScreeningOutcome.DEFER, "unknown_work_eligibility", LocationConfidence.LOW
    )
