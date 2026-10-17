"""Shared helpers for every job source: location policy, title filter, job dicts."""

import re
from datetime import datetime, timezone

from dotenv import load_dotenv

from jobfinder.config.profile import COUNTRY_TERMS, HOME_COUNTRIES, matches_target_roles

load_dotenv()

PROFILE_QUERY = "profile"  # sentinel: search for the candidate's target roles

# --- SHARED HELPERS -------------------------------------------------------

REMOTE_HINTS = [
    "remote",
    "anywhere",
    "distributed",
    "work from home",
    "home office",
    "homeoffice",
    "home-office",
    "thuiswerk",  # Dutch: work from home
    "télétravail",
]

# Sentinel location: the candidate's home countries under their per-country rules
# (profile.json locations.home_countries, see candidate_profile.HOME_COUNTRIES),
# plus remote roles that aren't restricted to the US or UK.
PREFERRED_LOCATION = "my locations"

# Words/regexes that place a job in a country beyond COUNTRY_TERMS: regions, provinces,
# cantons. Applied whenever that country is one of the candidate's home countries.
_REGION_TERMS = {
    "netherlands": [
        "noord-holland",
        "zuid-holland",
        "north holland",
        "south holland",
        "noord-brabant",
        "gelderland",
        "overijssel",
        "friesland",
        "drenthe",
        "flevoland",
        "zeeland",
        "groningen",
    ],
}
# Swiss address formats the country terms miss: "Wil SG, 9500" — a canton code
# after the city name, ending the string or followed by a comma / 4-digit PLZ.
_REGION_PATTERNS = {
    "switzerland": re.compile(
        r"\b[\wäöüéèà.\-]+ (ag|ai|ar|be|bl|bs|fr|ge|gl|gr|ju|lu|ne|nw|ow|sg|sh|so|sz|tg|ti|ur|vd|vs|zg|zh)\b"
        r"(?=$|\s*,|\s+\d{4}\b)"
    ),
}
_COUNTRY_LABELS = {"uk": "UK", "usa": "USA"}
_THE_COUNTRIES = {"netherlands", "uk", "usa", "united kingdom", "united states", "philippines"}


def _label(country: str) -> str:
    return _COUNTRY_LABELS.get(country, country.title())


def _search_countries() -> list:
    """Home countries searched one by one (Adzuna, climate.jobs). Remote-only countries
    are in the net but not a work location per se, so they're not fanned out."""
    return [h["country"] for h in HOME_COUNTRIES if h["mode"] != "remote_only"]


def _region_terms() -> list:
    """Regional words for all home countries (provinces, user-supplied extras)."""
    return [
        t for h in HOME_COUNTRIES for t in _REGION_TERMS.get(h["country"], []) + h["extra_terms"]
    ]


def home_policy_summary() -> str:
    """One-line description of the "my locations" rules, e.g. for the TUI location picker:
    "Switzerland · Germany remote · Netherlands hybrid/remote"."""
    parts = []
    for h in HOME_COUNTRIES:
        suffix = {"any": "", "hybrid_or_remote": " hybrid/remote", "remote_only": " remote"}[
            h["mode"]
        ]
        parts.append(_label(h["country"]) + suffix)
    return " · ".join(parts) if parts else "remote only"


def _home_region_label() -> str:
    """ "Switzerland, the Netherlands or Germany" — for prompts that name the region."""
    names = [
        ("the " if h["country"] in _THE_COUNTRIES else "") + _label(h["country"])
        for h in HOME_COUNTRIES
    ]
    if len(names) < 2:
        return names[0] if names else "Europe"
    return ", ".join(names[:-1]) + " or " + names[-1]


def _is_preferred_location(location: str) -> bool:
    return (location or "").strip().lower() == PREFERRED_LOCATION


def _location_terms(location: str) -> set:
    """Expand a location string into matchable terms (cities, country codes)."""
    if not location:
        return set()
    loc = location.lower().strip()
    if loc == PREFERRED_LOCATION:
        terms = set()
        for country in _search_countries():
            terms.add(country)
            terms.update(COUNTRY_TERMS.get(country, []))
        return terms
    terms = {loc}
    for country, cities in COUNTRY_TERMS.items():
        if loc == country or loc in cities:
            terms.add(country)
            terms.update(cities)
    return terms


def _term_pattern(terms: list) -> "re.Pattern":
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b")


_US_UK_PATTERN = re.compile(
    r"\b(us|usa|u\.s\.a?|united states|america|american|uk|u\.k|united kingdom|"
    r"britain|british|england|gb|london|new york|nyc|san francisco|boston|austin|denver|"
    r"seattle|chicago|los angeles)\b"
)
_GLOBAL_HINTS = ["worldwide", "global", "anywhere", "europe", "emea", "international"]

