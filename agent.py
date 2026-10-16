"""Climate/Green Tech Job Finder Agent.

Searches free job APIs and climate job boards in parallel, filters titles
against the candidate's target-role profile, validates fit + climate relevance
with an LLM, and saves results to jobs.csv.
"""

import os
import re
import csv
import json
import inspect
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

from candidate_profile import (
    ADZUNA_COUNTRIES,
    ADZUNA_DEFAULT,
    CANDIDATE_PROFILE,
    CLIMATEBASE_COUNTRIES,
    COUNTRY_TERMS,
    FIT_RUBRIC,
    HOME_COUNTRIES,
    NONCLIMATE_MIN_FIT,
    PROFILE_QUERY_SEEDS,
    TIMEZONE,
    matches_target_roles,
)

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


def _home_region_label() -> str:
    """"Switzerland, the Netherlands or Germany" — for prompts that name the region."""
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
        list(COUNTRY_TERMS.get(country, [country])) + _REGION_TERMS.get(country, [])
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


# --- SOURCES ---------------------------------------------------------------


_ARBEITNOW_MAX_PAGES = 15  # follow links.next until the 90-day boundary or the feed ends


def search_arbeitnow(query: str, location: str = "") -> list:
    """Search Arbeitnow API for EU/DACH jobs. Free, no API key required.

    The feed is Laravel-paginated (100 newest per page); reading only page 1
    collapsed the 90-day window to the newest ~day. We follow links.next until a
    page crosses the 90-day boundary (created_at is a Unix epoch — the old code
    stored it raw, so these jobs were never age-filtered) or the feed ends.
    """
    import time

    import requests

    terms = _location_terms(location)
    jobs = []
    url = "https://www.arbeitnow.com/api/job-board-api"
    try:
        for page_num in range(_ARBEITNOW_MAX_PAGES):
            if page_num:
                time.sleep(0.3)  # the feed rate-limits bursts; be polite between pages
            resp = requests.get(url, timeout=15)
            if resp.status_code != 200:  # rate-limited / hiccup: keep what we have so far
                break
            payload = resp.json()
            page_oldest = None
            for job in payload.get("data", []):
                created = job.get("created_at")
                posted = (
                    datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
                    if isinstance(created, (int, float))
                    else str(created or "")[:10]
                )
                age = _job_age_days(posted)
                if age is not None:
                    page_oldest = age if page_oldest is None else max(page_oldest, age)
                title = job.get("title", "")
                if not _title_matches(title, query):
                    continue
                # Remote roles qualify under the remote-anywhere policy regardless of
                # the (city-only) location string that _matches_location reads.
                is_remote = bool(job.get("remote"))
                if not is_remote and not _matches_location(job.get("location", ""), terms):
                    continue
                jobs.append(
                    _make_job(
                        title=title,
                        company=job.get("company_name"),
                        url=job.get("url"),
                        location=job.get("location"),
                        posted_date=posted,
                        wfh_policy="Remote" if is_remote else "On-site",
                        snippet=re.sub(r"<[^>]+>", " ", job.get("description", "")),
                        source="arbeitnow",
                    )
                )
            nxt = (payload.get("links") or {}).get("next")
            if not nxt or (page_oldest is not None and page_oldest > 90):
                break
            url = nxt
        print(f"   Arbeitnow: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   Arbeitnow error: {e}")
        return jobs


_REMOTEOK_MAX_OFFSET = 1500  # the load-more feed runs out ~offset 1000-1100


def search_remoteok(query: str, location: str = "") -> list:
    """Search RemoteOK for remote jobs via its HTML load-more feed.

    The /api endpoint hard-caps at the newest ~100 items and ignores ?offset, so
    it only ever saw ~4 days of postings. We page the site's load-more endpoint
    (?action=get_jobs&offset=N) instead — each job row carries a JSON-LD
    JobPosting (title, company, applicantLocationRequirements) plus a data-epoch —
    walking until a page crosses the 90-day window or the feed ends.
    """
    from html import unescape

    import requests

    now = datetime.now(timezone.utc).timestamp()
    jobs, seen = [], set()
    try:
        offset = 0
        while offset < _REMOTEOK_MAX_OFFSET:
            resp = requests.get(
                f"https://remoteok.com/?action=get_jobs&offset={offset}",
                headers=_HTTP_HEADERS,
                timeout=15,
            )
            if resp.status_code != 200:
                break
            rows = list(
                re.finditer(
                    r'<tr\b([^>]*\bdata-epoch="\d+"[^>]*)>(.*?)(?=<tr\b|</table)',
                    resp.text,
                    re.S,
                )
            )
            if not rows:
                break
            oldest_age = 0
            for m in rows:
                attrs, body = m.group(1), m.group(2)
                epoch_m = re.search(r'data-epoch="(\d+)"', attrs)
                url_m = re.search(r'data-url="([^"]*)"', attrs)
                comp_m = re.search(r'data-company="([^"]*)"', attrs)
                epoch = int(epoch_m.group(1)) if epoch_m else None
                if epoch is not None:
                    oldest_age = max(oldest_age, (now - epoch) / 86400)
                ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', body, re.S)
                try:
                    data = json.loads(ld.group(1)) if ld else {}
                except Exception:
                    data = {}
                title = unescape(data.get("title") or "")
                url = f"https://remoteok.com{url_m.group(1)}" if url_m else ""
                if not title or not url or url in seen or not _title_matches(title, query):
                    continue
                seen.add(url)
                alr = data.get("applicantLocationRequirements")
                names = (
                    [x.get("name", "") for x in alr]
                    if isinstance(alr, list)
                    else [alr.get("name", "")] if isinstance(alr, dict) else []
                )
                loc = (
                    ", ".join(n for n in names if n and n.lower() not in ("anywhere", "worldwide"))
                    or "Remote"
                )
                posted = (
                    datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")
                    if epoch is not None
                    else ""
                )
                jobs.append(
                    _make_job(
                        title=title,
                        company=unescape(
                            (comp_m.group(1) if comp_m else "")
                            or (data.get("hiringOrganization") or {}).get("name")
                            or ""
                        ),
                        url=url,
                        location=loc,
                        posted_date=posted,
                        wfh_policy="Remote",
                        snippet=re.sub(r"<[^>]+>", " ", data.get("description") or ""),
                        source="remoteok",
                    )
                )
            if oldest_age > 90:  # date-sorted: past here is older than the window
                break
            offset += len(rows)
        print(f"   RemoteOK: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   RemoteOK error: {e}")
        return jobs


_ADZUNA_MAX_PAGES = 6  # guard per (country, seed); ~most seeds stop at page 1 (mind API quota)


