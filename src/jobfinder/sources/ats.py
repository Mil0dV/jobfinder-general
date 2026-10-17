from datetime import datetime, timezone

from jobfinder.sources.base import _location_terms, _make_job, _matches_location, _title_matches

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
    from concurrent.futures import ThreadPoolExecutor

    import requests

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
    from concurrent.futures import ThreadPoolExecutor

    import requests

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
