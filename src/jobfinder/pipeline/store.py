"""Persistence: jobs.csv with cross-device dedup."""

import csv

CSV_COLUMNS = [
    "title",
    "company",
    "location",
    "salary",
    "posted_date",
    "wfh_policy",
    "fit_score",
    "startup_score",
    "climate_score",
    "reason",
    "contact",
    "source",
    "url",
]


def save_jobs(jobs: list, filepath: str = "jobs.csv", _push: bool = True) -> dict:
    """Save job dicts to CSV, deduplicating against existing rows by the job's
    sync identity: its normalized URL, or company + title only when there is no URL.

    This MUST match norm_key / decisionKey in sync.py & Job.kt — the Firestore
    doc id and the swipe-decision key are both URL-first. Collapsing two rows
    that share company+title but carry different URLs would give them different
    sync/decision keys, so each device would keep a different variant and their
    decisions would never line up (undecided counts diverge across devices).

    New rows are also pushed to the cross-device sync DB (sync.py) in the
    background, unless _push=False (used by sync_down itself to avoid loops).
    """
    try:
        if not jobs:
            return {"status": "success", "added": 0}

        existing = []
        try:
            with open(filepath, "r", newline="", encoding="utf-8") as f:
                existing = list(csv.DictReader(f))
        except FileNotFoundError:
            pass

        from jobfinder.sync.firestore import norm_key

        def ident(j):
            return norm_key(j.get("url", ""), j.get("company", ""), j.get("title", ""))

        existing_keys = {ident(j) for j in existing}
        existing_keys.discard("")
        new_rows, skipped = [], 0
        for job in jobs:
            k = ident(job)
            if k and k in existing_keys:
                skipped += 1
                continue
            if k:
                existing_keys.add(k)
            new_rows.append(job)

        if not new_rows:
            return {"status": "success", "added": 0, "skipped": skipped}

        all_keys = set()
        for j in existing + new_rows:
            all_keys.update(j.keys())
        all_keys.discard("snippet")  # too long for CSV
        all_keys -= {k for k in all_keys if k.startswith("_")}  # _desc, _num, … are runtime-only
        fieldnames = [k for k in CSV_COLUMNS if k in all_keys] + sorted(
            k for k in all_keys if k not in CSV_COLUMNS
        )

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(existing + new_rows)

        if _push:
            try:
                from jobfinder.sync.firestore import push_jobs_async

                push_jobs_async(new_rows)
            except Exception:
                pass

        return {
            "status": "success",
            "added": len(new_rows),
            "skipped": skipped,
            "total_in_file": len(existing) + len(new_rows),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def save_csv(jobs_data: str) -> dict:
    """Append CSV-formatted job rows to jobs.csv, deduping by URL.

    Args:
        jobs_data: CSV-formatted string starting with a header row.
    """
    from io import StringIO

    try:
        return save_jobs(list(csv.DictReader(StringIO(jobs_data))))
    except Exception as e:
        return {"status": "error", "message": str(e)}
