import json
import re
from datetime import datetime, timezone

from jobfinder.sources.base import _HTTP_HEADERS, _make_job, _title_matches

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