# Explicit "you must come to the office" signals (only these drop an NL job).
_ONSITE_HINTS = [
    "on-site",
    "onsite",
    "on site",
    "in-office",
    "in office",
    "office-based",
    "office based",
    "no remote",
    "not remote",
    "op locatie",  # Dutch: on location
    "vor ort",  # German: on site
]


def _in_home_country(home: dict, loc: str) -> bool:
    """True if a location string places a job in this home country."""
    country = home["country"]
    terms = (
        list(COUNTRY_TERMS.get(country, [country]))
        + _REGION_TERMS.get(country, [])
        + home["extra_terms"]
    )
    pattern = _REGION_PATTERNS.get(country)
    return bool(_term_pattern(terms).search(loc) or (pattern and pattern.search(loc)))


def _preferred_location_verdict(job: dict) -> tuple[bool, str]:
    """Apply the PREFERRED_LOCATION policy (HOME_COUNTRIES); returns (keep, drop_reason)."""
    loc = (job.get("location") or "").lower()
    wfh = (job.get("wfh_policy") or "").lower()
    blob = f"{loc} {wfh} {(job.get('snippet') or '')[:2000].lower()}"
    hybrid = "hybrid" in blob or "hybride" in blob
    remote = any(h in blob for h in REMOTE_HINTS)
    onsite = any(h in blob for h in _ONSITE_HINTS)
    for home in HOME_COUNTRIES:
        if not _in_home_country(home, loc):
            continue
        name, mode = _label(home["country"]), home["mode"]
        if mode == "any":
            return True, ""
        if mode == "remote_only":
            if remote:
                return True, ""
            cities = home["hybrid_cities"]
            if hybrid and cities and _term_pattern(cities).search(loc):
                return True, ""  # hybrid is fine only where it is commutable
            where = f"outside {', '.join(c.title() for c in cities)} / " if cities else ""
            return False, f"{name} {where}not remote"
        # hybrid_or_remote: benefit of the doubt — drop only on an explicit on-site signal
        if onsite and not (hybrid or remote):
            return False, f"{name} with explicit on-site signal and no hybrid/remote"
        return True, ""
    if hybrid or remote:
        # Remote elsewhere: keep unless restricted to the US or UK
        if _US_UK_PATTERN.search(f"{loc} {wfh}") and not any(h in loc for h in _GLOBAL_HINTS):
            return False, f"remote but US/UK-restricted location ({job.get('location')})"
        if re.search(r"\b(us|uk)[- ]only\b|\bonly (candidates|applicants) in the (us|uk)\b", blob):
            return False, "remote but description says US/UK only"
        return True, ""
    return False, "outside home countries and no remote/hybrid signal"


def _passes_preferred_location(job: dict) -> bool:
    return _preferred_location_verdict(job)[0]


LOCATION_DEBUG_CSV = "location_debug.csv"


def _write_location_debug(dropped: list) -> None:
    """Overwrite location_debug.csv with jobs the preferred-location policy dropped."""
    import csv

    cols = ["title", "company", "location", "wfh_policy", "drop_reason", "source", "url"]
    try:
        with open(LOCATION_DEBUG_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(dropped)
    except OSError as e:
        print(f"   Could not write {LOCATION_DEBUG_CSV}: {e}")


def _matches_location(job_location: str, terms: set) -> bool:
    """True if a job's location matches the requested terms (remote always passes)."""
    if not terms:
        return True
    loc = (job_location or "").lower()
    if any(hint in loc for hint in REMOTE_HINTS):
        return True
    return any(t in loc for t in terms)


def _title_matches(title: str, query: str) -> bool:
    """Match a job title against a free-text query, or the role profile."""
    if query == PROFILE_QUERY or not query:
        return matches_target_roles(title)
    q = query.lower()
    title_l = (title or "").lower()
    if "," in q:  # OR across comma-separated phrases
        return any(part.strip() in title_l for part in q.split(","))
    return all(word in title_l for word in q.split())  # AND across words


def _job_age_days(posted_date: str) -> int | None:
    """Parse common date formats; return age in days or None if unparseable."""
    if not posted_date:
        return None
    text = str(posted_date)[:10]
    try:
        dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return None


def _make_job(**kw) -> dict:
    """Normalize a job dict with all standard fields present."""
    return {
        "title": kw.get("title") or "",
        "company": kw.get("company") or "Unknown",
        "url": kw.get("url") or "",
        "location": kw.get("location") or "",
        "salary": kw.get("salary") or "Not listed",
        "posted_date": kw.get("posted_date") or "",
        "wfh_policy": kw.get("wfh_policy") or "Unknown",
        "snippet": (kw.get("snippet") or "")[:500],
        "source": kw.get("source") or "",
    }


_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) JobFinder/1.0"}
