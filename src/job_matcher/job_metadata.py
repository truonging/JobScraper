"""Conservative structured ATS metadata; never used for screening.

Lever createdAt is creation, not necessarily public publication time. Greenhouse
first_published is publication; updated_at is deliberately never a fallback.
"""

import re
from datetime import UTC, datetime, timedelta

EMPLOYMENT = {
    "fulltime": "full_time",
    "fulltimeemployment": "full_time",
    "fulltimeemployee": "full_time",
    "fulltimeexperienced": "full_time",
    "regularfulltime": "full_time",
    "clinicalfulltime": "full_time",
    "fulltimehybrid": "full_time",
    "fulltimeonsite": "full_time",
    "parttime": "part_time",
    "clinicalparttime": "part_time",
    "intern": "internship",
    "internship": "internship",
    "contract": "contract",
    "contractor": "contract",
    "contractfulltime": "contract",
    "fixedtermcontract": "contract",
    "temporary": "temporary",
    "seasonal": "temporary",
    "fixedterm": "temporary",
    "other": "other",
    "unknown": "unknown",
    "unspecified": "unknown",
}
WORKPLACE = {
    "remote": "remote",
    "fullyremote": "remote",
    "hybrid": "hybrid",
    "partiallyremotehybrid": "hybrid",
    "onsite": "onsite",
    "fullyonsite": "onsite",
    "inoffice": "onsite",
    "unknown": "unknown",
    "unspecified": "unknown",
}
# Exact custom field names only. Generic "Job Type", boolean remote eligibility,
# office associations and application employment-history settings are ambiguous.
GH_EMPLOYMENT_FIELDS = {
    "employment type",
    "employmentstatus",
    "employment status",
    "employee time type",
    "time type",
    "full or part time",
    "full time/part time",
    "full-time/ part-time",
    "th: employment type",
    "adp employment type",
}
GH_WORKPLACE_FIELDS = {
    "workplace type",
    "workplace",
    "remote type",
    "job location type (hybrid, onsite, remote)",
    "work arrangement",
}


