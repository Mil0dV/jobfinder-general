"""Welcome to the Jungle (app.welcometothejungle.com) — optional, login-gated source.

WTTJ's job app has no public API, so it needs a signed-in session. `login()` opens
Playwright's bundled Chromium with a persistent profile (`.jobfinder_wttj_profile/`,
gitignored); the user signs in once (Google SSO works — it is a real browser window)
and the session lives in that folder. Playwright is an optional extra:

    uv sync --extra wttj && uv run playwright install chromium
"""

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from jobfinder.config.paths import PROJECT_ROOT
from jobfinder.sources.base import _location_terms, _make_job, _matches_location, _title_matches

PROFILE_DIR = Path(os.getenv("WTTJ_PROFILE_DIR") or PROJECT_ROOT / ".jobfinder_wttj_profile")
CAPTURE_FILE = PROJECT_ROOT / ".jobfinder_wttj_capture.jsonl"
APP_URL = "https://app.welcometothejungle.com/jobs"
_STATIC_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".woff", ".woff2")
_NOISE_HOSTS = ("google", "gstatic", "doubleclick", "segment", "sentry", "datadog", "amplitude")


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "Playwright is not installed. Run:\n"
            "  uv sync --extra wttj && uv run playwright install chromium"
        )
    return sync_playwright