def search_adzuna(query: str, location: str = "") -> list:
    """Search Adzuna API. Requires ADZUNA_APP_ID and ADZUNA_API_KEY in .env."""
    import requests

    app_id, api_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_API_KEY")
    if not app_id or not api_key:
        print("   Adzuna: skipped (no API key configured)")
        return []

    loc = (location or "").lower()
    if _is_preferred_location(location):
        country_codes = [
            ADZUNA_COUNTRIES[c] for c in _search_countries() if c in ADZUNA_COUNTRIES
        ]
        if not country_codes:
            print("   Adzuna: skipped (no Adzuna country among your home countries)")
            return []
    else:
        country_code = ADZUNA_DEFAULT
        for name, code in ADZUNA_COUNTRIES.items():
            if name in loc or any(city in loc for city in COUNTRY_TERMS.get(name, [])):
                country_code = code
                break
        if not country_code:
            print(f"   Adzuna: skipped (no country match for location {location!r})")
            return []
        country_codes = [country_code]

    seeds = PROFILE_QUERY_SEEDS if query == PROFILE_QUERY else [query]
    jobs, seen = [], set()
    # /search/{page} is date-sorted; a query with count>50 (e.g. NL "product lead"
    # had ~250) lost everything past page 1 before. Page through each (country,
    # seed) until a short page ends it or the guard trips. max_days_old matches the
    # pipeline's 90-day window (was 60, which pre-filtered 61-90-day CH/NL roles).
    for country_code, seed in ((c, s) for c in country_codes for s in seeds):
        params = {
            "app_id": app_id,
            "app_key": api_key,
            "what": seed,
            "results_per_page": 50,
            "max_days_old": 90,
            "sort_by": "date",
        }
        if (
            location
            and not _is_preferred_location(location)
            and loc not in (ADZUNA_COUNTRIES.keys() | {country_code})
        ):
            params["where"] = location
        try:
            for page in range(1, _ADZUNA_MAX_PAGES + 1):
                resp = requests.get(
                    f"https://api.adzuna.com/v1/api/jobs/{country_code}/search/{page}",
                    params=params,
                    timeout=15,
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
                for job in results:
                    url = job.get("redirect_url", "")
                    title = job.get("title", "")
                    if url in seen or not _title_matches(title, query):
                        continue
                    seen.add(url)
                    desc = job.get("description", "")
                    salary = "Not listed"
                    if job.get("salary_min"):
                        salary = f"{int(job['salary_min']):,} - {int(job.get('salary_max', 0)):,}"
                    jobs.append(
                        _make_job(
                            title=title,
                            company=job.get("company", {}).get("display_name"),
                            url=url,
                            location=job.get("location", {}).get("display_name", ""),
                            salary=salary,
                            posted_date=str(job.get("created", ""))[:10],
                            wfh_policy=(
                                "Remote" if "remote" in (title + desc).lower() else "On-site"
                            ),
                            snippet=desc,
                            source="adzuna",
                        )
                    )
                if len(results) < 50:  # last page for this (country, seed)
                    break
        except Exception as e:
            print(f"   Adzuna error ({seed}): {e}")
    print(f"   Adzuna: {len(jobs)} matching jobs")
    return jobs


# Curated climate companies with live ATS boards (verified 2026-07).
GREENHOUSE_CLIMATE_COMPANIES = [
    "watershed",
    "overstory",
    "patch",
    "ecoworks",
    "redwoodmaterials",
    "eh2",
    "camusenergy",
    "carbonfuture",
]
LEVER_CLIMATE_COMPANIES = ["pachama", "envelio", "voltus"]


def search_greenhouse(query: str, location: str = "") -> list:
    """Search Greenhouse boards of curated climate companies. No API key needed."""
    import requests
    from concurrent.futures import ThreadPoolExecutor

    terms = _location_terms(location)

    def fetch(slug):
        found = []
        try:
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=10)
            if r.status_code != 200:
                return []
            for j in r.json().get("jobs", []):
                title = j.get("title", "")
                loc = j.get("location", {}).get("name", "")
                if not _title_matches(title, query) or not _matches_location(loc, terms):
                    continue
                found.append(
                    _make_job(
                        title=title,
                        company=slug.replace("-", " ").title(),
                        url=j.get("absolute_url"),
                        location=loc,
                        posted_date=str(j.get("updated_at", ""))[:10],
                        wfh_policy="Remote" if "remote" in loc.lower() else "On-site",
                        source="greenhouse",
                    )
                )
        except Exception:
            pass
        return found

    jobs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for result in ex.map(fetch, GREENHOUSE_CLIMATE_COMPANIES):
            jobs.extend(result)
    print(f"   Greenhouse: {len(jobs)} matching jobs")
    return jobs


def search_lever(query: str, location: str = "") -> list:
    """Search Lever boards of curated climate companies. No API key needed."""
    import requests
    from concurrent.futures import ThreadPoolExecutor

    terms = _location_terms(location)

    def fetch(slug):
        found = []
        try:
            r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=10)
            if r.status_code != 200:
                return []
            for j in r.json():
                title = j.get("text", "")
                loc = j.get("categories", {}).get("location", "") or ""
                if not _title_matches(title, query) or not _matches_location(loc, terms):
                    continue
                ts = j.get("createdAt")
                posted = (
                    datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    if ts
                    else ""
                )
                workplace = j.get("workplaceType", "")
                found.append(
                    _make_job(
                        title=title,
                        company=slug.replace("-", " ").title(),
                        url=j.get("hostedUrl"),
                        location=loc,
                        posted_date=posted,
                        wfh_policy=(
                            "Remote" if workplace == "remote" else workplace.title() or "Unknown"
                        ),
                        snippet=j.get("descriptionPlain", ""),
                        source="lever",
                    )
                )
        except Exception:
            pass
        return found

    jobs = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for result in ex.map(fetch, LEVER_CLIMATE_COMPANIES):
            jobs.extend(result)
    print(f"   Lever: {len(jobs)} matching jobs")
    return jobs


CLIMATETECHLIST_AIRTABLE_URL = "https://airtable.com/embed/appo9WyVgYvpap792/shrbAhhxKqvWjaJMs"


def search_climatetechlist(query: str, location: str = "") -> list:
    """Search ClimateTechList's Airtable shared view (~10k climate jobs). No API key."""
    import requests

    try:
        resp = requests.get(CLIMATETECHLIST_AIRTABLE_URL, headers=_HTTP_HEADERS, timeout=15)
        resp.raise_for_status()
        html = resp.text

        url_match = re.search(r'urlWithParams["\s:]*"(.*?)"', html)
        if not url_match:
            print("   ClimateTechList: could not extract API URL")
            return []
        api_url = "https://airtable.com" + url_match.group(1).replace("\\u002F", "/").replace(
            "\\/", "/"
        )

        headers = {
            **_HTTP_HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "x-airtable-inter-service-client": "webClient",
            "x-time-zone": TIMEZONE,
        }
        app_id_match = re.search(r'"x-airtable-application-id"\s*:\s*"([^"]*)"', html)
        if app_id_match:
            headers["x-airtable-application-id"] = app_id_match.group(1)

        data = requests.get(
            api_url, headers=headers, cookies=resp.cookies.get_dict(), timeout=30
        ).json()

        table = data.get("data", {}).get("table", {})
        col_by_name = {c.get("name", "").lower().strip(): c["id"] for c in table.get("columns", [])}

        def find_col(*candidates):
            for name in candidates:
                if name in col_by_name:
                    return col_by_name[name]
            for cand in candidates:
                for col_name, col_id in col_by_name.items():
                    if cand in col_name:
                        return col_id
            return None

        col_title = find_col("position title", "title", "position")
        col_company = find_col("company", "employer")
        col_apply = find_col("apply to job", "apply")
        col_location = find_col("job location", "location")
        col_salary = find_col("salary range (beta)", "salary", "compensation")
        col_date = find_col("date first listed", "date")
        col_remote = find_col("remote")

        terms = _location_terms(location)
        jobs = []
        for row in table.get("rows", []):
            cells = row.get("cellValuesByColumnId", {})

            def cell(col_id, default=""):
                val = cells.get(col_id) if col_id else None
                if isinstance(val, dict):
                    return val.get("url", "")
                return str(val).strip() if val else default

            title = re.sub(r"^[✨\U0001f31f⭐\U0001f525]+", "", cell(col_title)).strip()
            if not _title_matches(title, query):
                continue
            job_location = cell(col_location).split("\n")[0].strip()
            if not _matches_location(job_location, terms):
                continue
            jobs.append(
                _make_job(
                    title=title,
                    company=cell(col_company, "Unknown"),
                    url=cell(col_apply),
                    location=job_location,
                    salary=cell(col_salary, "Not listed"),
                    posted_date=cell(col_date)[:10],
                    wfh_policy=cell(col_remote, "Unknown"),
                    source="climatetechlist",
                )
            )
        print(f"   ClimateTechList: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   ClimateTechList error: {e}")
        return []


# Public search-only Algolia credentials extracted from climatebase.org's frontend.
CLIMATEBASE_ALGOLIA_APP = "8PSNFFQTXQ"
CLIMATEBASE_ALGOLIA_KEY = "d2ebe27d3cc3d35fea04da7b1b0718a8"
_CLIMATEBASE_MAX_PAGES = 12  # per seed, after the 90-day filter (Algolia caps offset at 1000)

# Climatebase's `how_to_apply` field is whatever the "Apply" button links to on the
# company's own ATS, which is often the application-form URL, not the job posting —
# e.g. .../apply, .../applicants/new (Greenhouse job boards), .../application (Ashby).
# Fetching that page for a description scrapes form labels ("First name", "Resume")
# instead of the role, and opening it in a browser drops straight into the form.
# Stripping the trailing segment recovers the real posting URL on every ATS checked
# (Lever, Workable, Ashby, Greenhouse job boards).
_APPLY_SUFFIX_RE = re.compile(r"/(?:apply|application|applicants/new)/?$", re.I)


