import re
from datetime import datetime, timedelta, timezone

from jobfinder.config.profile import (
    CLIMATEBASE_COUNTRIES,
    COUNTRY_TERMS,
    HOME_COUNTRIES,
    PROFILE_QUERY_SEEDS,
)
from jobfinder.sources.base import PROFILE_QUERY, _is_preferred_location, _make_job, _title_matches

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
    from concurrent.futures import ThreadPoolExecutor

    import requests

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
