"""Four conservative occupational checks using title and actual duty evidence."""

import re

OCCUPATIONAL_REASONS = (
    "paid_survey_participation",
    "retail_store_operations",
    "licensed_clinical_care",
    "food_service_operations",
)

# Protect a technical occupation, not incidental vocabulary in employer copy.
_TECHNICAL_TITLE = re.compile(
    r"\b(?:software|engineer(?:ing)?|developer|programmer|devops|informatics|"
    r"bioinformatics|data (?:analyst|analytics|scientist|engineer)|"
    r"technical support|help[ -]?desk|service desk|systems?|cloud|cybersecurity|"
    r"automation|robotics|firmware|embedded|QA|quality assurance|"
    r"survey (?:methodologist|statistician))\b|\bIT\b",
    re.I,
)
_TECHNICAL_DUTY = re.compile(
    r"^(?:(?:you(?: will|'ll)?|responsibilities include|duties include)\s+)?"
    r"(?:design|build|develop|debug|implement|maintain|write|deploy|configure)\b"
    r"[^.!?]{0,70}\b(?:software|APIs?|microservices|source code|Python|JavaScript|"
    r"SQL (?:queries|pipelines)|data pipelines|cloud infrastructure|network infrastructure|"
    r"automated tests|POS integrations)\b|"
    r"^(?:you(?: will|'ll)?\s+)?(?:provide technical support for [^.!?]{0,50}"
    r"(?:software|applications|survey platforms?|POS systems?|networks)|"
    r"troubleshoot [^.!?]{0,40}(?:software|network infrastructure|APIs)|"
    r"(?:analyze|model|query|visualize) [^.!?]{0,60}(?:SQL|Python|statistical models))\b",
    re.I,
)
_POLL_TITLE = re.compile(
    r"\b(?:data entry|customer (?:service|care)|client service|panelist|"
    r"survey (?:taker|participant|respondent)|questionnaire|focus group|reviewer)\b",
    re.I,
)
_POLL_DUTY = re.compile(
    r"\b(?:earn (?:at home )?by taking (?:paid )?(?:polls|surveys)|"
    r"participate in (?:paid |online )?(?:polls|surveys|focus groups)|"
    r"(?:complete|take|answer|fill out) (?:paid |online |consumer )*(?:surveys|polls|questionnaires))\b",
    re.I,
)
_POLL_PAYMENT = re.compile(
    r"\b(?:earn (?:at home )?by taking (?:paid )?(?:polls|surveys)|"
    r"paid (?:polls|surveys|questionnaires|focus groups)|earn extra income|"
    r"earn money|compensated (?:for|to))\b",
    re.I,
)
_RETAIL_TITLE = re.compile(
    r"\b(?:key[ -]?holder|cashier|merchandiser|merchandising (?:associate|specialist)|"
    r"retail (?:associate|sales|shift|store)|store (?:associate|manager|floor lead)|"
    r"assistant (?:store )?manager|shift (?:lead|leader|manager)|sales floor)\b",
    re.I,
)
_RETAIL_DUTY = re.compile(
    r"\b(?:work (?:the|a) (?:cash )?register|process (?:retail |sales )?transactions|"
    r"(?:open|close|opening|closing) (?:and (?:close|open) )?(?:the |our )?store|"
    r"(?:cover|manage|lead|support|supervise|work|maintain|replenish|stock) (?:the |our )?sales floor|"
    r"(?:stock|restock|replenish|display|arrange) (?:store |retail )?(?:shelves|merchandise|displays)|"
    r"(?:assist|help|serve) (?:in-store )?(?:customers|shoppers) [^.!?]{0,45}(?:purchases|shopping|merchandise)|"
    r"(?:perform|process|handle) retail transactions)\b",
    re.I,
)
_RETAIL_MANAGEMENT_DUTY = re.compile(
    r"\b(?:support (?:your |the |our )?store manager [^.!?]{0,65}(?:sales plan|sales targets)|"
    r"merchandise (?:the|our) store|manage (?:day-to-day )?store operations|"
    r"run (?:your|the|our) store)\b",
    re.I,
)
_CLINICAL_TITLE = re.compile(
    r"\b(?:physician|hospitalist|geriatrician|surgeon|psychiatrist|psychologist|"
    r"nurse|clinical practitioner|nurse practitioner|physician assistant|"
    r"dentist|physical therapist|occupational therapist|respiratory therapist|"
    r"mammography technologist|x-ray technologist|board certified behavior analyst|BCBA)\b",
    re.I,
)
_CLINICAL_DUTY = re.compile(
    r"\b(?:(?:provid(?:e|es|ing)|deliver(?:s|ing)?|perform(?:s|ing)?|administer(?:s|ing)?|conduct(?:s|ing)?|coordinat(?:e|es|ing)|manag(?:e|es|ing)) "
    r"[^.!?]{0,65}(?:patient care|clinical care|bedside|wound care|psychotherapy|"
    r"psychiatric (?:care|evaluations)|psychological (?:testing|assessments)|"
    r"clinical (?:assessments|evaluations)|treatment plans|nursing care|"
    r"behavior(?:al)? (?:therapy|treatment)|mammogra\w*|x-ray examinations)|"
    r"(?:diagnose|treat|assess|evaluate|examine) (?:and (?:treat|diagnose|assess) )?"
    r"(?:patients|clients)|prescribe (?:medication|medications)|"
    r"(?:care|caring) for (?:adult|child|adolescent|patients)|"
    r"complete(?:s|ing)? medication reconciliation)\b",
    re.I,
)
_FOOD_TITLE = re.compile(
    r"\b(?:cookie crew|shift (?:leader|lead|manager)|chef|cook|kitchen (?:assistant|staff|crew)|"
    r"culinary lead|bakery (?:staff|associate|crew)|food service (?:worker|staff)|"
    r"restaurant (?:crew|staff|server)|barista|dishwasher)\b",
    re.I,
)
_FOOD_DUTY = re.compile(
    r"\b(?:(?:bake|prepare|cook|serve|package|deliver) [^.!?]{0,45}"
    r"(?:cookies|food|meals|dough|pastries|beverages|coffee)|"
    r"(?:lead|manage|supervise|oversee) [^.!?]{0,45}(?:bakery|kitchen|restaurant) (?:operations|staff|team)|"
    r"(?:teach|instruct|lead|assist) [^.!?]{0,45}(?:cooking|culinary) (?:classes|instruction)|"
    r"(?:clean|sanitize|wash) [^.!?]{0,35}(?:dishes|kitchen equipment|food preparation)|"
    r"follow(?:s|ing)? provided recipes)\b",
    re.I,
)