# Exact field/value additions reviewed in the 2026-11-06 metadata coverage audit.
# Only conservative_global_extension_allowed=True rows are included. Keep provider
# and field scope: a safe value in one field need not be safe in another.
LEVER_EMPLOYMENT_ADDITIONS = {
    "1099": "contract",
    "1099/contractor": "contract",
    "1099contractor": "contract",
    "analysis&reporting(fulltime)": "full_time",
    "artificialintelligence(fulltime)": "full_time",
    "associatefulltime": "full_time",
    "cloudengineering(fulltime)": "full_time",
    "coaching(fulltime)": "full_time",
    "coaching(parttime)": "part_time",
    "computerscientist(fulltime)": "full_time",
    "configurationmanagement(fulltime)": "full_time",
    "contingentworker:fulltime": "full_time",
    "contract2.5years": "contract",
    "contract612months": "contract",
    "contractbased": "contract",
    "contractcertificated": "contract",
    "contracthourly": "contract",
    "contractlatam": "contract",
    "contractor/freelance": "contract",
    "contractremote": "contract",
    "contractsalary": "contract",
    "corporate(fulltime)": "full_time",
    "databaseadministrator(fulltime)": "full_time",
    "databaseengineer(fulltime)": "full_time",
    "dataengineer(fulltime)": "full_time",
    "devops(fulltime)": "full_time",
    "directcontract": "contract",
    "directfulltime": "full_time",
    "eefulltime": "full_time",
    "employee/fulltime": "full_time",
    "employee:fulltime": "full_time",
    "employee:parttime": "part_time",
    "employeefulltimeregular": "full_time",
    "engineer(fulltime)": "full_time",
    "eor/fulltime": "full_time",
    "ffulltime": "full_time",
    "fieldfulltime": "full_time",
    "fieldleadership(fulltime)": "full_time",
    "fieldseasonal": "temporary",
    "fixedtermemployee": "temporary",
    "freelance": "contract",
    "freelance/projectbased": "contract",
    "freelancecontract": "contract",
    "freelancer": "contract",
    "freelancer(remote)": "contract",
    "freelanceremote": "contract",
    "frintern": "internship",
    "fulltime(40)": "full_time",
    "fulltime(exempt)": "full_time",
    "fulltime(ft)": "full_time",
    "fulltime(fte)": "full_time",
    "fulltime(fullremote)": "full_time",
    "fulltime(hourly)": "full_time",
    "fulltime(hybrid)": "full_time",
    "fulltime(individualcontributor)": "full_time",
    "fulltime(nonexempt)": "full_time",
    "fulltime(office)": "full_time",
    "fulltime(partlyremote)": "full_time",
    "fulltime(permanent)": "full_time",
    "fulltime(regular)": "full_time",
    "fulltime(remote)": "full_time",
    "fulltime(salary)": "full_time",
    "fulltime,exempt": "full_time",
    "fulltime,nonexempt": "full_time",
    "fulltime,onsite": "full_time",
    "fulltime,permanent": "full_time",
    "fulltime,remote": "full_time",
    "fulltime,remoteorhybrid": "full_time",
    "fulltime,w2": "full_time",
    "fulltime/37.5hours": "full_time",
    "fulltime/hourly": "full_time",
    "fulltime/hybridwork": "full_time",
    "fulltime/onsite": "full_time",
    "fulltime/onsiteorremote": "full_time",
    "fulltime/regular": "full_time",
    "fulltime/remote": "full_time",
    "fulltime/remote/hybrid": "full_time",
    "fulltime/salary": "full_time",
    "fulltime20262027schoolyear": "full_time",
    "fulltime:inthefield": "full_time",
    "fulltime:remote": "full_time",
    "fulltime[hourly]": "full_time",
    "fulltimecontratoadistancia": "full_time",
    "fulltimecontratoindefinido": "full_time",
    "fulltimedays": "full_time",
    "fulltimedirectplacement": "full_time",
    "fulltimeemp": "full_time",
    "fulltimeexempt": "full_time",
    "fulltimeffs": "full_time",
    "fulltimefield": "full_time",
    "fulltimefloat": "full_time",
    "fulltimehourly": "full_time",
    "fulltimeimmediate": "full_time",
    "fulltimeincludingweekends": "full_time",
    "fulltimeinoffice": "full_time",
    "fulltimelatam": "full_time",
    "fulltimeleadership": "full_time",
    "fulltimemaintenance": "full_time",
    "fulltimemfgassoc,distassoc,qualassocplantfloorpositions": "full_time",
    "fulltimenights": "full_time",
    "fulltimenonexempt": "full_time",
    "fulltimenonexempt(h)": "full_time",
    "fulltimenonexempt/hourly": "full_time",
    "fulltimenonexempthourly": "full_time",
    "fulltimenonunion": "full_time",
    "fulltimeonsiteorremote": "full_time",
    "fulltimepermanent": "full_time",
    "fulltimeregular": "full_time",
    "fulltimeremote": "full_time",
    "fulltimeremotebasedinnorthcarolina": "full_time",
    "fulltimeremoteinpersonmeetingattendancerequired": "full_time",
    "fulltimeremoteok": "full_time",
    "fulltimesalaried": "full_time",
    "fulltimesalaried(fte)": "full_time",
    "fulltimesalary": "full_time",
    "fulltimetempsplein": "full_time",
    "fulltimeunion": "full_time",
    "fulltimeus": "full_time",
    "fulltimeusa": "full_time",
    "fulltimew2": "full_time",
    "fulltime|onsite": "full_time",
    "fulltime|remote": "full_time",
    "fulltime–earlycareer13yearsofexperience": "full_time",
    "fulltime–experienced4+yearsofexperience": "full_time",
    "hardwareengineering(fulltime)": "full_time",
    "hourly,fulltime": "full_time",
    "iintern": "internship",
    "independentcontract": "contract",
    "independentcontractor": "contract",
    "independentcontractorprojectbased": "contract",
    "indirectfulltime": "full_time",
    "interim": "temporary",
    "internationalcontractor(b2b)": "contract",
    "internationalfulltimeemployee": "full_time",
    "intern|summer": "internship",
    "it(fulltime)": "full_time",
    "journalistcontractor": "contract",
    "longtermtemp": "temporary",
    "marketing/seo/fulltime": "full_time",
    "modifiedfulltime": "full_time",
    "networkengineer(fulltime)": "full_time",
    "networkevaluator(fulltime)": "full_time",
    "officecontract": "contract",
    "officefulltime": "full_time",
    "oncallparttime": "part_time",
    "ontheroadfulltime": "full_time",
    "ontheroadparttime": "part_time",
    "operations/dataentryvirtualassistant/fulltime": "full_time",
    "parttime(20hoursorless)": "part_time",
    "parttime(individualcontributor)": "part_time",
    "parttime,benefited": "part_time",
    "parttime,permanent": "part_time",
    "parttime/regular": "part_time",
    "parttime:inthefield": "part_time",
    "parttime[hourly]": "part_time",
    "parttimeffs": "part_time",
    "parttimefloat": "part_time",
    "parttimehourly": "part_time",
    "parttimehubzone": "part_time",
    "parttimeimmediate": "part_time",
    "parttimenonexempt/hourly": "part_time",
    "parttimenonunion": "part_time",
    "parttimeperdiem": "part_time",
    "parttimeremote": "part_time",
    "parttimesalary": "part_time",
    "parttimeunion": "part_time",
    "parttimevariable": "part_time",
    "parttime|hybrid": "part_time",
    "parttime|remote": "part_time",
    "permanent(fulltime)": "full_time",
    "permanent,fulltime": "full_time",
    "permanent/fulltime": "full_time",
    "permanentfulltime": "full_time",
    "permanentparttime": "part_time",
    "programcoordinator(fulltime)": "full_time",
    "projectbasedcontractor": "contract",
    "proposalmanager(fulltime)": "full_time",
    "regular(fulltime)": "full_time",
    "regular,contractor": "contract",
    "regular,fulltime": "full_time",
    "regular,hourly,fulltime": "full_time",
    "regular,parttime": "part_time",
    "regular,salaried,fulltime": "full_time",
    "regularparttime": "part_time",
    "regularparttimehbo": "part_time",
    "remotefulltime": "full_time",
    "researchscientist(fulltime)": "full_time",
    "salaried,fulltime": "full_time",
    "salariedfulltimeexempt": "full_time",
    "securityengineer/securityofficer(fulltime)": "full_time",
    "shopfulltime": "full_time",
    "shorttermtemp": "temporary",
    "skillbridgeintern": "internship",
    "socialmediamarketing/management/fulltime": "full_time",
    "softwaredeveloper(fulltime)": "full_time",
    "softwareengineering(fulltime)": "full_time",
    "stages/internship": "internship",
    "studentintern": "internship",
    "studioleadership(fulltime)": "full_time",
    "studioleadership(parttime)": "part_time",
    "studiosupport(parttime)": "part_time",
    "subcontractor": "contract",
    "summerintern": "internship",
    "summerinternship": "internship",
    "system/networkadministration(fulltime)": "full_time",
    "systemsengineering(fulltime)": "full_time",
    "systemsengineering(parttime)": "part_time",
    "technicalexpert/functional(fulltime)": "full_time",
    "temporaryft": "temporary",
    "tempsplein/fulltime": "full_time",
    "testengineer(fulltime)": "full_time",
    "thirdpartycontract": "contract",
    "trainer(fulltime)": "full_time",
    "union/fulltime": "full_time",
    "unpaidinternship": "internship",
    "unrepresentedcontract": "contract",
    "usfulltimesalaried": "full_time",
    "virtualassistant/fulltime": "full_time",
    "virtualassistant/parttime": "part_time",
    "winterintern": "internship",
}

