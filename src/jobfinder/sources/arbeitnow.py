import re
from datetime import datetime, timezone

from jobfinder.sources.base import (
    _job_age_days,
    _location_terms,
    _make_job,
    _matches_location,
    _title_matches,
)

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
