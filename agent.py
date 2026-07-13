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
from datetime import datetime, timezone
from dotenv import load_dotenv

from candidate_profile import CANDIDATE_PROFILE, PROFILE_QUERY_SEEDS, matches_target_roles

load_dotenv()

PROFILE_QUERY = "profile"  # sentinel: search for the candidate's target roles

# --- SHARED HELPERS -------------------------------------------------------

COUNTRY_TERMS = {
    "germany": [
        "germany",
        "deutschland",
        "berlin",
        "munich",
        "münchen",
        "hamburg",
        "frankfurt",
        "cologne",
        "köln",
        "stuttgart",
        "leipzig",
        "de",
    ],
    "switzerland": [
        "switzerland",
        "schweiz",
        "suisse",
        "zurich",
        "zürich",
        "geneva",
        "genf",
        "basel",
        "bern",
        "lausanne",
        "zug",
        "ch",
    ],
    "austria": ["austria", "österreich", "vienna", "wien", "graz", "linz", "at"],
    "netherlands": [
        "netherlands",
        "nederland",
        "amsterdam",
        "rotterdam",
        "utrecht",
        "the hague",
        "eindhoven",
        "nl",
    ],
    "europe": ["europe", "eu", "emea", "european"],
}

REMOTE_HINTS = ["remote", "anywhere", "distributed", "work from home", "home office"]


def _location_terms(location: str) -> set:
    """Expand a location string into matchable terms (cities, country codes)."""
    if not location:
        return set()
    loc = location.lower().strip()
    terms = {loc}
    for country, cities in COUNTRY_TERMS.items():
        if loc == country or loc in cities:
            terms.add(country)
            terms.update(cities)
    return terms


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


def search_arbeitnow(query: str, location: str = "") -> list:
    """Search Arbeitnow API for EU/DACH jobs. Free, no API key required."""
    import requests

    try:
        resp = requests.get("https://www.arbeitnow.com/api/job-board-api", timeout=15)
        resp.raise_for_status()
        terms = _location_terms(location)
        jobs = []
        for job in resp.json().get("data", []):
            title = job.get("title", "")
            if not _title_matches(title, query):
                continue
            if not _matches_location(job.get("location", ""), terms):
                continue
            jobs.append(
                _make_job(
                    title=title,
                    company=job.get("company_name"),
                    url=job.get("url"),
                    location=job.get("location"),
                    posted_date=str(job.get("created_at", ""))[:10],
                    wfh_policy="Remote" if job.get("remote") else "On-site",
                    snippet=re.sub(r"<[^>]+>", " ", job.get("description", "")),
                    source="arbeitnow",
                )
            )
        print(f"   Arbeitnow: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   Arbeitnow error: {e}")
        return []


def search_remoteok(query: str, location: str = "") -> list:
    """Search RemoteOK JSON feed for remote jobs."""
    import requests

    try:
        resp = requests.get("https://remoteok.com/api", headers=_HTTP_HEADERS, timeout=15)
        resp.raise_for_status()
        jobs = []
        for job in resp.json():
            if not isinstance(job, dict) or "position" not in job:
                continue
            title = job.get("position", "")
            if not _title_matches(title, query):
                continue
            salary = "Not listed"
            if job.get("salary_min") and job.get("salary_max"):
                salary = f"${job['salary_min']:,} - ${job['salary_max']:,}"
            jobs.append(
                _make_job(
                    title=title,
                    company=job.get("company"),
                    url=job.get("url"),
                    location=job.get("location") or "Remote",
                    salary=salary,
                    posted_date=str(job.get("date", ""))[:10],
                    wfh_policy="Remote",
                    snippet=re.sub(r"<[^>]+>", " ", job.get("description", "")),
                    source="remoteok",
                )
            )
        print(f"   RemoteOK: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   RemoteOK error: {e}")
        return []