GH_EMPLOYMENT_ADDITIONS = {
    "classification": {"fulltime": "full_time", "parttime": "part_time"},
    "employee class": {"fixedterm": "temporary"},
    "employee type": {
        "fixedterm(fixedterm)": "temporary",
        "fulltime": "full_time",
        "fulltime=80100hrs/biweekly": "full_time",
        "fulltime=80100hrs/week": "full_time",
        "intern": "internship",
        "parttime(beneligible)=60hrs/biweekly": "part_time",
        "parttime(benfullcosttoee)=4058hrs/biweekly": "part_time",
        "temporary": "temporary",
    },
    "employment duration": {
        "fulltime": "full_time",
        "intern": "internship",
        "parttime24": "part_time",
    },
    "employment type": {
        "contractoreor": "contract",
        "emergingmarketcontractor": "contract",
        "employeefulltime": "full_time",
        "exemptfulltime,salaried": "full_time",
        "fixedterm(fixedterm)": "temporary",
        "ft": "full_time",
        "fulltime(exempt)": "full_time",
        "fulltime,exempt": "full_time",
        "fulltime,nonexempt": "full_time",
        "fulltime:experienced": "full_time",
        "fulltime:newgrad": "full_time",
        "fulltimecampus": "full_time",
        "fulltimeexempt": "full_time",
        "fulltimehourly": "full_time",
        "fulltimesalary": "full_time",
        "fulltimetier1": "full_time",
        "fulltimetier2": "full_time",
        "independentcontractor": "contract",
        "jumptradingintern": "internship",
        "nonexemptfulltime,hourly": "full_time",
        "parttime,nonexempt": "part_time",
        "parttimehourly": "part_time",
        "parttimeoncall": "part_time",
        "prn1099": "contract",
        "store,seasonal": "temporary",
        "summerinternship": "internship",
        "temporary(fixedterm)": "temporary",
        "temporary(taco)": "temporary",
        "temporary(tourguide)": "temporary",
        "temporary/interim/fixedterm": "temporary",
    },
    "engagement type": {"fulltime": "full_time", "parttime": "part_time"},
    "full-time/part-time status": {"fulltime": "full_time", "parttime": "part_time"},
    "full/part-time": {"fulltime": "full_time", "parttime": "part_time"},
    "hours": {
        "fulltime": "full_time",
        "parttime": "part_time",
        "temporary": "temporary",
    },
    "other employment types": {"temporary": "temporary"},
    "schedule": {"fulltime": "full_time"},
    "schedule (ft, pt)": {"fulltime": "full_time", "parttime": "part_time"},
    "ukg pro employee type": {"intern": "internship"},
    "worker category": {
        "ffulltime": "full_time",
        "fulltime": "full_time",
        "intern": "internship",
        "intrintern": "internship",
    },
    "worker sub-type": {"fixedterm(fixedterm)": "temporary"},
}

