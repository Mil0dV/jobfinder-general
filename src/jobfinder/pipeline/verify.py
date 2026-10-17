"""Liveness check for older postings."""

import re

from jobfinder.sources.base import _HTTP_HEADERS, _job_age_days

# Postings older than this get an is-it-still-online check during search.
VERIFY_AGE_DAYS = 30
_OFFLINE_STATUSES = {404, 410}


def verify_jobs_online(
    jobs: list, verify_unknown_dates: bool = False, _progress=None, check_all: bool = False
) -> tuple[list, int]:
    """Drop jobs whose posting URL is definitively gone. Returns (kept, dropped_count).

    Only checks postings older than VERIFY_AGE_DAYS (and, optionally, ones with
    no parseable date) unless `check_all` is set (used for saved/liked jobs,
    which may have gone offline since they were found). A posting counts as offline ONLY on a definitive
    404/410. Crawl refusals (401/403/405/429), bot walls, server errors, and
    timeouts are NOT evidence the job is gone — those get the benefit of the
    doubt and the job is kept.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import requests

    def needs_check(job):
        if not (job.get("url") or "").startswith("http"):
            return False
        if check_all:
            return True
        age = _job_age_days(job.get("posted_date"))
        if age is None:
            return verify_unknown_dates
        return age > VERIFY_AGE_DAYS

    to_check = [j for j in jobs if needs_check(j)]
    if not to_check:
        return jobs, 0

    def ashby_online(url):
        """Ashby is a JS app that answers 200 for dead postings; ask its GraphQL instead.

        Returns None when the URL isn't an Ashby posting or the answer is unclear.
        """
        m = re.match(r"https?://jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", url)
        if not m:
            return None
        query = (
            "query ApiJobPosting($organizationHostedJobsPageName: String!, "
            "$jobPostingId: String!) { jobPosting(organizationHostedJobsPageName: "
            "$organizationHostedJobsPageName, jobPostingId: $jobPostingId) { id } }"
        )
        resp = requests.post(
            "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting",
            json={
                "operationName": "ApiJobPosting",
                "variables": {
                    "organizationHostedJobsPageName": m.group(1),
                    "jobPostingId": m.group(2),
                },
                "query": query,
            },
            timeout=8,
        )
        data = resp.json().get("data")
        if resp.status_code != 200 or not isinstance(data, dict) or "jobPosting" not in data:
            return None
        return data["jobPosting"] is not None

    def embedded_greenhouse_online(url):
        """Company career pages embedding Greenhouse (?gh_jid=N) answer 200 for dead jobs.

        Read the board token off the page's embed script, then ask the Greenhouse
        board API about the job. None when it isn't such a URL or the answer is unclear.
        """
        m = re.search(r"[?&]gh_jid=(\d+)", url)
        if not m:
            return None
        page = requests.get(url, headers=_HTTP_HEADERS, timeout=8)
        token = re.search(r"greenhouse\.io/embed/job_board/js\?for=([\w-]+)", page.text)
        if page.status_code != 200 or not token:
            return None
        api = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{token.group(1)}/jobs/{m.group(1)}",
            timeout=8,
        )
        if api.status_code == 404:
            return False
        return True if api.status_code == 200 else None

    def is_online(job):
        try:
            for check in (ashby_online, embedded_greenhouse_online):
                verdict = check(job["url"])
                if verdict is not None:
                    return verdict
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
