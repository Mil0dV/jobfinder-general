import json
import os
import re
from datetime import datetime, timezone

from jobfinder.sources.base import (
    _HTTP_HEADERS,
    PROFILE_QUERY,
    _is_preferred_location,
    _job_age_days,
    _location_terms,
    _make_job,
    _matches_location,
    _search_countries,
    _title_matches,
)

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
    from html import unescape

    import requests

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
