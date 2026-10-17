"""Unified search: all sources in parallel, dedup, validate, rank."""

import inspect

from jobfinder.config.profile import HOME_COUNTRIES, NONCLIMATE_MIN_FIT
from jobfinder.pipeline.validate import validate_jobs_batch
from jobfinder.pipeline.verify import verify_jobs_online
from jobfinder.sources.adzuna import search_adzuna
from jobfinder.sources.arbeitnow import search_arbeitnow
from jobfinder.sources.ats import search_greenhouse, search_lever
from jobfinder.sources.base import (
    _REGION_PATTERNS,
    LOCATION_DEBUG_CSV,
    PROFILE_QUERY,
    _home_region_label,
    _is_preferred_location,
    _job_age_days,
    _location_terms,
    _preferred_location_verdict,
    _region_terms,
    _term_pattern,
    _write_location_debug,
)
from jobfinder.sources.cdrjobs import search_cdrjobs
from jobfinder.sources.climatebase import search_climatebase
from jobfinder.sources.climatejobs import search_climatejobs
from jobfinder.sources.climatetechlist import search_climatetechlist
from jobfinder.sources.jobsincarbon import search_jobsincarbon
from jobfinder.sources.remoteimpact import search_remoteimpact
from jobfinder.sources.remoteok import search_remoteok
from jobfinder.sources.wttj import search_wttj


def _dedup_and_filter_stale(jobs: list, seen_keys: set, max_age_days: int) -> list:
    """Drop stale postings first, then dedup by cross-device identity.

    Order matters: the old code registered the dedup key *before* the age check,
    so an ancient variant of a job poisoned the key and a fresh repost from a
    better source vanished entirely. Same-key variants now resolve to the freshest
    one (dated beats undated; among dated, newest posted_date wins).
    """

    def _age(job):
        return _job_age_days(job.get("posted_date", ""))

    from jobfinder.sync.firestore import norm_key

    fresh = [j for j in jobs if (a := _age(j)) is None or a <= max_age_days]
    by_key: dict[str, dict] = {}
    kept = []
    for job in fresh:
        key = norm_key(job.get("url", ""), job.get("company", ""), job.get("title", ""))
        if not key:  # nothing to identify the job by — never dedup pasted/unknown jobs
            kept.append(job)
            continue
        if key in seen_keys:
            continue
        if (prev := by_key.get(key)) is not None:
            pa, a = _age(prev), _age(job)
            if pa is None or (a is not None and a < pa):
                by_key[key] = job
        else:
            by_key[key] = job
    kept.extend(by_key.values())
    seen_keys.update(by_key)
    return kept


def _search_dedupe(all_jobs: list, seen_keys: set, max_age_days: int) -> list:
    return _dedup_and_filter_stale(all_jobs, seen_keys, max_age_days)


ALL_SOURCES = [
    search_arbeitnow,
    search_remoteok,
    search_adzuna,
    search_greenhouse,
    search_lever,
    search_climatetechlist,
    search_climatebase,
    search_jobsincarbon,
    search_remoteimpact,
    search_climatejobs,
    search_cdrjobs,
    search_wttj,
]


def _drop_region_locked(jobs: list, location: str) -> tuple[list, list]:
    """Split off jobs Jev says are region-locked away from the searched location.

    A job whose location field already names the searched place is never dropped
    (an on-site Zürich role that mentions US clients is not region-locked).
    """
    import jobfinder.pipeline.jev as jev

    terms = set(_location_terms(location))
    if _is_preferred_location(location):
        terms.update(_region_terms())
    terms = sorted(terms)
    local = _term_pattern(terms) if terms else None
    kept, dropped = [], []
    for job in jobs:
        lock = (job.get("_jev") or {}).get("region_locked", 0)
        loc = (job.get("location") or "").lower()
        is_local = bool(local and local.search(loc)) or any(
            _REGION_PATTERNS[h["country"]].search(loc)
            for h in HOME_COUNTRIES
            if h["country"] in _REGION_PATTERNS
        )
        if lock >= jev.REGION_LOCK_THRESHOLD and not is_local:
            dropped.append({**job, "drop_reason": f"Jev: region-locked ({lock:.2f})"})
        else:
            kept.append(job)
    return kept, dropped