GH_WORKPLACE_ADDITIONS = {
    "flexible work arrangement": {
        "hybrid": "hybrid",
        "onsiteessential": "onsite",
        "onsiteoffice": "onsite",
        "remote": "remote",
    },
    "job location type": {"hybrid": "hybrid", "remote": "remote"},
    "job model": {"hybrid": "hybrid", "office": "onsite", "remote": "remote"},
    "location type:": {"hybrid": "hybrid", "remote": "remote"},
    "no. of days in office": {
        "fullyonsite": "onsite",
        "fullyremote": "remote",
        "hybrid–2daysonsite/week": "hybrid",
        "hybrid–3daysonsite/week": "hybrid",
        "hybrid–4daysonsite/week": "hybrid",
    },
    "office type": {"hybrid": "hybrid", "inoffice": "onsite", "remote": "remote"},
    "remote options": {"fullyremote": "remote", "remotewithinsinglestate": "remote"},
    "remote type": {"partiallyremote/hybrid": "hybrid"},
    "telework": {
        "positionisfulltimeworkfromhome": "remote",
        "positionrequiresallworkdoneonsite": "onsite",
    },
    "work arrangement": {"hybrid": "hybrid", "onsite": "onsite", "remote": "remote"},
    "work arrangement type": {
        "hybrid": "hybrid",
        "office": "onsite",
        "remote": "remote",
    },
    "work designation": {"onsite": "onsite"},
    "work location type": {
        "hybrid": "hybrid",
        "office": "onsite",
        "onsite": "onsite",
        "remote": "remote",
    },
    "work site type": {"hybrid": "hybrid", "onsite": "onsite", "remote": "remote"},
    "work status": {"hybrid": "hybrid", "onsite": "onsite", "remote": "remote"},
    "work style": {"inoffice": "onsite", "remote": "remote"},
    "work type": {"hybrid": "hybrid", "onsite": "onsite", "remote": "remote"},
}


