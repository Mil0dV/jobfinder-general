"""Direct (non-agent) profile and network searches."""

from jobfinder.pipeline.search import search_jobs
from jobfinder.pipeline.store import save_jobs
from jobfinder.sources.base import PROFILE_QUERY


def run_profile_search(location: str = "", query: str = "", non_climate: bool = False) -> None:
    """Deterministic search + save, no agent loop. Used by `cli.py --profile`."""
    try:
        from jobfinder.sync.firestore import sync_down, sync_up

        sync_down(verbose=True)
        # Flush any decisions/jobs a prior session's async push never sent.
        sync_up(verbose=True)
    except Exception:
        pass
    result = search_jobs(query=query or PROFILE_QUERY, location=location, non_climate=non_climate)
    jobs = result["jobs"]
    saved = save_jobs(jobs + result.get("general_jobs", []))
    print(
        f"\n💾 Saved {saved.get('added', 0)} new jobs "
        f"({saved.get('skipped', 0)} already known) → jobs.csv"
    )
    if jobs:
        from jobfinder.config.profile import is_pearl

        print("\nTop matches (fit/startup/climate):")
        for j in jobs[:15]:
            pearl = "🦪 " if is_pearl(j) else ""
            print(
                f"  {pearl}[{j['fit_score']}/{j.get('startup_score', '?')}/{j['climate_score']}]"
                f" {j['title']} — {j['company']} ({j['location'] or j['wfh_policy']})"
            )
            print(f"      {j['url']}")


def run_connection_search(location: str = "", query: str = "") -> None:
    """Search the job boards of companies where the candidate knows someone.

    Same deterministic shape as run_profile_search, but the only source is
    connections.search_connections (see connections.py).
    """
    from jobfinder.sources.connections import search_connections

    try:
        from jobfinder.sync.firestore import sync_down, sync_up

        sync_down(verbose=True)
        sync_up(verbose=True)
    except Exception:
        pass
    result = search_jobs(
        query=query or PROFILE_QUERY, location=location, _sources=[search_connections]
    )
    jobs = result["jobs"]
    saved = save_jobs(jobs + result.get("general_jobs", []))
    print(
        f"\n💾 Saved {saved.get('added', 0)} new jobs "
        f"({saved.get('skipped', 0)} already known) → jobs.csv"
    )
    if jobs:
        print("\nTop matches (fit/startup/climate):")
        for j in jobs[:15]:
            print(
                f"  🤝 [{j['fit_score']}/{j.get('startup_score', '?')}/{j['climate_score']}]"
                f" {j['title']} — {j['company']} ({j['location'] or j['wfh_policy']})"
            )
            if j.get("contact"):
                extra = f" (+{j['contacts_n'] - 1} more)" if j.get("contacts_n", 1) > 1 else ""
                print(f"      via {j['contact']}{extra}")
            print(f"      {j['url']}")