def _shape(value, depth=0):
    """Structure of a JSON value without its content (keys, list lengths, types)."""
    if isinstance(value, dict):
        if depth >= 3:
            return "{…}"
        return {k: _shape(v, depth + 1) for k, v in list(value.items())[:25]}
    if isinstance(value, list):
        return [f"len={len(value)}", _shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def _log_response(response, out):
    """Append one API call (URL, request body, response *shape*) — never headers or cookies."""
    req = response.request
    if req.resource_type not in ("xhr", "fetch"):
        return
    parts = urlsplit(req.url)
    if parts.path.lower().endswith(_STATIC_EXT) or any(n in parts.netloc for n in _NOISE_HOSTS):
        return
    entry = {
        "method": req.method,
        "url": f"{parts.scheme}://{parts.netloc}{parts.path}"
        + (f"?{parts.query}" if parts.query else ""),
        "status": response.status,
        "request_body": (req.post_data or "")[:20000] or None,
    }
    try:
        entry["response_shape"] = _shape(response.json())
    except Exception:
        entry["response_shape"] = None
    out.write(json.dumps(entry, ensure_ascii=False) + "\n")
    out.flush()


def login(capture: bool = True) -> None:
    """Open a browser window so the user can sign in to WTTJ; the session persists on disk.

    With `capture`, the app's XHR/fetch calls are logged to CAPTURE_FILE while the window is
    open (URLs, request bodies and response shapes only) — used to find the jobs API.
    """
    sync_playwright = _require_playwright()
    PROFILE_DIR.mkdir(exist_ok=True)
    print(
        "A browser window will open. Sign in to Welcome to the Jungle (Google SSO is fine),\n"
        "open the jobs page, run a search or two, then close the window to finish."
    )
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport=None,
            # Google refuses sign-in from browsers that announce they are automated.
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        out = open(CAPTURE_FILE, "a", encoding="utf-8") if capture else None
        if out:
            ctx.on("response", lambda r: _log_response(r, out))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(APP_URL)
        try:
            ctx.wait_for_event("close", timeout=0)  # until the user closes the window
        except Exception:
            pass
        if out:
            out.close()
    print(
        f"Session saved in {PROFILE_DIR}/"
        + (f"; API calls logged to {CAPTURE_FILE}" if capture else "")
    )


_GQL = "https://api.exp.welcometothejungle.com/graphql"
_MAX_JOBS = 100
_RECS_QUERY = """query R($i: Boolean) { currentUser { id
  jobRecommendations(ignoreSubfunctions: $i) { job { externalId } } } }"""
_JOB_QUERY = """query D($id: ID!) { publicJob(externalId: $id) {
  title originalUrl remoteOnly live insertedAt officeLocation locationRestrictions
  formattedLocationPreferences { name }
  salaryRange { minAmount maxAmount currency }
  involvesBullets { value } requirements { value desirable } company { name } } }"""


def session_available() -> bool:
    """True when a login profile exists and Playwright is importable (else the source is off)."""
    if not PROFILE_DIR.is_dir():
        return False
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


def fetch_jobs() -> list[dict]:
    """The signed-in user's WTTJ recommendation queue as raw `publicJob` dicts.

    Read-only: it never sends the swipe/react mutations, so the user's WTTJ queue is untouched.
    `ignoreSubfunctions` widens the queue from the strict matches (a handful) to ~90 roles.
    Raises RuntimeError when the session has expired.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=True)
        try:
            csrf = next((c["value"] for c in ctx.cookies() if c["name"] == "_csrf_token"), "")
            headers = {
                "content-type": "application/json",
                "x-csrf-token": csrf,
                "origin": "https://app.welcometothejungle.com",
                "referer": "https://app.welcometothejungle.com/",
            }

            def gql(query, variables):
                resp = ctx.request.post(
                    _GQL, data=json.dumps({"query": query, "variables": variables}), headers=headers
                )
                return (resp.json().get("data") or {}) if resp.ok else {}

            user = gql(_RECS_QUERY, {"i": True}).get("currentUser")
            if not user:
                raise RuntimeError("WTTJ session expired — run: uv run python cli.py --login-wttj")
            ids = [r["job"]["externalId"] for r in user["jobRecommendations"]][:_MAX_JOBS]
            jobs = []
            for ext_id in ids:
                job = gql(_JOB_QUERY, {"id": ext_id}).get("publicJob")
                if job and job.get("live"):
                    job["externalId"] = ext_id
                    jobs.append(job)
            return jobs
        finally:
            ctx.close()


def search_wttj(query: str, location: str = "") -> list:
    """Welcome to the Jungle recommendations — optional, needs `cli.py --login-wttj` once.

    A personalized queue (~90 roles), not a searchable board; silently skipped when there is
    no saved login or Playwright. See wttj.py.
    """
    if not session_available():
        return []
    try:
        terms = _location_terms(location)
        jobs = []
        for j in fetch_jobs():
            title = (j.get("title") or "").strip()
            if not title or not _title_matches(title, query):
                continue
            places = [p["name"] for p in j.get("formattedLocationPreferences") or []]
            if j.get("officeLocation"):
                places.insert(0, str(j["officeLocation"]))
            job_location = "; ".join(dict.fromkeys(places))
            if j.get("remoteOnly") and "remote" not in job_location.lower():
                job_location = ("Remote — " + job_location) if job_location else "Remote"
            if not _matches_location(job_location, terms):
                continue
            sal = j.get("salaryRange") or {}
            salary = ""
            if sal.get("minAmount") or sal.get("maxAmount"):
                lo, hi = (
                    int(float(sal[k])) if sal.get(k) else None for k in ("minAmount", "maxAmount")
                )
                span = f"{lo:,}–{hi:,}" if lo and hi else f"{(lo or hi):,}"
                salary = f"{span} {sal.get('currency') or ''}".strip()
            bullets = [b["value"] for b in j.get("involvesBullets") or []]
            jobs.append(
                _make_job(
                    title=title,
                    company=(j.get("company") or {}).get("name"),
                    url=j.get("originalUrl")
                    or f"https://app.welcometothejungle.com/jobs/{j['externalId']}",
                    location=job_location or "See posting",
                    salary=salary,
                    posted_date="",  # insertedAt is first-seen (often years old), not freshness
                    wfh_policy="Remote" if j.get("remoteOnly") else "Unknown",
                    snippet=" ".join(bullets),
                    source="wttj",
                )
            )
        print(f"   WTTJ: {len(jobs)} matching jobs")
        return jobs
    except Exception as e:
        print(f"   WTTJ error: {e}")
        return []


if __name__ == "__main__":
    login()
