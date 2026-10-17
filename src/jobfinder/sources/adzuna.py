import os

from jobfinder.config.profile import (
    ADZUNA_COUNTRIES,
    ADZUNA_DEFAULT,
    COUNTRY_TERMS,
    PROFILE_QUERY_SEEDS,
)
from jobfinder.sources.base import (
    PROFILE_QUERY,
    _is_preferred_location,
    _make_job,
    _search_countries,
    _title_matches,
)

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
        country_codes = [ADZUNA_COUNTRIES[c] for c in _search_countries() if c in ADZUNA_COUNTRIES]
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