def _strip_apply_suffix(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    path = _APPLY_SUFFIX_RE.sub("", parts.path)
    if path == parts.path:
        return url
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def search_climatebase(query: str, location: str = "") -> list:
    """Search Climatebase (~14k climate jobs) via its public Algolia index."""
    import requests
    from concurrent.futures import ThreadPoolExecutor

    endpoint = f"https://{CLIMATEBASE_ALGOLIA_APP}-dsn.algolia.net/1/indexes/Job_production/query"
    headers = {
        "X-Algolia-API-Key": CLIMATEBASE_ALGOLIA_KEY,
        "X-Algolia-Application-Id": CLIMATEBASE_ALGOLIA_APP,
        "Content-Type": "application/json",
    }

    facet_filters = []
    loc = (location or "").lower()
    if _is_preferred_location(location):
        # OR group: every home country (the verdict policy then trims e.g. remote-only
        # countries), or remote anywhere
        facet_filters = [
            [
                *(
                    f"countries:{CLIMATEBASE_COUNTRIES.get(h['country'], h['country'].title())}"
                    for h in HOME_COUNTRIES
                ),
                "remote_preferences:Remote",
            ]
        ]
    else:
        for term, country in CLIMATEBASE_COUNTRIES.items():
            if term in loc or any(city in loc for city in COUNTRY_TERMS.get(term, [])):
                # OR group: jobs in the country, or remote anywhere
                facet_filters = [[f"countries:{country}", "remote_preferences:Remote"]]
                break

    # The Algolia index is relevance-ordered (not date-sorted) with thousands of
    # hits per seed, so a single top-50 page dropped most matching CH/NL/remote
    # roles. We bound the set server-side to the last 90 days via the numeric
    # activation_date_i, then page through what remains (a handful of pages once
    # date-filtered) so the 90-day window — not the page size — governs.
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=90)).timestamp())

    seeds = PROFILE_QUERY_SEEDS if query == PROFILE_QUERY else [query]
    jobs, seen = [], set()

    def add_hit(hit):
        title = hit.get("title", "")
        job_id = hit.get("objectID", "")
        if job_id in seen or not _title_matches(title, query):
            return
        seen.add(job_id)
        url = hit.get("how_to_apply") or f"https://climatebase.org/job/{job_id}"
        if not url.startswith("http"):
            url = f"https://climatebase.org/job/{job_id}"
        url = _strip_apply_suffix(url)
        remote_prefs = hit.get("remote_preferences") or []
        jobs.append(
            _make_job(
                title=title,
                company=hit.get("name_of_employer"),
                url=url,
                location=", ".join(hit.get("locations", [])[:2])
                or ", ".join(hit.get("countries", [])[:2])
                or ("Remote" if "Remote" in remote_prefs else ""),
                salary=(
                    f"{hit['salary_from']} - {hit['salary_to']}"
                    if hit.get("salary_from")
                    else "Not listed"
                ),
                posted_date=str(hit.get("activation_date", ""))[:10],
                wfh_policy=", ".join(remote_prefs) or "Unknown",
                snippet=re.sub(r"<[^>]+>", " ", hit.get("description") or ""),
                source="climatebase",
            )
        )

    def fetch_seed(seed):
        out = []
        try:
            for page in range(_CLIMATEBASE_MAX_PAGES):
                body = {
                    "query": seed,
                    "hitsPerPage": 50,
                    "page": page,
                    "numericFilters": [f"activation_date_i>={cutoff}"],
                }
                if facet_filters:
                    body["facetFilters"] = facet_filters
                resp = requests.post(endpoint, headers=headers, json=body, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                out.extend(data.get("hits", []))
                if page + 1 >= data.get("nbPages", 1):
                    break
        except Exception as e:
            print(f"   Climatebase error ({seed}): {e}")
        return out

    # Seeds fan out in parallel; hits are merged (dedup + filter) single-threaded.
    with ThreadPoolExecutor(max_workers=min(8, len(seeds))) as ex:
        for hits in ex.map(fetch_seed, seeds):
            for hit in hits:
                add_hit(hit)
    print(f"   Climatebase: {len(jobs)} matching jobs")
    return jobs


_JOBSINCARBON_MAX_FETCH = 40  # title-matches whose pages we fetch (sitemap is newest-first)


def _jobsincarbon_parse(page: str) -> dict:
    """Pull title/company/location/date from a jobsincarbon.com JSON-LD JobPosting."""
    for m in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S
    ):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not (isinstance(item, dict) and item.get("@type") == "JobPosting"):
                continue
            job_loc = item.get("jobLocation")
            if isinstance(job_loc, list):
                job_loc = job_loc[0] if job_loc else {}
            addr = (job_loc or {}).get("address") or {}
            loc = ", ".join(
                x
                for x in (
                    addr.get("addressLocality"),
                    addr.get("addressRegion"),
                    addr.get("addressCountry"),
                )
                if x
            )
            remote = str(item.get("jobLocationType", "")).upper() == "TELECOMMUTE"
            return {
                "title": item.get("title") or "",
                "company": (item.get("hiringOrganization") or {}).get("name") or "Unknown",
                "location": loc or ("Remote" if remote else "See posting"),
                "posted_date": str(item.get("datePosted") or "")[:10],
                "remote": remote,
            }
    return {}


def search_jobsincarbon(query: str, location: str = "") -> list:
    """Search Jobs in Carbon (jobsincarbon.com) via its sitemap.

    The board migrated off niceboard to jobsincarbon.com (JS-rendered, but each
    /jobs/<slug> page ships a JSON-LD JobPosting). We title-filter on the slug
    first (cheap), then fetch each match's page for its real title / company /
    location / date. Location filtering happens AFTER the fetch (once the location
    is known), so — unlike the old matches[:15] cap — a role is never dropped by a
    fetch cap before it has even been location-checked.
    """
    import requests
    from concurrent.futures import ThreadPoolExecutor

    try:
        resp = requests.get(
            "https://jobsincarbon.com/sitemap.xml", headers=_HTTP_HEADERS, timeout=15
        )
        resp.raise_for_status()
        urls = re.findall(r"<loc>(https://jobsincarbon\.com/jobs/[^<]+)</loc>", resp.text)

        matches = []
        for url in urls:
            slug = url.rstrip("/").rsplit("/jobs/", 1)[-1]
            if not slug or slug == "jobs":
                continue
            slug_text = re.sub(r"-[0-9a-f]{6,}$", "", slug).replace("-", " ")
            if _title_matches(slug_text, query):
                matches.append(url)
        matches = matches[:_JOBSINCARBON_MAX_FETCH]

        terms = _location_terms(location)

        def fetch(url):
            try:
                page = requests.get(url, headers=_HTTP_HEADERS, timeout=10).text
            except Exception:
                return None
            info = _jobsincarbon_parse(page)
            if not info.get("title") or not _title_matches(info["title"], query):
                return None
            if not (info.get("remote") or _matches_location(info["location"], terms)):
                return None
            return _make_job(
                title=info["title"],
                company=info["company"],
                url=url,
                location=info["location"],
                posted_date=info["posted_date"],
                wfh_policy="Remote" if info.get("remote") else "Unknown",
                source="jobsincarbon",
            )

        jobs = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            jobs = [j for j in ex.map(fetch, matches) if j]
        print(f"   JobsInCarbon: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   JobsInCarbon error: {e}")
        return []


_REMOTEIMPACT_MAX_PAGES = 25  # newest-first, undated cards (~2 weeks deep): a recency-bounded walk


def search_remoteimpact(query: str, location: str = "") -> list:
    """Search Remote Impact's climate & environment category.

    ~20 cards/page, date-sorted newest-first, but the cards carry NO date, so we
    walk a bounded number of pages (a recency proxy) rather than to a 90-day
    cutoff. The old code paginated with a "page/N/" path that 404s (so it only
    ever read page 1) and hardcoded location "Remote"; we use the real "?page=N"
    param and parse each card's actual location (e.g. "Remote (US)", "UK") so the
    downstream US/UK-only policy can act on it.
    """
    import requests

    terms = _location_terms(location)
    jobs = []
    try:
        for page in range(1, _REMOTEIMPACT_MAX_PAGES + 1):
            suffix = "" if page == 1 else f"?page={page}"
            resp = requests.get(
                f"https://remoteimpact.org/jobs/category/climate-environment/{suffix}",
                headers=_HTTP_HEADERS,
                timeout=15,
            )
            if resp.status_code != 200:
                break
            cards = list(
                re.finditer(
                    r'<a href="(/jobs/[^"]+)"[^>]*>([^<]+)</a>\s*</h2>\s*<p[^>]*>([^<]*)</p>'
                    r'(?P<rest>.*?)(?=<a href="/jobs/[^"]+"[^>]*>[^<]+</a>\s*</h2>|\Z)',
                    resp.text,
                    re.S,
                )
            )
            if not cards:
                break
            for m in cards:
                title = m.group(2).strip()
                if not _title_matches(title, query):
                    continue
                loc_m = re.search(r"</svg>\s*([A-Za-z][^<]*?)\s*</span>", m.group("rest"))
                job_location = loc_m.group(1).strip() if loc_m else "Remote"
                if not _matches_location(job_location, terms):
                    continue
                jobs.append(
                    _make_job(
                        title=title,
                        company=m.group(3).strip() or "Unknown",
                        url=f"https://remoteimpact.org{m.group(1)}",
                        location=job_location,
                        wfh_policy="Remote" if "remote" in job_location.lower() else "Unknown",
                        source="remoteimpact",
                    )
                )
        print(f"   RemoteImpact: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   RemoteImpact error: {e}")
        return jobs