def search_adzuna(query: str, location: str = "") -> list:
    """Search Adzuna API. Requires ADZUNA_APP_ID and ADZUNA_API_KEY in .env."""
    import requests

    app_id, api_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_API_KEY")
    if not app_id or not api_key:
        print("   Adzuna: skipped (no API key configured)")
        return []

    country_map = {
        "switzerland": "ch",
        "germany": "de",
        "austria": "at",
        "netherlands": "nl",
        "uk": "gb",
        "united kingdom": "gb",
        "france": "fr",
        "usa": "us",
    }
    loc = (location or "").lower()
    country_code = "ch"
    for name, code in country_map.items():
        if name in loc or any(city in loc for city in COUNTRY_TERMS.get(name, [])):
            country_code = code
            break

    seeds = PROFILE_QUERY_SEEDS if query == PROFILE_QUERY else [query]
    jobs, seen = [], set()
    for seed in seeds:
        try:
            params = {
                "app_id": app_id,
                "app_key": api_key,
                "what": seed,
                "results_per_page": 50,
                "max_days_old": 60,
                "sort_by": "date",
            }
            if location and loc not in (country_map.keys() | {country_code}):
                params["where"] = location
            resp = requests.get(
                f"https://api.adzuna.com/v1/api/jobs/{country_code}/search/1",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            for job in resp.json().get("results", []):
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
                        wfh_policy="Remote" if "remote" in (title + desc).lower() else "On-site",
                        snippet=desc,
                        source="adzuna",
                    )
                )
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
            "x-time-zone": "Europe/Zurich",
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
CLIMATEBASE_COUNTRY = {
    "switzerland": "Switzerland",
    "germany": "Germany",
    "austria": "Austria",
    "netherlands": "Netherlands",
    "uk": "United Kingdom",
    "france": "France",
}


def search_climatebase(query: str, location: str = "") -> list:
    """Search Climatebase (~14k climate jobs) via its public Algolia index."""
    import requests

    endpoint = f"https://{CLIMATEBASE_ALGOLIA_APP}-dsn.algolia.net/1/indexes/Job_production/query"
    headers = {
        "X-Algolia-API-Key": CLIMATEBASE_ALGOLIA_KEY,
        "X-Algolia-Application-Id": CLIMATEBASE_ALGOLIA_APP,
        "Content-Type": "application/json",
    }

    facet_filters = []
    loc = (location or "").lower()
    for term, country in CLIMATEBASE_COUNTRY.items():
        if term in loc or any(city in loc for city in COUNTRY_TERMS.get(term, [])):
            # OR group: jobs in the country, or remote anywhere
            facet_filters = [[f"countries:{country}", "remote_preferences:Remote"]]
            break

    seeds = PROFILE_QUERY_SEEDS if query == PROFILE_QUERY else [query]
    jobs, seen = [], set()
    for seed in seeds:
        try:
            body = {"query": seed, "hitsPerPage": 50}
            if facet_filters:
                body["facetFilters"] = facet_filters
            resp = requests.post(endpoint, headers=headers, json=body, timeout=15)
            resp.raise_for_status()
            for hit in resp.json().get("hits", []):
                title = hit.get("title", "")
                job_id = hit.get("objectID", "")
                if job_id in seen or not _title_matches(title, query):
                    continue
                seen.add(job_id)
                url = hit.get("how_to_apply") or f"https://climatebase.org/job/{job_id}"
                if not url.startswith("http"):
                    url = f"https://climatebase.org/job/{job_id}"
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
        except Exception as e:
            print(f"   Climatebase error ({seed}): {e}")
    print(f"   Climatebase: {len(jobs)} matching jobs")
    return jobs