def normalize(value, mapping):
    if not isinstance(value, str):
        return None
    return mapping.get(re.sub(r"[\s_-]+", "", value.strip().casefold()))


def publication_date(value, observed_at, *, milliseconds=False):
    try:
        if milliseconds:
            if (
                type(value) not in (int, float)
                or not 946684800000 <= value < 4102444800000
            ):
                return None
            date = datetime.fromtimestamp(value / 1000, UTC)
        else:
            if not isinstance(value, str):
                return None
            date = datetime.fromisoformat(value)
            if date.utcoffset() is None:
                return None
            date = date.astimezone(UTC)
        if datetime(2000, 1, 1, tzinfo=UTC) <= date <= observed_at + timedelta(days=1):
            return date
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return None


def _custom(payload, names, mapping, additions=None, *, workplace_lists=False):
    values = set()
    metadata = payload.get("metadata")
    for field in metadata if isinstance(metadata, list) else []:
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            continue
        name = " ".join(field["name"].split()).casefold()
        reviewed = (additions or {}).get(name, {})
        if name not in names and not reviewed:
            continue

        def scalar(value):
            # Existing values keep their meaning; new aliases remain field-local.
            return (normalize(value, mapping) if name in names else None) or normalize(
                value, reviewed
            )

        raw = field.get("value")
        if workplace_lists and isinstance(raw, list):
            if not raw:
                continue
            members = [scalar(member) for member in raw]
            # An unknown member or different alternatives do not establish one mode,
            # even if another metadata field supplies a recognized value.
            if None in members or len(set(members)) != 1:
                return None
            values.add(members[0])
        else:
            value = scalar(raw)
            if value is not None:
                values.add(value)
    # No precedence between hours, relationship, duration or workplace options.
    return next(iter(values)) if len(values) == 1 else None


def extract_metadata(source, payload, observed_at):
    if source == "lever":
        categories = payload.get("categories")
        return dict(
            employment_type=normalize(
                categories.get("commitment") if isinstance(categories, dict) else None,
                EMPLOYMENT,
            )
            or normalize(
                categories.get("commitment") if isinstance(categories, dict) else None,
                LEVER_EMPLOYMENT_ADDITIONS,
            ),
            workplace_type=normalize(payload.get("workplaceType"), WORKPLACE),
            posted_at=publication_date(
                payload.get("createdAt"), observed_at, milliseconds=True
            ),
        )
    if source == "greenhouse":
        return dict(
            employment_type=_custom(
                payload, GH_EMPLOYMENT_FIELDS, EMPLOYMENT, GH_EMPLOYMENT_ADDITIONS
            ),
            workplace_type=_custom(
                payload,
                GH_WORKPLACE_FIELDS,
                WORKPLACE,
                GH_WORKPLACE_ADDITIONS,
                workplace_lists=True,
            ),
            posted_at=publication_date(payload.get("first_published"), observed_at),
        )
    return dict(employment_type=None, workplace_type=None, posted_at=None)