_CJ_MONTHS = {
    m: i
    for i, m in enumerate(
        "January February March April May June July August September October November December".split(),
        start=1,
    )
}


def _climatejobs_date(text: str) -> str:
    """Parse climate.jobs "Posted 17th July" into YYYY-MM-DD (year inferred)."""
    m = re.search(r"Posted\s+(\d+)(?:st|nd|rd|th)?\s+([A-Za-z]+)", text)
    if not m:
        return ""
    day, month = int(m.group(1)), _CJ_MONTHS.get(m.group(2).capitalize())
    if not month:
        return ""
    now = datetime.now(timezone.utc)
    try:
        dt = datetime(now.year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return ""
    if dt > now:  # a future date means the posting is from last year
        dt = dt.replace(year=now.year - 1)
    return dt.strftime("%Y-%m-%d")


# climate.jobs full-catalog crawl bookkeeping. The date-sorted catalog is deep
# (many pages sit inside the 90-day window), so walking it fully is only worth
# doing occasionally. The first pass crawls to the 90-day stop and records when;
# passes within _CJ_FULL_REFRESH_DAYS re-crawl only postings newer than that
# anchor (everything older is already in jobs.csv) and stop after a few pages.
_CJ_CRAWL_STATE = ".climatejobs_crawl.json"
_CJ_FULL_REFRESH_DAYS = 7
_CJ_INCREMENTAL_MARGIN_DAYS = 2  # overlap re-crawled each time, so nothing slips the gap
_CJ_MAX_PAGES = 60  # safety bound on a full pass (~1400 postings) if dates go unparseable


def _cj_last_full_crawl() -> "datetime | None":
    """When the full climate.jobs catalog was last crawled end-to-end, or None."""
    try:
        with open(_CJ_CRAWL_STATE, encoding="utf-8") as f:
            ts = json.load(f).get("last_full_crawl")
        return datetime.fromisoformat(ts) if ts else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _cj_record_full_crawl(when: datetime) -> None:
    try:
        with open(_CJ_CRAWL_STATE, "w", encoding="utf-8") as f:
            json.dump({"last_full_crawl": when.isoformat()}, f)
    except OSError:
        pass


# CDRjobs' board is a pym iframe (embed.cdrjobs.earth) over a public, auth-free Apollo
# GraphQL API; the clientKey is the embed UUID from the Wix page's "Add job board" snippet
# (https://www.cdrjobs.earth/job-board). Introspection is off and `limit` caps at 100, so
# the catalog (~600 jobs, no masked premium rows) is walked with the keyset cursor the
# frontend uses: pagination {limit, takeAfter: <last publishedAt>, takeAfterId: <last id>}.
_CDRJOBS_GRAPHQL = "https://prod-backend.cdrjobs.earth/graphql"
_CDRJOBS_CLIENT_KEY = "f2c03c34-d670-448c-b5ca-77b6b69b6f26"
_CDRJOBS_MAX_PAGES = 15
_CDRJOBS_QUERY = """query searchJobs($clientKey: String!, $filters: jobFiltersInput!,
    $pagination: paginationInput!) {
  searchJobs(clientKey: $clientKey, filters: $filters, pagination: $pagination) {
    data { id title sourceUrl locations { country city } remote currency minSalary maxSalary
      publishedAt company { name } }
  }
}"""
_CDRJOBS_COUNTRIES = {
    "ch": "Switzerland", "de": "Germany", "nl": "Netherlands", "at": "Austria",
    "fr": "France", "gb": "United Kingdom", "ie": "Ireland", "es": "Spain", "it": "Italy",
    "be": "Belgium", "dk": "Denmark", "se": "Sweden", "no": "Norway", "fi": "Finland",
    "pt": "Portugal", "pl": "Poland", "us": "United States", "ca": "Canada",
    "au": "Australia", "in": "India", "is": "Iceland", "ae": "United Arab Emirates",
}  # unknown codes pass through upper-cased


def search_cdrjobs(query: str, location: str = "") -> list:
    """Search CDRjobs (carbon dioxide removal industry board, ~600 jobs). No login."""
    import requests

    headers = {**_HTTP_HEADERS, "Origin": "https://embed.cdrjobs.earth"}
    try:
        terms = _location_terms(location)
        jobs, pagination = [], {"limit": 100}
        for _ in range(_CDRJOBS_MAX_PAGES):
            resp = requests.post(
                _CDRJOBS_GRAPHQL,
                headers=headers,
                json={
                    "query": _CDRJOBS_QUERY,
                    "variables": {
                        "clientKey": _CDRJOBS_CLIENT_KEY,
                        "filters": {},
                        "pagination": pagination,
                    },
                },
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
            page = ((payload.get("data") or {}).get("searchJobs") or {}).get("data")
            if page is None:
                print(f"   CDRjobs: unexpected response {str(payload.get('errors'))[:120]}")
                break
            for j in page:
                title = (j.get("title") or "").strip()
                if not title or not _title_matches(title, query):
                    continue
                places = []
                for loc in j.get("locations") or []:
                    code = (loc.get("country") or "").lower()
                    country = _CDRJOBS_COUNTRIES.get(code, code.upper())
                    places.append(", ".join(p for p in (loc.get("city"), country) if p))
                remote = j.get("remote")  # "yes" | "hybrid" | "no"
                job_location = "; ".join(places)
                if remote == "yes":
                    job_location = ("Remote — " + job_location) if job_location else "Remote"
                if not _matches_location(job_location, terms):
                    continue
                lo, hi = j.get("minSalary"), j.get("maxSalary")
                salary = ""
                if lo or hi:
                    span = f"{lo:,}–{hi:,}" if lo and hi else f"{(lo or hi):,}"
                    salary = f"{span} {j.get('currency') or ''}".strip()
                jobs.append(
                    _make_job(
                        title=title,
                        company=(j.get("company") or {}).get("name"),
                        url=j.get("sourceUrl"),
                        location=job_location or "See posting",
                        salary=salary,
                        posted_date=(j.get("publishedAt") or "")[:10],
                        wfh_policy={"yes": "Remote", "hybrid": "Hybrid", "no": "On-site"}.get(
                            remote, "Unknown"
                        ),
                        source="cdrjobs",
                    )
                )
            if len(page) < 100:
                break
            pagination = {
                "limit": 100,
                "takeAfter": page[-1]["publishedAt"],
                "takeAfterId": page[-1]["id"],
            }
        print(f"   CDRjobs: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   CDRjobs error: {e}")
        return []


def search_wttj(query: str, location: str = "") -> list:
    """Welcome to the Jungle recommendations — optional, needs `cli.py --login-wttj` once.

    A personalized queue (~90 roles), not a searchable board; silently skipped when there is
    no saved login or Playwright. See wttj.py.
    """
    import wttj

    if not wttj.session_available():
        return []
    try:
        terms = _location_terms(location)
        jobs = []
        for j in wttj.fetch_jobs():
            title = (j.get("title") or "").strip()
            if not title or not _title_matches(title, query):
                continue
            places = [p["name"] for p in j.get("formattedLocationPreferences") or []]
            if j.get("officeLocation"):
                places.insert(0, str(j["officeLocation"]))
            job_location = "; ".join(dict.fromkeys(places))
            if j.get("remoteOnly") and "remote" not in job_location.lower():
                job_location = ("Remote — " + job_location) if job_location else "Remote"
            if not _matches_location(job_location, terms):
                continue
            sal = j.get("salaryRange") or {}
            salary = ""
            if sal.get("minAmount") or sal.get("maxAmount"):
                lo, hi = (int(float(sal[k])) if sal.get(k) else None for k in ("minAmount", "maxAmount"))
                span = f"{lo:,}–{hi:,}" if lo and hi else f"{(lo or hi):,}"
                salary = f"{span} {sal.get('currency') or ''}".strip()
            bullets = [b["value"] for b in j.get("involvesBullets") or []]
            jobs.append(
                _make_job(
                    title=title,
                    company=(j.get("company") or {}).get("name"),
                    url=j.get("originalUrl") or f"https://app.welcometothejungle.com/jobs/{j['externalId']}",
                    location=job_location or "See posting",
                    salary=salary,
                    posted_date="",  # insertedAt is first-seen (often years old), not freshness
                    wfh_policy="Remote" if j.get("remoteOnly") else "Unknown",
                    snippet=" ".join(bullets),
                    source="wttj",
                )
            )
        print(f"   WTTJ: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   WTTJ error: {e}")
        return []


def search_climatejobs(query: str, location: str = "") -> list:
    """Search climate.jobs (carries Remote Climate Jobs' premium listings).

    Server-rendered HTML, ~24 cards/page. A logged-in cj_session cookie
    (CLIMATEJOBS_SESSION in .env) unlocks the premium/RCJ cards; without it
    the free subset still works. Location filtering uses the site's
    /jobs/location/<country> pages when available.

    climate.jobs has a single location slug per listing page, so a multi-country
    preference (PREFERRED_LOCATION) can't be one slug — /jobs/location/my-locations
    404s and falls back to the full date-sorted catalog. So for the preferred
    location we scrape each country's own page (small, complete, server-filtered)
    and add the full catalog to catch remote-elsewhere roles. The full catalog is
    crawled to the 90-day cutoff on a first pass, then incrementally (only newer
    than the last full crawl) for a week — see the _cj_*_crawl helpers. Results
    across pages are merged and deduped by URL.
    """
    import requests

    from html import unescape

    session_cookie = os.getenv("CLIMATEJOBS_SESSION", "")
    cookies = {"cj_session": session_cookie} if session_cookie else {}
    terms = _location_terms(location)

    # (slug, filter_locally) targets. filter_locally=False trusts the server-side
    # location page; the empty-slug full catalog is filtered against `terms`.
    if _is_preferred_location(location):
        targets = [
            (re.sub(r"[^a-z0-9]+", "-", c).strip("-"), False) for c in _search_countries()
        ] + [("", True)]
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", (location or "").lower()).strip("-")
        targets = [(slug, not slug)]

    # How deep to walk the full catalog: a fresh deep crawl to the 90-day cutoff,
    # or (if one ran recently) just the postings newer than that anchor. Only the
    # profile query maps to a stable anchor; free-text `q=` gets a full pass each
    # time. Country location pages are small and always walked fully (stop_age 90).
    now = datetime.now(timezone.utc)
    is_profile = not query or query == PROFILE_QUERY
    last_full = _cj_last_full_crawl() if is_profile else None
    incremental = last_full is not None and (now - last_full).days < _CJ_FULL_REFRESH_DAYS
    catalog_stop_age = (now - last_full).days + _CJ_INCREMENTAL_MARGIN_DAYS if incremental else 90

    jobs, seen_urls, ran_full_catalog = [], set(), False
    try:
        for slug, filter_locally in targets:
            base = (
                f"https://climate.jobs/jobs/location/{slug}"
                if slug
                else "https://climate.jobs/jobs"
            )
            full_catalog = not slug  # the deep firehose vs a small country page
            stop_age = catalog_stop_age if full_catalog else 90
            for page in range(1, _CJ_MAX_PAGES + 1):
                params = {"sort": "date_desc", "page": page}
                if query and query != PROFILE_QUERY:
                    params["q"] = query
                resp = requests.get(
                    base, params=params, cookies=cookies, headers=_HTTP_HEADERS, timeout=15
                )
                if resp.status_code == 404 and slug:  # unknown location slug: filter locally
                    base, slug, filter_locally = "https://climate.jobs/jobs", "", True
                    resp = requests.get(
                        base, params=params, cookies=cookies, headers=_HTTP_HEADERS, timeout=15
                    )
                if resp.status_code != 200:
                    break

                cards = re.split(r'<a href="/company/(?=[^"]+/[^"]+")', resp.text)[1:]
                if not cards:
                    # Ran off the end of the catalog (not page 1, so not an error/block).
                    if full_catalog and not incremental and page > 1:
                        ran_full_catalog = True
                    break
                oldest_age = 0
                for card in cards:
                    path_m = re.match(r'([^"]+)"', card)
                    title_m = re.search(r"<h3[^>]*>\s*(.*?)\s*</h3>", card, re.S)
                    if not path_m or not title_m:
                        continue
                    url = f"https://climate.jobs/company/{path_m.group(1)}"
                    if url in seen_urls:
                        continue
                    title = unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip()
                    if not _title_matches(title, query):
                        continue
                    company_m = re.search(r'truncate">\s*(.*?)\s*</p>', card, re.S)
                    snippet_m = re.search(r'line-clamp-2 mb-4">\s*(.*?)\s*</p>', card, re.S)
                    loc_m = re.search(r'loading="lazy">\s*([A-Za-z][^<]*?)\s*</span>', card)
                    wfh_m = re.search(r">\s*(Remote|Hybrid|On-site|In-person)\s*</span>", card)
                    salary_m = re.search(
                        r"text-forest-500[\s\S]{0,600}?</svg>\s*([^<]+?)\s*<", card
                    )
                    posted = _climatejobs_date(card)
                    if (age := _job_age_days(posted)) is not None:
                        oldest_age = max(oldest_age, age)
                    job_location = loc_m.group(1) if loc_m else ""
                    if filter_locally and not _matches_location(job_location, terms):
                        continue
                    seen_urls.add(url)
                    jobs.append(
                        _make_job(
                            title=title,
                            company=unescape(company_m.group(1)) if company_m else "Unknown",
                            url=url,
                            location=job_location or "See posting",
                            salary=salary_m.group(1).strip() if salary_m else "",
                            posted_date=posted,
                            wfh_policy=wfh_m.group(1) if wfh_m else "Unknown",
                            snippet=unescape(snippet_m.group(1)) if snippet_m else "",
                            source="climatejobs",
                        )
                    )
                if oldest_age > stop_age:  # date-sorted: everything past here is older
                    if full_catalog and not incremental:
                        ran_full_catalog = True  # reached the 90-day cutoff cleanly
                    break
            else:  # loop exhausted _CJ_MAX_PAGES without hitting the cutoff
                if full_catalog and not incremental:
                    ran_full_catalog = True
        if ran_full_catalog:
            _cj_record_full_crawl(now)
        print(f"   ClimateJobs: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   ClimateJobs error: {e}")
        return jobs


# --- LLM VALIDATION --------------------------------------------------------

VALIDATION_MODEL = "claude-haiku-4-5"  # fast + cheap; scoring is a simple batch task

_anthropic_client = None

# Sources that only list climate jobs — climate relevance is inherent.
CLIMATE_BOARD_SOURCES = {
    "climatebase",
    "climatetechlist",
    "greenhouse",
    "lever",
    "jobsincarbon",
    "remoteimpact",
    "climatejobs",
    "cdrjobs",
}  # (wttj is a general tech board — deliberately not here)

# Structured-output schema for one validation batch.
_VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "evaluations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "fit": {"type": "integer"},
                    "startup": {"type": "integer"},
                    "climate": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "fit", "startup", "climate", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["evaluations"],
    "additionalProperties": False,
}


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import llm

        _anthropic_client = llm.get_client()
    return _anthropic_client


def _dedup_and_filter_stale(jobs: list, seen_keys: set, max_age_days: int) -> list:
    """Drop stale postings first, then dedup by cross-device identity.

    Order matters: the old code registered the dedup key *before* the age check,
    so an ancient variant of a job poisoned the key and a fresh repost from a
    better source vanished entirely. Same-key variants now resolve to the freshest
    one (dated beats undated; among dated, newest posted_date wins).
    """
    def _age(job):
        return _job_age_days(job.get("posted_date", ""))

    from sync import norm_key

    fresh = [j for j in jobs if (a := _age(j)) is None or a <= max_age_days]
    by_key: dict[str, dict] = {}
    kept = []
    for job in fresh:
        key = norm_key(job.get("url", ""), job.get("company", ""), job.get("title", ""))
        if not key:  # nothing to identify the job by — never dedup pasted/unknown jobs
            kept.append(job)
            continue
        if key in seen_keys:
            continue
        if (prev := by_key.get(key)) is not None:
            pa, a = _age(prev), _age(job)
            if pa is None or (a is not None and a < pa):
                by_key[key] = job
        else:
            by_key[key] = job
    kept.extend(by_key.values())
    seen_keys.update(by_key)
    return kept


def _search_dedupe(all_jobs: list, seen_keys: set, max_age_days: int) -> list:
    return _dedup_and_filter_stale(all_jobs, seen_keys, max_age_days)


def _salvage_evaluations_json(text: str) -> dict | None:
    """Recover the complete evaluations from a truncated validation JSON reply.

    A batch cut off mid-write fails json.loads outright; the old fallback then
    scored the *whole* batch with priors (fit=3 / climate=4 on climate boards —
    jobs that pass the gates unvalidated). This trims to the last complete
    evaluation object and closes the structure, so only the genuinely missing
    tail falls back.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    last_close = -1  # position just past the last complete evaluation object
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])  # complete, not truncated
                except json.JSONDecodeError:
                    return None
            if depth == 1:  # closed one evaluation inside evaluations[]
                # (outer object = depth 1 while open, each evaluation = depth 2)
                last_close = i + 1
    if last_close <= start:
        return None
    # Truncated (possibly mid-string): keep everything up to the last complete
    # evaluation and close evaluations[] + the outer object.
    try:
        data = json.loads(text[start:last_close] + "]}")
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get("evaluations"), list):
        return data
    return None


def _validation_json(prompt: str) -> dict | None:
    """Score a validation batch via the LLM with a JSON-schema-constrained response."""
    import llm

    if not llm.api_key():
        return None
    try:
        response = _get_anthropic_client().messages.create(
            model=VALIDATION_MODEL,
            max_tokens=4000,  # 15 evaluations + reasons ≈ 1.5-2.5k; 2000 truncated whole batches
            output_config={"format": {"type": "json_schema", "schema": _VALIDATION_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in response.content if b.type == "text")
    except Exception as e:
        print(f"   Validation error ({VALIDATION_MODEL}): {e}")
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = _salvage_evaluations_json(text)
        if data is None:
            print(f"   Validation reply unparseable ({VALIDATION_MODEL}) — batch left unvalidated")
            return None
        print(
            f"   Validation reply truncated — salvaged {len(data.get('evaluations', []))} "
            f"complete evaluation(s), only the missing tail falls back"
        )
    return {str(ev["id"]): ev for ev in data.get("evaluations", []) if isinstance(ev, dict) and "id" in ev}


def validate_jobs_batch(jobs: list, search_query: str) -> list:
    """Score a batch of jobs (max ~15) for role fit and climate relevance via LLM."""
    if not jobs:
        return []

    role_target = (
        "the candidate profile below"
        if search_query == PROFILE_QUERY
        else f'the search query "{search_query}" and the candidate profile below'
    )
    jobs_text = "".join(
        f"ID {i}: {j.get('title')} at {j.get('company')} ({j.get('location', '?')}) "
        f"- {(j.get('_desc') or j.get('snippet') or '')[:1000]}\n"
        for i, j in enumerate(jobs)
    )
    prompt = f"""Evaluate these job postings for a specific candidate.

CANDIDATE PROFILE:
{CANDIDATE_PROFILE}

JOBS:
{jobs_text}

Respond with JSON: an "evaluations" array with one object per job ID, each with:
- id: the job ID from the list above.
- fit: 0-5. How well the ROLE matches {role_target}. Score the role itself:
  {FIT_RUBRIC["criteria"]}.
  5 = {FIT_RUBRIC["five"]}
  3 = {FIT_RUBRIC["three"]}
  0-1 = {FIT_RUBRIC["zero"]}
- startup: 0-5. How startup-like (non-bureaucratic) the COMPANY feels, judged from the
  posting's tone, company size/stage, and process signals.
  5 = scrappy startup/scale-up: small team, founders close, fast decisions, ship-first
  3 = grown-up scale-up: some process and structure, still moves quickly
  0-1 = big corporate / government / consultancy: layers, committees, enterprise
        process, compliance-speak, "alignment with stakeholders"
- climate: 0-5. Climate relevance of the COMPANY/mission.
  5 = core climate mission (carbon removal, renewables, grid, climate SaaS)
  3 = meaningful sustainability angle
  0-1 = no climate connection, or fossil/heavy-polluter
- reason: one short sentence.

Example:
{{"evaluations": [{{"id": 0, "fit": 5, "startup": 4, "climate": 4, "reason": "Head of Product at a carbon accounting startup"}}]}}"""

    results_map = _validation_json(prompt)
    if results_map is None:
        # No LLM available: titles already passed the profile regex, so assume
        # moderate fit; trust climate-only boards for climate relevance.
        return [
            {
                "fit_score": 3,
                "startup_score": 3,
                "climate_score": 4 if j.get("source") in CLIMATE_BOARD_SOURCES else 0,
                "reason": "unvalidated (no LLM available)",
            }
            for j in jobs
        ]

    validations = []
    for i, job in enumerate(jobs):
        res = results_map.get(str(i), {})
        try:
            fit = int(res.get("fit", 3))
            startup = int(res.get("startup", 3))
            climate = int(res.get("climate", 0))
        except (TypeError, ValueError):
            fit, startup, climate = 3, 3, 0
        if climate == 0 and job.get("source") in CLIMATE_BOARD_SOURCES:
            climate = 3  # climate board listing outranks a missing LLM judgment
        validations.append(
            {
                "fit_score": fit,
                "startup_score": startup,
                "climate_score": climate,
                "reason": res.get("reason", ""),
            }
        )
    return validations


# --- UNIFIED SEARCH --------------------------------------------------------

ALL_SOURCES = [
    search_arbeitnow,
    search_remoteok,
    search_adzuna,
    search_greenhouse,
    search_lever,
    search_climatetechlist,
    search_climatebase,
    search_jobsincarbon,
    search_remoteimpact,
    search_climatejobs,
    search_cdrjobs,
    search_wttj,
]

# Postings older than this get an is-it-still-online check during search.
VERIFY_AGE_DAYS = 30
_OFFLINE_STATUSES = {404, 410}


def verify_jobs_online(
    jobs: list, verify_unknown_dates: bool = False, _progress=None
) -> tuple[list, int]:
    """Drop jobs whose posting URL is definitively gone. Returns (kept, dropped_count).

    Only checks postings older than VERIFY_AGE_DAYS (and, optionally, ones with
    no parseable date). A posting counts as offline ONLY on a definitive
    404/410. Crawl refusals (401/403/405/429), bot walls, server errors, and
    timeouts are NOT evidence the job is gone — those get the benefit of the
    doubt and the job is kept.
    """
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def needs_check(job):
        if not (job.get("url") or "").startswith("http"):
            return False
        age = _job_age_days(job.get("posted_date"))
        if age is None:
            return verify_unknown_dates
        return age > VERIFY_AGE_DAYS

    to_check = [j for j in jobs if needs_check(j)]
    if not to_check:
        return jobs, 0

    def is_online(job):
        try:
            resp = requests.get(
                job["url"], headers=_HTTP_HEADERS, timeout=8, allow_redirects=True, stream=True
            )
            resp.close()
            if resp.status_code in _OFFLINE_STATUSES:
                return False
            # Soft 404s: ATSes 200-redirect dead postings back to the board root
            # (Greenhouse adds ?error=true; the job-id path segment disappears).
            if resp.history:
                final = resp.url or ""
                if "error=true" in final:
                    return False
                segment = job["url"].split("?")[0].rstrip("/").rsplit("/", 1)[-1]
                if len(segment) >= 6 and any(c.isdigit() for c in segment):
                    if segment not in final:
                        return False
            return True
        except Exception:
            return True  # network trouble is not evidence the posting is gone

    offline_ids = set()
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(is_online, j): j for j in to_check}
        for done, future in enumerate(as_completed(futures), 1):
            job = futures[future]
            if not future.result():
                offline_ids.add(id(job))
            if _progress:
                _progress("verify", done, len(to_check), job.get("company", ""))

    kept = [j for j in jobs if id(j) not in offline_ids]
    if offline_ids:
        print(f"   Dropped {len(offline_ids)} posting(s) that are no longer online")
    return kept, len(offline_ids)


def _drop_region_locked(jobs: list, location: str) -> tuple[list, list]:
    """Split off jobs Jev says are region-locked away from the searched location.

    A job whose location field already names the searched place is never dropped
    (an on-site Zürich role that mentions US clients is not region-locked).
    """
    import jev

    terms = set(_location_terms(location))
    if _is_preferred_location(location):
        terms.update(_region_terms())
    terms = sorted(terms)
    local = _term_pattern(terms) if terms else None
    kept, dropped = [], []
    for job in jobs:
        lock = (job.get("_jev") or {}).get("region_locked", 0)
        loc = (job.get("location") or "").lower()
        is_local = bool(local and local.search(loc)) or any(
            _REGION_PATTERNS[h["country"]].search(loc)
            for h in HOME_COUNTRIES
            if h["country"] in _REGION_PATTERNS
        )
        if lock >= jev.REGION_LOCK_THRESHOLD and not is_local:
            dropped.append({**job, "drop_reason": f"Jev: region-locked ({lock:.2f})"})
        else:
            kept.append(job)
    return kept, dropped


def _apply_jev_scores(job: dict) -> None:
    """Let Jev's calibrated fit/startup/climate scores override the LLM's (when Jev judged the job).

    Large disagreements are noted in `reason` so a surprising score is explainable.
    """
    verdict = job.get("_jev")
    if not verdict:
        return
    notes = []
    for key in ("fit", "startup", "climate"):
        field = f"{key}_score"
        llm_score, jev_score = job.get(field), int(round(verdict[key]))
        job[field] = jev_score
        if isinstance(llm_score, int) and abs(jev_score - llm_score) >= 2:
            notes.append(f"{key} {jev_score} per Jev, LLM {llm_score}")
    if notes:
        job["reason"] = f"{job.get('reason', '')} [{'; '.join(notes)}]".strip()


def fetch_descriptions(jobs: list, _progress=None) -> None:
    """Fetch the full posting text for each job in parallel, attached as `_desc`.

    Runs during search so LLM validation scores the real description (not just
    the feed snippet) and the TUI cards can preview it instantly. Fetched text
    is disk-cached (job_descriptions.json) via cover_letter.fetch_job_description;
    `_desc` itself is stripped before CSV writes. Jobs whose fetch fails keep
    only their snippet.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from cover_letter import fetch_job_description

    to_fetch = [j for j in jobs if (j.get("url") or "").startswith("http") and "_desc" not in j]
    if not to_fetch:
        return

    def fetch(job):
        text = fetch_job_description(job["url"])
        if text:
            job["_desc"] = text

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(fetch, j): j for j in to_fetch}
        for done, future in enumerate(as_completed(futures), 1):
            if _progress:
                _progress("descriptions", done, len(to_fetch), futures[future].get("company", ""))