def _apply_jev_scores(job: dict) -> None:
    """Let Jev's calibrated fit/startup/climate scores override the LLM's (when Jev judged the job).

    Large disagreements are noted in `reason` so a surprising score is explainable.
    """
    verdict = job.get("_jev")
    if not verdict:
        return
    notes = []
    for key in ("fit", "startup", "climate"):
        field = f"{key}_score"
        llm_score, jev_score = job.get(field), int(round(verdict[key]))
        job[field] = jev_score
        if isinstance(llm_score, int) and abs(jev_score - llm_score) >= 2:
            notes.append(f"{key} {jev_score} per Jev, LLM {llm_score}")
    if notes:
        job["reason"] = f"{job.get('reason', '')} [{'; '.join(notes)}]".strip()


def fetch_descriptions(jobs: list, _progress=None) -> None:
    """Fetch the full posting text for each job in parallel, attached as `_desc`.

    Runs during search so LLM validation scores the real description (not just
    the feed snippet) and the TUI cards can preview it instantly. Fetched text
    is disk-cached (job_descriptions.json) via cover_letter.fetch_job_description;
    `_desc` itself is stripped before CSV writes. Jobs whose fetch fails keep
    only their snippet.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from jobfinder.documents.cover_letter import fetch_job_description

    to_fetch = [j for j in jobs if (j.get("url") or "").startswith("http") and "_desc" not in j]
    if not to_fetch:
        return

    def fetch(job):
        text = fetch_job_description(job["url"])
        if text:
            job["_desc"] = text

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(fetch, j): j for j in to_fetch}
        for done, future in enumerate(as_completed(futures), 1):
            if _progress:
                _progress("descriptions", done, len(to_fetch), futures[future].get("company", ""))


def search_jobs(
    query: str = "",
    location: str = "",
    max_results: int = 50,
    min_fit: int = 3,
    min_climate: int = 2,
    max_age_days: int = 90,
    non_climate: bool = False,
    _progress=None,
    _sources: list | None = None,
) -> dict:
    """Search all job sources in parallel and validate results with an LLM.

    Args:
        query: Job title query. Empty or "profile" searches the candidate's
            full target-role profile (see candidate_profile.py).
        location: Country/city filter; remote jobs always pass.
        max_results: Cap on returned jobs.
        min_fit: Minimum role-fit score (0-5) to keep a job.
        min_climate: Minimum climate score (0-5) to keep a job.
        max_age_days: Drop jobs posted longer ago than this (when date is known).
        non_climate: Invert the climate gate — keep only jobs scoring BELOW
            min_climate but fitting the role at least NONCLIMATE_MIN_FIT. The
            best general (non-climate) roles for the same profile.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    query = (query or PROFILE_QUERY).strip()
    for prefix in ("find ", "search for ", "looking for ", "jobs in ", "jobs for "):
        query = query.lower().replace(prefix, "")
    query = query.replace("in the climate and green tech sector", "").strip() or PROFILE_QUERY

    sources = _sources or ALL_SOURCES
    label = "target-role profile" if query == PROFILE_QUERY else f"'{query}'"
    print(
        f"🔍 Searching {len(sources)} sources for {label}" + (f" in {location}" if location else "")
    )

    def call_source(fn):
        # Sources that report fine-grained progress (connections) get the callback.
        if "_progress" in inspect.signature(fn).parameters:
            return fn(query, location, _progress=_progress)
        return fn(query, location)

    all_jobs = []
    with ThreadPoolExecutor(max_workers=len(sources)) as ex:
        futures = {ex.submit(call_source, fn): fn.__name__ for fn in sources}
        for done, future in enumerate(as_completed(futures), 1):
            name = futures[future].removeprefix("search_")
            try:
                jobs = future.result()
                all_jobs.extend(jobs)
                if _progress:
                    _progress("sources", done, len(sources), f"{name}:{len(jobs)}")
            except Exception as e:
                print(f"   {futures[future]} error: {e}")
                if _progress:
                    _progress("sources", done, len(sources), f"{name}:error")

    # Dedupe by cross-device identity (company|title, URL fallback) — same key
    # save_jobs and the sync layer use, so one search never yields two rows that
    # would later collapse to one job. Stale postings are dropped *before* keys
    # are registered (a poisoned key used to swallow fresh reposts), and same-key
    # variants resolve to the freshest one.

    seen_keys: set = set()
    unique_jobs = _dedup_and_filter_stale(all_jobs, seen_keys, max_age_days)

    dropped_by_policy = []
    if _is_preferred_location(location):
        kept = []
        for job in unique_jobs:
            ok, reason = _preferred_location_verdict(job)
            if ok:
                kept.append(job)
            else:
                dropped_by_policy.append({**job, "drop_reason": reason})
        unique_jobs = kept
        print(
            f"   Location policy: {len(kept)}/{len(kept) + len(dropped_by_policy)} jobs kept"
            f" (dropped → {LOCATION_DEBUG_CSV})"
        )
        _write_location_debug(dropped_by_policy)

    # Dead postings out first — no point fetching or scoring roles already filled.
    # Undated jobs (wttj, remoteimpact, scrape sources) get checked too: without a
    # date they used to bypass the liveness pass entirely.
    unique_jobs, dropped = verify_jobs_online(
        unique_jobs, verify_unknown_dates=True, _progress=_progress
    )

    # Full descriptions next: validation scores the real posting text, and the
    # TUI cards get their previews for free.
    fetch_descriptions(unique_jobs, _progress=_progress)

    def is_general(job):
        return (
            job["fit_score"] >= max(min_fit, NONCLIMATE_MIN_FIT)
            and job["climate_score"] < min_climate
        )

    def keeps(job):
        if non_climate:
            return is_general(job)
        return job["fit_score"] >= min_fit and job["climate_score"] >= min_climate

    # Jev: typed fit/startup/climate scores + region-lock check on the full description.
    import jobfinder.pipeline.jev as jev

    if jev.enabled():
        region = (
            _home_region_label() if _is_preferred_location(location) else (location or "Europe")
        )
        got = jev.annotate(unique_jobs, region, _progress=_progress)
        unique_jobs, jev_dropped = _drop_region_locked(unique_jobs, location)
        print(f"   Jev: {got} judged, {len(jev_dropped)} dropped as region-locked")
        if jev_dropped:
            _write_location_debug(dropped_by_policy + jev_dropped)
        # Jev already scored these: skip the LLM for the ones that can't pass, so it
        # only runs (for the one-line reason) on survivors and on unjudged jobs.
        before = len(unique_jobs)
        unique_jobs = [
            j
            for j in unique_jobs
            if not j.get("_jev") or (_apply_jev_scores(j), keeps(j) or is_general(j))[1]
        ]
        print(f"   Jev thresholds: {before - len(unique_jobs)} dropped")

    print(f"   {len(unique_jobs)} unique recent jobs; validating with LLM...")

    batch_size = 15
    batches = [unique_jobs[i : i + batch_size] for i in range(0, len(unique_jobs), batch_size)]
    validated = []
    general = []  # fit the role but fail the climate gate: kept for the "general roles" view
    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, (batch, validations) in enumerate(
            zip(batches, ex.map(lambda b: validate_jobs_batch(b, query), batches)), 1
        ):
            for job, val in zip(batch, validations):
                job.update(val)
                _apply_jev_scores(job)
                if keeps(job):
                    validated.append(job)
                elif is_general(job):
                    general.append(job)
            if _progress:
                _progress("validate", i, len(batches), f"{len(validated)} kept")

    from jobfinder.config.profile import is_pearl

    def rank(j):
        return (
            not is_pearl(j),  # pearls (supermatches) first
            -j["fit_score"],
            -j["climate_score"],
            -j.get("startup_score", 0),
            j.get("posted_date", ""),
        )

    validated.sort(key=rank)
    general.sort(key=rank)
    final = validated[:max_results]
    if non_climate:
        print(
            f"   Final: {len(final)} jobs (general roles: fit ≥ "
            f"{max(min_fit, NONCLIMATE_MIN_FIT)}, climate < {min_climate})"
        )
    else:
        print(f"   Final: {len(final)} jobs (fit ≥ {min_fit}, climate ≥ {min_climate})")
    if _progress:
        _progress("done", 1, 1, f"{len(final)} jobs")

    return {
        "status": "success",
        "query": query,
        "location": location,
        "total_found": len(final),
        "offline_dropped": dropped,
        "jobs": final,
        "general_jobs": [] if non_climate else general[:max_results],
    }
