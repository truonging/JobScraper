"""Bounded vocabulary for the independent geographic assessment.

This is deliberately not a geocoder. City-only labels need corroboration; ISO
codes are interpreted only in Lever's country field, never in arbitrary prose.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass


def names_pattern(names: Iterable[str]) -> re.Pattern[str]:
    return re.compile(
        r"(?<!\w)(?:"
        + "|".join(re.escape(x) for x in sorted(names, key=len, reverse=True))
        + r")(?!\w)",
        re.I,
    )


COUNTRIES = {
    x: x
    for x in (
        "Costa Rica|United Kingdom|Canada|India|Brazil|Germany|France|Mexico|Australia|"
        "Philippines|Japan|Singapore|Poland|Ireland|Netherlands|Spain|Portugal|"
        "South Africa|New Zealand|Argentina|Colombia|Romania|Switzerland|South Korea|"
        "North Korea|Taiwan|Hong Kong|China|Israel|Ukraine|Italy|Peru|Chile|Uruguay|"
        "Ecuador|Venezuela|Bolivia|Paraguay|Panama|Guatemala|El Salvador|Honduras|"
        "Nicaragua|Dominican Republic|Jamaica|Trinidad and Tobago|Bahamas|Barbados|"
        "Belgium|Austria|Czech Republic|Slovakia|Slovenia|Croatia|Serbia|"
        "Bosnia and Herzegovina|Montenegro|North Macedonia|Albania|Greece|Cyprus|"
        "Malta|Hungary|Bulgaria|Moldova|Lithuania|Latvia|Estonia|Finland|Sweden|"
        "Norway|Denmark|Iceland|Luxembourg|Monaco|Liechtenstein|Turkey|Russia|"
        "Belarus|Armenia|Azerbaijan|Kazakhstan|Uzbekistan|Kyrgyzstan|Pakistan|"
        "Bangladesh|Sri Lanka|Nepal|Bhutan|Indonesia|Malaysia|Thailand|Vietnam|"
        "Cambodia|Laos|Myanmar|Mongolia|United Arab Emirates|Saudi Arabia|Qatar|"
        "Bahrain|Kuwait|Oman|Jordan|Lebanon|Egypt|Morocco|Tunisia|Algeria|Nigeria|"
        "Kenya|Ghana|Uganda|Tanzania|Rwanda|Ethiopia|Senegal|Zambia|Zimbabwe|"
        "Mauritius|Botswana|Namibia|Mozambique|Angola|Cameroon|Madagascar|Fiji|"
        "Papua New Guinea"
    ).split("|")
}
COUNTRIES.update(
    {
        "UK": "United Kingdom",
        "U.K.": "United Kingdom",
        "England": "United Kingdom",
        "Scotland": "United Kingdom",
        "Wales": "United Kingdom",
        "Northern Ireland": "United Kingdom",
        "Czechia": "Czech Republic",
        "Türkiye": "Turkey",
        "UAE": "United Arab Emirates",
        "Viet Nam": "Vietnam",
    }
)
ISO_COUNTRIES = dict(
    zip(
        "GB CA IN BR DE FR MX AU PH JP SG PL IE NL ES PT ZA NZ AR CO RO CH KR TW HK CN IL UA IT PE CL CR AE SE DK FI NO BE AT CZ MY TH VN ID PK NG KE".split(),
        "United Kingdom|Canada|India|Brazil|Germany|France|Mexico|Australia|Philippines|Japan|Singapore|Poland|Ireland|Netherlands|Spain|Portugal|South Africa|New Zealand|Argentina|Colombia|Romania|Switzerland|South Korea|Taiwan|Hong Kong|China|Israel|Ukraine|Italy|Peru|Chile|Costa Rica|United Arab Emirates|Sweden|Denmark|Finland|Norway|Belgium|Austria|Czech Republic|Malaysia|Thailand|Vietnam|Indonesia|Pakistan|Nigeria|Kenya".split(
            "|"
        ),
        strict=True,
    )
)
REGIONS = "EMEA|APAC|APJ|EU|Europe|European Union|LATAM|Latin America|Asia Pacific|Asia-Pacific|Asia|Africa|Middle East|DACH|ANZ".split(
    "|"
)
STATES = "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia".split(
    "|"
)
US_CITIES = "New York|New York City|NYC|San Francisco|Los Angeles|San Diego|San Jose|Seattle|Boston|Austin|Dallas|Houston|Chicago|Denver|Atlanta|Miami|Philadelphia|Phoenix|Salt Lake City|Mountain View|Palo Alto|Sunnyvale|Santa Clara|San Mateo|Redwood City|Menlo Park|Bellevue|San Antonio|Raleigh|Charlotte|Minneapolis|Pittsburgh|Detroit|Indianapolis|Nashville|Tampa|Orlando|Las Vegas|Reno|Baltimore|Cincinnati|Kansas City|Boulder|Sacramento|Irvine|Santa Monica".split(
    "|"
)
CITIES = {}
for _country, _cities in {
    "United Kingdom": "London|Manchester|Bristol|Edinburgh|Belfast|Glasgow|Leeds|Birmingham",
    "India": "Bengaluru|Bangalore|Mumbai|Delhi|Gurugram|Gurgaon|Hyderabad|Pune|Chennai|Noida|Kolkata",
    "Canada": "Toronto|Vancouver|Montreal|Montréal|Ottawa|Calgary|Mississauga|Waterloo",
    "Germany": "Berlin|Munich|München|Hamburg|Frankfurt|Cologne|Düsseldorf",
    "France": "Paris|Lyon|Marseille|Toulouse",
    "Netherlands": "Amsterdam|Rotterdam|Utrecht",
    "Ireland": "Dublin|Cork",
    "Spain": "Madrid|Barcelona|Valencia",
    "Portugal": "Lisbon|Porto",
    "Poland": "Warsaw|Krakow|Kraków|Wroclaw|Wrocław|Gdansk",
    "Brazil": "São Paulo|Sao Paulo|Belo Horizonte|Rio de Janeiro|Curitiba|Campinas",
    "Mexico": "Mexico City|Ciudad de Mexico|Guadalajara|Monterrey",
    "Costa Rica": "Heredia",
    "Colombia": "Bogotá|Bogota|Medellín|Medellin",
    "Argentina": "Buenos Aires",
    "Australia": "Sydney|Melbourne|Brisbane|Perth|Adelaide",
    "New Zealand": "Auckland|Wellington",
    "Japan": "Tokyo|Osaka",
    "South Korea": "Seoul",
    "Taiwan": "Taipei|Hsinchu",
    "China": "Shanghai|Beijing|Shenzhen|Hangzhou",
    "Israel": "Tel Aviv|Haifa",
    "Philippines": "Manila|Makati|Cebu|Taguig",
    "Malaysia": "Kuala Lumpur",
    "Thailand": "Bangkok",
    "Vietnam": "Ho Chi Minh City|Hanoi",
    "Indonesia": "Jakarta",
    "United Arab Emirates": "Dubai|Abu Dhabi",
    "Czech Republic": "Prague",
    "Sweden": "Stockholm",
    "Denmark": "Copenhagen",
    "Norway": "Oslo",
    "Finland": "Helsinki",
    "Belgium": "Brussels",
    "Austria": "Vienna",
    "Switzerland": "Zurich|Zürich|Geneva",
    "South Africa": "Cape Town|Johannesburg",
}.items():
    CITIES.update(dict.fromkeys(_cities.split("|"), _country))

_COUNTRY = names_pattern(COUNTRIES)
_REGION = names_pattern(REGIONS)
_CITY = names_pattern(CITIES)
_STATE = names_pattern(STATES)
_US_CITY = names_pattern(US_CITIES)
_US = re.compile(
    r"\b(?:united states(?: of america)?|usa|u\.s\.(?:a\.?)?)(?!\w)|(?-i:\bUS\b)", re.I
)
_CODE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)(?=$|[\s,;/)])"
)
_LOOKUP = {k.casefold(): v for k, v in COUNTRIES.items()}
_CITY_LOOKUP = {k.casefold(): v for k, v in CITIES.items()}
_TRAILING_STATE = re.compile(
    r",\s*(?:"
    + "|".join(re.escape(x) for x in STATES)
    + r")(?:\s+\d{5}(?:-\d{4})?)?(?:\s*[-,()]?\s*(?:US|United States|Remote))?\s*$",
    re.I,
)
_START = names_pattern(
    [
        *COUNTRIES,
        *REGIONS,
        *CITIES,
        *STATES,
        *US_CITIES,
        "North America",
        "United States",
        "USA",
        "US",
    ]
)


def starts_with_place(value: str) -> bool:
    """Require the location to directly follow an employee-location predicate."""
    value = re.sub(
        r"^(?:(?:only|exclusively|anywhere|the|our|either|remote|multiple locations|locations|offices|in|from|within)\s+)+",
        "",
        value.lstrip(" :(-—–"),
        flags=re.I,
    )
    return bool(_START.match(value))


@dataclass(frozen=True, slots=True)
class Places:
    us: bool
    countries: tuple[str, ...]
    regions: tuple[str, ...]
    city_countries: tuple[str, ...]


def places(value: str) -> Places:
    """Recognize bounded names, shielding US states before foreign countries.

    A foreign country suppresses colliding US city/state inference in the same
    segment; explicit US country text still allows genuine mixed locations.
    """
    # One source uses this joined state spelling in otherwise explicit US
    # addresses (for example Jamaica, NewYork 11420).
    value = re.sub(r"\bNewYork\b", "New York", value, flags=re.I)
    us = False
    countries, regions, cities = set(), set(), set()
    for segment in re.split(r"[;/|]|\s(?:or|and)\s", value, flags=re.I):
        shielded = re.sub(r"\bNew Mexico\b", "", segment, flags=re.I)
        foreign = {_LOOKUP[m.group().casefold()] for m in _COUNTRY.finditer(shielded)}
        # Turkey, Texas and Mexico, Missouri are US cities. An explicit trailing
        # US state disambiguates the preceding country-shaped city token.
        trailing_state = _TRAILING_STATE.search(segment)
        if trailing_state and not re.search(r"\bbaja california\b", segment, re.I):
            foreign.clear()
        code = _CODE.search(segment)
        country_matches = list(_COUNTRY.finditer(shielded))
        if (
            code
            and country_matches
            and all(m.end() <= code.start() for m in country_matches)
        ):
            foreign.clear()
        countries.update(foreign)
        # Africa inside South Africa is a country component, not a separate
        # region-wide remote boundary.
        region_text = _COUNTRY.sub("", shielded)
        regions.update(m.group() for m in _REGION.finditer(region_text))
        state = _STATE.search(segment)
        state_us = bool(state and state.group().casefold() != "georgia")
        local_us = bool(
            _US.search(segment)
            or (
                not foreign
                and not re.search(r"\bbaja california\b", segment, re.I)
                and (state_us or _CODE.search(segment) or _US_CITY.search(segment))
            )
        )
        us |= local_us
        if not local_us:
            cities.update(
                _CITY_LOOKUP[m.group().casefold()] for m in _CITY.finditer(segment)
            )
    return Places(
        us, tuple(sorted(countries)), tuple(sorted(regions)), tuple(sorted(cities))
    )
