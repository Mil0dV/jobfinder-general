from jobfinder.sources.base import (
    _HTTP_HEADERS,
    _location_terms,
    _make_job,
    _matches_location,
    _title_matches,
)

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
    "ch": "Switzerland",
    "de": "Germany",
    "nl": "Netherlands",
    "at": "Austria",
    "fr": "France",
    "gb": "United Kingdom",
    "ie": "Ireland",
    "es": "Spain",
    "it": "Italy",
    "be": "Belgium",
    "dk": "Denmark",
    "se": "Sweden",
    "no": "Norway",
    "fi": "Finland",
    "pt": "Portugal",
    "pl": "Poland",
    "us": "United States",
    "ca": "Canada",
    "au": "Australia",
    "in": "India",
    "is": "Iceland",
    "ae": "United Arab Emirates",
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
