"""One-off migration: re-key job_decisions.json from the old URL-first identity
to the new company|title-first identity (sync.norm_key). Run once after the
identity change. Idempotent — re-running is a no-op.

    uv run python -m jobfinder.migrations.migrate_decision_keys          # dry run: report only
    uv run python -m jobfinder.migrations.migrate_decision_keys --apply   # rewrite file + push to Firestore

Existing Firestore decision docs keyed by the old id are left in place; they
become harmless orphans (no job's new identity matches them). Only local
job_decisions.json is rewritten and the new-keyed decisions are pushed up.
"""

import json
import os
import sys

from jobfinder.config.paths import PROJECT_ROOT
from jobfinder.sync.firestore import norm_key

DECISIONS = os.path.join(PROJECT_ROOT, "job_decisions.json")


def remap(old: dict) -> tuple[dict, int, int]:
    new: dict = {}
    rekeyed = merged = 0
    for k, rec in old.items():
        nk = norm_key(k, rec.get("company", ""), rec.get("title", "")) or k
        if nk != k:
            rekeyed += 1
        if nk in new:
            merged += 1
            # keep the later-ts record on collision (two URL-variants of one role)
            if new[nk].get("ts", "") >= rec.get("ts", ""):
                continue
        new[nk] = rec
    return new, rekeyed, merged


def main() -> None:
    apply = "--apply" in sys.argv
    with open(DECISIONS, encoding="utf-8") as f:
        old = json.load(f)
    new, rekeyed, merged = remap(old)
    print(f"{len(old)} decisions -> {len(new)}  ({rekeyed} re-keyed, {merged} merged as dupes)")

    if not apply:
        # Show the collisions so a real merge can be eyeballed before committing.
        seen: dict = {}
        for k, rec in old.items():
            nk = norm_key(k, rec.get("company", ""), rec.get("title", "")) or k
            seen.setdefault(nk, []).append((k, rec.get("decision"), rec.get("ts")))
        for nk, rows in seen.items():
            if len(rows) > 1:
                print(f"  MERGE {nk!r}:")
                for k, dec, ts in rows:
                    print(f"    - {dec:8} {ts}  {k}")
        print("\nDry run. Re-run with --apply to rewrite the file and push to Firestore.")
        return

    with open(DECISIONS, "w", encoding="utf-8") as f:
        json.dump(new, f, indent=2, ensure_ascii=False)
    print("Rewrote job_decisions.json.")

    from jobfinder.sync.firestore import get_sync, sync_up

    s = get_sync()
    if not s:
        print("Sync not configured — local file only.")
        return

    sync_up(verbose=True)
    print("Pushed migrated decisions to Firestore.")

    # Delete non-canonical remote decision docs left behind by the old identity:
    # URL-keyed rows that now re-key to company|title, and raw-cased company|title
    # keys the old Android build wrote without lowercasing. Only delete when the
    # canonical key already exists remotely (a counterpart holds the same decision),
    # so nothing is ever lost. Unknown-company rows re-key to themselves and stay.
    # Idempotent: a second run finds none.
    remote = s.pull_decisions()
    canon = {norm_key(k, r.get("company", ""), r.get("title", "")) or k for k, r in remote.items()}
    stale = []
    for k, rec in remote.items():
        nk = norm_key(k, rec.get("company", ""), rec.get("title", "")) or k
        if nk != k and nk in canon:
            stale.append(k)
    for k in stale:
        s.push_decision(k, None)
    print(f"Deleted {len(stale)} non-canonical decision docs from Firestore (counterparts kept).")


if __name__ == "__main__":
    main()
