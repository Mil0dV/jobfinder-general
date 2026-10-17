import re

from jobfinder.sources.base import (
    _HTTP_HEADERS,
    _location_terms,
    _make_job,
    _matches_location,
    _title_matches,
)

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
