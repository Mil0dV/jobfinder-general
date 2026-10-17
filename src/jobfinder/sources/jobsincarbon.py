import json
import re

from jobfinder.sources.base import (
    _HTTP_HEADERS,
    _location_terms,
    _make_job,
    _matches_location,
    _title_matches,
)

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
    from concurrent.futures import ThreadPoolExecutor

    import requests

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