def search_jobsincarbon(query: str, location: str = "") -> list:
    """Search Jobs in Carbon (carbon-markets niceboard) via its sitemap."""
    import requests

    try:
        resp = requests.get(
            "https://jobsincarbon.niceboard.co/sitemap-jobs.xml",
            headers=_HTTP_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        urls = re.findall(r"<loc>(https://[^<]+/job/[^<]+)</loc>", resp.text)

        matches = []
        for url in urls:
            slug = url.rsplit("/job/", 1)[-1]
            slug_text = re.sub(r"^\d+-", "", slug).replace("-", " ")
            if _title_matches(slug_text, query):
                matches.append((url, slug_text))

        jobs = []
        for url, slug_text in matches[:15]:  # cap page fetches
            title, company = slug_text.title(), "Unknown"
            try:
                page = requests.get(url, headers=_HTTP_HEADERS, timeout=10).text
                og = re.search(r'<meta property="og:title" content="([^"]+)"', page)
                if og:
                    # niceboard og:title format: "Title at Company"
                    parts = og.group(1).rsplit(" at ", 1)
                    title = parts[0].strip()
                    if len(parts) == 2:
                        company = parts[1].strip()
                loc_m = re.search(r'"jobLocation"[^}]*"address[^}]*"name"\s*:\s*"([^"]+)"', page)
                job_location = loc_m.group(1) if loc_m else "See posting"
            except Exception:
                job_location = "See posting"
            if not _matches_location(job_location, _location_terms(location)):
                continue
            jobs.append(
                _make_job(
                    title=title,
                    company=company,
                    url=url,
                    location=job_location,
                    wfh_policy="Unknown",
                    source="jobsincarbon",
                )
            )
        print(f"   JobsInCarbon: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   JobsInCarbon error: {e}")
        return []


def search_remoteimpact(query: str, location: str = "") -> list:
    """Search Remote Impact's climate & environment category (remote impact jobs)."""
    import requests

    jobs = []
    try:
        for page in range(1, 4):
            suffix = "" if page == 1 else f"page/{page}/"
            resp = requests.get(
                f"https://remoteimpact.org/jobs/category/climate-environment/{suffix}",
                headers=_HTTP_HEADERS,
                timeout=15,
            )
            if resp.status_code != 200:
                break
            cards = re.findall(
                r'<a href="(/jobs/[^"]+)"[^>]*>([^<]+)</a>\s*</h2>\s*' r"<p[^>]*>([^<]*)</p>",
                resp.text,
            )
            if not cards:
                break
            for path, title, company in cards:
                title = title.strip()
                if not _title_matches(title, query):
                    continue
                jobs.append(
                    _make_job(
                        title=title,
                        company=company.strip() or "Unknown",
                        url=f"https://remoteimpact.org{path}",
                        location="Remote",
                        wfh_policy="Remote",
                        source="remoteimpact",
                    )
                )
        print(f"   RemoteImpact: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   RemoteImpact error: {e}")
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
}

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
        import anthropic

        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def _validation_json(prompt: str) -> dict | None:
    """Score a validation batch via Claude with a JSON-schema-constrained response."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        response = _get_anthropic_client().messages.create(
            model=VALIDATION_MODEL,
            max_tokens=2000,
            output_config={"format": {"type": "json_schema", "schema": _VALIDATION_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        return {str(ev["id"]): ev for ev in data.get("evaluations", [])}
    except Exception as e:
        print(f"   Validation error ({VALIDATION_MODEL}): {e}")
        return None


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
  hands-on technical focus, product/vision ownership, and team leadership.
  5 = exact target role at right seniority (e.g. Head of Product, CTO, Founding Engineer,
      Technical/Platform PM, Director of Engineering, EiR) with real ownership
  3 = adjacent role or seniority slightly off (e.g. plain Product Manager, Engineering
      Manager), or ownership unclear
  0-1 = wrong role family (sales, marketing, hardware/mechanical, junior roles, internships)
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
    _progress=None,
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
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    query = (query or PROFILE_QUERY).strip()
    for prefix in ("find ", "search for ", "looking for ", "jobs in ", "jobs for "):
        query = query.lower().replace(prefix, "")
    query = query.replace("in the climate and green tech sector", "").strip() or PROFILE_QUERY

    label = "target-role profile" if query == PROFILE_QUERY else f"'{query}'"
    print(
        f"🔍 Searching {len(ALL_SOURCES)} sources for {label}"
        + (f" in {location}" if location else "")
    )

    all_jobs = []
    with ThreadPoolExecutor(max_workers=len(ALL_SOURCES)) as ex:
        futures = {ex.submit(fn, query, location): fn.__name__ for fn in ALL_SOURCES}
        for done, future in enumerate(as_completed(futures), 1):
            name = futures[future].removeprefix("search_")
            try:
                jobs = future.result()
                all_jobs.extend(jobs)
                if _progress:
                    _progress("sources", done, len(ALL_SOURCES), f"{name}:{len(jobs)}")
            except Exception as e:
                print(f"   {futures[future]} error: {e}")
                if _progress:
                    _progress("sources", done, len(ALL_SOURCES), f"{name}:error")

    # Dedupe by normalized URL, then by (company, title)
    seen_urls, seen_keys, unique_jobs = set(), set(), []
    for job in all_jobs:
        url = (job.get("url") or "").split("?")[0].strip().rstrip("/")
        key = (job.get("company", "").lower(), job.get("title", "").lower())
        if (url and url in seen_urls) or key in seen_keys:
            continue
        if url:
            seen_urls.add(url)
        seen_keys.add(key)
        age = _job_age_days(job.get("posted_date"))
        if age is not None and age > max_age_days:
            continue
        unique_jobs.append(job)

    # Dead postings out first — no point fetching or scoring roles already filled.
    unique_jobs, dropped = verify_jobs_online(unique_jobs, _progress=_progress)

    # Full descriptions next: validation scores the real posting text, and the
    # TUI cards get their previews for free.
    fetch_descriptions(unique_jobs, _progress=_progress)

    print(f"   {len(unique_jobs)} unique recent jobs; validating with LLM...")

    batch_size = 15
    batches = [unique_jobs[i : i + batch_size] for i in range(0, len(unique_jobs), batch_size)]
    validated = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, (batch, validations) in enumerate(
            zip(batches, ex.map(lambda b: validate_jobs_batch(b, query), batches)), 1
        ):
            for job, val in zip(batch, validations):
                job.update(val)
                if job["fit_score"] >= min_fit and job["climate_score"] >= min_climate:
                    validated.append(job)
            if _progress:
                _progress("validate", i, len(batches), f"{len(validated)} kept")

    from candidate_profile import is_pearl

    validated.sort(
        key=lambda j: (
            not is_pearl(j),  # pearls (supermatches) first
            -j["fit_score"],
            -j["climate_score"],
            -j.get("startup_score", 0),
            j.get("posted_date", ""),
        )
    )
    final = validated[:max_results]
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
    "source",
    "url",
]


def save_jobs(jobs: list, filepath: str = "jobs.csv", _push: bool = True) -> dict:
    """Save job dicts to CSV, deduplicating by normalized URL against existing rows.

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

        def norm_url(u):
            return (u or "").split("?")[0].strip().rstrip("/")

        existing_urls = {norm_url(j.get("url")) for j in existing}
        new_rows, skipped = [], 0
        for job in jobs:
            u = norm_url(job.get("url"))
            if u and u not in existing_urls:
                new_rows.append(job)
                existing_urls.add(u)
            else:
                skipped += 1

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


def run_profile_search(location: str = "", query: str = "") -> None:
    """Deterministic search + save, no agent loop. Used by `cli.py --profile`."""
    try:
        from sync import sync_down

        sync_down(verbose=True)
    except Exception:
        pass
    result = search_jobs(query=query or PROFILE_QUERY, location=location)
    jobs = result["jobs"]
    saved = save_jobs(jobs)
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


# --- AGENT ------------------------------------------------------------------


class ClaudeAgent:
    """Manual tool-use loop against the Anthropic Messages API."""

    def __init__(self, model="claude-opus-4-8", tools=None, system_prompt=""):
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("⚠️ WARNING: ANTHROPIC_API_KEY not found in environment.")
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
                thinking={"type": "adaptive"},
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