def _duty(pattern: re.Pattern[str], description: str) -> str | None:
    """Return the actual source clause, avoiding negated duty statements."""
    for clause in re.split(r"[\n.!?]+", description):
        clause = " ".join(clause.strip(" -•\t").split())
        match = pattern.search(clause)
        if match and not re.search(
            r"\b(?:not|never|no)\b", clause[: match.start()], re.I
        ):
            return clause
    return None


def occupational_matches(title: str, description: str) -> dict[str, tuple[str, ...]]:
    """Return independent reason -> (title match, source duty clauses) evidence.

    A technical title or explicit engineering duty conservatively retains the
    representation. Generic technology use, patient data, and retail POS use
    do not establish technical employment.
    """
    if _TECHNICAL_TITLE.search(title) or _duty(_TECHNICAL_DUTY, description):
        return {}
    result = {}
    # These four fixed checks are deliberately independent; no configurable engine.
    poll_title = _POLL_TITLE.search(title)
    poll_duty = _duty(_POLL_DUTY, description) if poll_title else None
    payment = _duty(_POLL_PAYMENT, description) if poll_duty else None
    if poll_duty and re.search(
        r"\b(?:to apply|application (?:process|questionnaire)|during onboarding|as part of (?:the )?(?:application|hiring))\b",
        poll_duty,
        re.I,
    ):
        poll_duty = None
    if poll_title and poll_duty and payment:
        result[OCCUPATIONAL_REASONS[0]] = tuple(
            dict.fromkeys((poll_title.group(), poll_duty, payment))
        )
    retail_title = _RETAIL_TITLE.search(title)
    retail_duty = _duty(_RETAIL_DUTY, description) if retail_title else None
    if retail_title and not retail_duty:
        retail_duty = _duty(_RETAIL_MANAGEMENT_DUTY, description)
    if retail_title and retail_duty:
        result[OCCUPATIONAL_REASONS[1]] = (retail_title.group(), retail_duty)
    clinical_title = _CLINICAL_TITLE.search(title)
    clinical_duty = _duty(_CLINICAL_DUTY, description) if clinical_title else None
    if clinical_title and clinical_duty:
        result[OCCUPATIONAL_REASONS[2]] = (clinical_title.group(), clinical_duty)
    food_title = _FOOD_TITLE.search(title)
    food_duty = _duty(_FOOD_DUTY, description) if food_title else None
    if food_title and food_duty:
        result[OCCUPATIONAL_REASONS[3]] = (food_title.group(), food_duty)
    return result