def search_jobs(
    query: str = "",
    location: str = "",
    max_results: int = 50,
    min_fit: int = 3,
    min_climate: int = 2,
    max_age_days: int = 90,
    non_climate: bool = False,
    _progress=None,
    _sources: list | None = None,
) -> dict:
    """Search all job sources in parallel and validate results with an LLM.

    Args:
        query: Job title query. Empty or "profile" searches the candidate's
            full target-role profile (see candidate_profile.py).
        location: Country/city filter; remote jobs always pass.
        max_results: Cap on returned jobs.
        min_fit: Minimum role-fit score (0-5) to keep a job.
        min_climate: Minimum climate score (0-5) to keep a job.
        max_age_days: Drop jobs posted longer ago than this (when date is known).
        non_climate: Invert the climate gate — keep only jobs scoring BELOW
            min_climate but fitting the role at least NONCLIMATE_MIN_FIT. The
            best general (non-climate) roles for the same profile.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    query = (query or PROFILE_QUERY).strip()
    for prefix in ("find ", "search for ", "looking for ", "jobs in ", "jobs for "):
        query = query.lower().replace(prefix, "")
    query = query.replace("in the climate and green tech sector", "").strip() or PROFILE_QUERY

    sources = _sources or ALL_SOURCES
    label = "target-role profile" if query == PROFILE_QUERY else f"'{query}'"
    print(
        f"🔍 Searching {len(sources)} sources for {label}" + (f" in {location}" if location else "")
    )

    def call_source(fn):
        # Sources that report fine-grained progress (connections) get the callback.
        if "_progress" in inspect.signature(fn).parameters:
            return fn(query, location, _progress=_progress)
        return fn(query, location)

    all_jobs = []
    with ThreadPoolExecutor(max_workers=len(sources)) as ex:
        futures = {ex.submit(call_source, fn): fn.__name__ for fn in sources}
        for done, future in enumerate(as_completed(futures), 1):
            name = futures[future].removeprefix("search_")
            try:
                jobs = future.result()
                all_jobs.extend(jobs)
                if _progress:
                    _progress("sources", done, len(sources), f"{name}:{len(jobs)}")
            except Exception as e:
                print(f"   {futures[future]} error: {e}")
                if _progress:
                    _progress("sources", done, len(sources), f"{name}:error")

    # Dedupe by cross-device identity (company|title, URL fallback) — same key
    # save_jobs and the sync layer use, so one search never yields two rows that
    # would later collapse to one job. Stale postings are dropped *before* keys
    # are registered (a poisoned key used to swallow fresh reposts), and same-key
    # variants resolve to the freshest one.
    from sync import norm_key

    seen_keys: set = set()
    unique_jobs = _dedup_and_filter_stale(all_jobs, seen_keys, max_age_days)

    dropped_by_policy = []
    if _is_preferred_location(location):
        kept = []
        for job in unique_jobs:
            ok, reason = _preferred_location_verdict(job)
            if ok:
                kept.append(job)
            else:
                dropped_by_policy.append({**job, "drop_reason": reason})
        unique_jobs = kept
        print(
            f"   Location policy: {len(kept)}/{len(kept) + len(dropped_by_policy)} jobs kept"
            f" (dropped → {LOCATION_DEBUG_CSV})"
        )
        _write_location_debug(dropped_by_policy)

    # Dead postings out first — no point fetching or scoring roles already filled.
    # Undated jobs (wttj, remoteimpact, scrape sources) get checked too: without a
    # date they used to bypass the liveness pass entirely.
    unique_jobs, dropped = verify_jobs_online(
        unique_jobs, verify_unknown_dates=True, _progress=_progress
    )

    # Full descriptions next: validation scores the real posting text, and the
    # TUI cards get their previews for free.
    fetch_descriptions(unique_jobs, _progress=_progress)

    def is_general(job):
        return (
            job["fit_score"] >= max(min_fit, NONCLIMATE_MIN_FIT)
            and job["climate_score"] < min_climate
        )

    def keeps(job):
        if non_climate:
            return is_general(job)
        return job["fit_score"] >= min_fit and job["climate_score"] >= min_climate

    # Jev: typed fit/startup/climate scores + region-lock check on the full description.
    import jev

    if jev.enabled():
        region = (
            _home_region_label()
            if _is_preferred_location(location)
            else (location or "Europe")
        )
        got = jev.annotate(unique_jobs, region, _progress=_progress)
        unique_jobs, jev_dropped = _drop_region_locked(unique_jobs, location)
        print(f"   Jev: {got} judged, {len(jev_dropped)} dropped as region-locked")
        if jev_dropped:
            _write_location_debug(dropped_by_policy + jev_dropped)
        # Jev already scored these: skip the LLM for the ones that can't pass, so it
        # only runs (for the one-line reason) on survivors and on unjudged jobs.
        before = len(unique_jobs)
        unique_jobs = [
            j
            for j in unique_jobs
            if not j.get("_jev")
            or (_apply_jev_scores(j), keeps(j) or is_general(j))[1]
        ]
        print(f"   Jev thresholds: {before - len(unique_jobs)} dropped")

    print(f"   {len(unique_jobs)} unique recent jobs; validating with LLM...")

    batch_size = 15
    batches = [unique_jobs[i : i + batch_size] for i in range(0, len(unique_jobs), batch_size)]
    validated = []
    general = []  # fit the role but fail the climate gate: kept for the "general roles" view
    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, (batch, validations) in enumerate(
            zip(batches, ex.map(lambda b: validate_jobs_batch(b, query), batches)), 1
        ):
            for job, val in zip(batch, validations):
                job.update(val)
                _apply_jev_scores(job)
                if keeps(job):
                    validated.append(job)
                elif is_general(job):
                    general.append(job)
            if _progress:
                _progress("validate", i, len(batches), f"{len(validated)} kept")

    from candidate_profile import is_pearl

    def rank(j):
        return (
            not is_pearl(j),  # pearls (supermatches) first
            -j["fit_score"],
            -j["climate_score"],
            -j.get("startup_score", 0),
            j.get("posted_date", ""),
        )

    validated.sort(key=rank)
    general.sort(key=rank)
    final = validated[:max_results]
    if non_climate:
        print(
            f"   Final: {len(final)} jobs (general roles: fit ≥ "
            f"{max(min_fit, NONCLIMATE_MIN_FIT)}, climate < {min_climate})"
        )
    else:
        print(f"   Final: {len(final)} jobs (fit ≥ {min_fit}, climate ≥ {min_climate})")
    if _progress:
        _progress("done", 1, 1, f"{len(final)} jobs")

    return {
        "status": "success",
        "query": query,
        "location": location,
        "total_found": len(final),
        "offline_dropped": dropped,
        "jobs": final,
        "general_jobs": [] if non_climate else general[:max_results],
    }


# --- PERSISTENCE -----------------------------------------------------------

CSV_COLUMNS = [
    "title",
    "company",
    "location",
    "salary",
    "posted_date",
    "wfh_policy",
    "fit_score",
    "startup_score",
    "climate_score",
    "reason",
    "contact",
    "source",
    "url",
]


def save_jobs(jobs: list, filepath: str = "jobs.csv", _push: bool = True) -> dict:
    """Save job dicts to CSV, deduplicating against existing rows by the job's
    sync identity: its normalized URL, or company + title only when there is no URL.

    This MUST match norm_key / decisionKey in sync.py & Job.kt — the Firestore
    doc id and the swipe-decision key are both URL-first. Collapsing two rows
    that share company+title but carry different URLs would give them different
    sync/decision keys, so each device would keep a different variant and their
    decisions would never line up (undecided counts diverge across devices).

    New rows are also pushed to the cross-device sync DB (sync.py) in the
    background, unless _push=False (used by sync_down itself to avoid loops).
    """
    try:
        if not jobs:
            return {"status": "success", "added": 0}

        existing = []
        try:
            with open(filepath, "r", newline="", encoding="utf-8") as f:
                existing = list(csv.DictReader(f))
        except FileNotFoundError:
            pass

        from sync import norm_key

        def ident(j):
            return norm_key(j.get("url", ""), j.get("company", ""), j.get("title", ""))

        existing_keys = {ident(j) for j in existing}
        existing_keys.discard("")
        new_rows, skipped = [], 0
        for job in jobs:
            k = ident(job)
            if k and k in existing_keys:
                skipped += 1
                continue
            if k:
                existing_keys.add(k)
            new_rows.append(job)

        if not new_rows:
            return {"status": "success", "added": 0, "skipped": skipped}

        all_keys = set()
        for j in existing + new_rows:
            all_keys.update(j.keys())
        all_keys.discard("snippet")  # too long for CSV
        all_keys -= {k for k in all_keys if k.startswith("_")}  # _desc, _num, … are runtime-only
        fieldnames = [k for k in CSV_COLUMNS if k in all_keys] + sorted(
            k for k in all_keys if k not in CSV_COLUMNS
        )

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(existing + new_rows)

        if _push:
            try:
                from sync import push_jobs_async

                push_jobs_async(new_rows)
            except Exception:
                pass

        return {
            "status": "success",
            "added": len(new_rows),
            "skipped": skipped,
            "total_in_file": len(existing) + len(new_rows),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def save_csv(jobs_data: str) -> dict:
    """Append CSV-formatted job rows to jobs.csv, deduping by URL.

    Args:
        jobs_data: CSV-formatted string starting with a header row.
    """
    from io import StringIO

    try:
        return save_jobs(list(csv.DictReader(StringIO(jobs_data))))
    except Exception as e:
        return {"status": "error", "message": str(e)}


# --- COMPANY RESEARCH ------------------------------------------------------

CACHE_FILE = "company_cache.json"


def get_company_info(company_name: str) -> dict:
    """Search for company info (greentech focus, WFH policy) with caching."""
    if not company_name:
        return {"status": "error", "message": "No company name provided"}

    try:
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    if company_name in cache:
        return cache[company_name]

    print(f"   Researching {company_name}...")
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            about = list(
                ddgs.text(f"{company_name} company climate sustainability mission", max_results=3)
            )
            wfh = list(ddgs.text(f"{company_name} remote work policy", max_results=2))
        info = {
            "status": "success",
            "company": company_name,
            "about_snippets": " ".join(r.get("body", "") for r in about)[:800],
            "wfh_snippets": " ".join(r.get("body", "") for r in wfh)[:500],
        }
        cache[company_name] = info
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
        return info
    except Exception as e:
        return {"status": "error", "error": str(e)}


# --- DIRECT (NON-AGENT) PROFILE SEARCH --------------------------------------


def run_profile_search(location: str = "", query: str = "", non_climate: bool = False) -> None:
    """Deterministic search + save, no agent loop. Used by `cli.py --profile`."""
    try:
        from sync import sync_down, sync_up

        sync_down(verbose=True)
        # Flush any decisions/jobs a prior session's async push never sent.
        sync_up(verbose=True)
    except Exception:
        pass
    result = search_jobs(query=query or PROFILE_QUERY, location=location, non_climate=non_climate)
    jobs = result["jobs"]
    saved = save_jobs(jobs + result.get("general_jobs", []))
    print(
        f"\n💾 Saved {saved.get('added', 0)} new jobs "
        f"({saved.get('skipped', 0)} already known) → jobs.csv"
    )
    if jobs:
        from candidate_profile import is_pearl

        print("\nTop matches (fit/startup/climate):")
        for j in jobs[:15]:
            pearl = "🦪 " if is_pearl(j) else ""
            print(
                f"  {pearl}[{j['fit_score']}/{j.get('startup_score', '?')}/{j['climate_score']}]"
                f" {j['title']} — {j['company']} ({j['location'] or j['wfh_policy']})"
            )
            print(f"      {j['url']}")


def run_connection_search(location: str = "", query: str = "") -> None:
    """Search the job boards of companies where the candidate knows someone.

    Same deterministic shape as run_profile_search, but the only source is
    connections.search_connections (see connections.py).
    """
    from connections import search_connections

    try:
        from sync import sync_down, sync_up

        sync_down(verbose=True)
        sync_up(verbose=True)
    except Exception:
        pass
    result = search_jobs(
        query=query or PROFILE_QUERY, location=location, _sources=[search_connections]
    )
    jobs = result["jobs"]
    saved = save_jobs(jobs + result.get("general_jobs", []))
    print(
        f"\n💾 Saved {saved.get('added', 0)} new jobs "
        f"({saved.get('skipped', 0)} already known) → jobs.csv"
    )
    if jobs:
        print("\nTop matches (fit/startup/climate):")
        for j in jobs[:15]:
            print(
                f"  🤝 [{j['fit_score']}/{j.get('startup_score', '?')}/{j['climate_score']}]"
                f" {j['title']} — {j['company']} ({j['location'] or j['wfh_policy']})"
            )
            if j.get("contact"):
                extra = f" (+{j['contacts_n'] - 1} more)" if j.get("contacts_n", 1) > 1 else ""
                print(f"      via {j['contact']}{extra}")
            print(f"      {j['url']}")


# --- AGENT ------------------------------------------------------------------


class ClaudeAgent:
    """Manual tool-use loop against the Anthropic Messages API."""

    def __init__(self, model="claude-sonnet-5", tools=None, system_prompt=""):
        import llm

        if not llm.api_key():
            print(f"⚠️ WARNING: {llm.api_key_env()} not found in environment.")
        self.model = model
        self.tools = tools or []
        self.system_prompt = system_prompt
        self.tool_map = {tool.__name__: tool for tool in self.tools}

    def _get_tool_schemas(self):
        schemas = []
        for tool in self.tools:
            signature = inspect.signature(tool)
            input_schema = {"type": "object", "properties": {}, "required": []}
            for name, param in signature.parameters.items():
                if name.startswith("_"):  # internal-only params (e.g. progress callbacks)
                    continue
                p_type = "string"
                if param.annotation is int:
                    p_type = "integer"
                elif param.annotation is bool:
                    p_type = "boolean"
                input_schema["properties"][name] = {
                    "type": p_type,
                    "description": f"Parameter {name}",
                }
                if param.default == inspect.Parameter.empty:
                    input_schema["required"].append(name)
            schemas.append(
                {
                    "name": tool.__name__,
                    "description": tool.__doc__ or "",
                    "input_schema": input_schema,
                }
            )
        return schemas

    def run(self, prompt: str):
        client = _get_anthropic_client()
        messages = [{"role": "user", "content": prompt}]
        tool_schemas = self._get_tool_schemas()

        for _ in range(10):
            response = client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=self.system_prompt,
                thinking={"type": "enabled", "budget_tokens": 2048},  # haiku: no adaptive mode
                tools=tool_schemas,
                messages=messages,
            )
            if response.stop_reason != "tool_use":
                return next((b.text for b in response.content if b.type == "text"), "")

            # Echo the assistant turn (thinking + tool_use blocks) back unchanged,
            # then answer every tool call in a single user message.
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                try:
                    result = self.tool_map[block.name](**block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str)[:60000],
                        }
                    )
                except Exception as e:
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Error: {e}",
                            "is_error": True,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
        return "Max iterations reached."


root_agent = ClaudeAgent(
    model="claude-sonnet-5",
    system_prompt=f"""You are a job search agent finding climate/green-tech jobs for this candidate:

{CANDIDATE_PROFILE}

WORKFLOW:
1. Call search_jobs with the job title (or "profile" to search all the candidate's target
   roles) and optional location. It queries 9 sources in parallel, scores each job for
   role fit (0-5), startup-ness (0-5), and climate relevance (0-5), and returns only
   good matches.
2. Convert results to CSV and call save_csv. Header row:
   title,company,location,salary,posted_date,wfh_policy,fit_score,startup_score,climate_score,reason,source,url
3. Reply with a brief summary: top matches first (highest fit+climate), grouped by source,
   with links.

RULES:
- Pass structured data straight through; do not invent or drop fields.
- Optionally call get_company_info for promising companies the candidate may not know.
""",
    tools=[search_jobs, get_company_info, save_csv],
)


if __name__ == "__main__":
    import sys

    run_profile_search(location=sys.argv[1] if len(sys.argv) > 1 else "")
