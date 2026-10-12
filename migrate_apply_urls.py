"""One-off cleanup: rewrite jobs.csv URLs that point at an ATS apply-form page
(.../apply, .../applicants/new, .../application) back to the job posting page.

Climatebase's `how_to_apply` field stores whatever the "Apply" button links to,
which is often the application form rather than the posting — see
agent.py's `_strip_apply_suffix`. Rows already in jobs.csv from before that fix
existed still carry the form URL. Idempotent — re-running is a no-op.

`push_jobs` is a per-key upsert (agent.py's `job_key` = company|title), so
pushing the corrected rows overwrites the existing Firestore docs in place —
that's what fixes the URL for the Android app / other synced devices, since a
plain `sync_up()` only pushes keys the remote DB doesn't already have.

    uv run python migrate_apply_urls.py                  # dry run: report only
    uv run python migrate_apply_urls.py --apply           # rewrite jobs.csv
    uv run python migrate_apply_urls.py --apply --push    # + push fixed rows to Firestore
"""

import csv
import sys

from agent import _strip_apply_suffix

JOBS_CSV = "jobs.csv"


def main() -> None:
    apply = "--apply" in sys.argv
    push = "--push" in sys.argv
    with open(JOBS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    changed_rows = []
    for row in rows:
        old = row.get("url", "")
        new = _strip_apply_suffix(old)
        if new != old:
            changed_rows.append(row)
            print(f"  {old}\n  -> {new}\n")
            row["url"] = new

    print(f"{len(changed_rows)} of {len(rows)} rows changed.")

    if not apply:
        print("\nDry run. Re-run with --apply to rewrite jobs.csv.")
        return

    with open(JOBS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    print("Rewrote jobs.csv.")

    if not push or not changed_rows:
        return

    from sync import get_sync

    s = get_sync()
    if not s:
        print("Sync not configured — local file only.")
        return
    s.push_jobs(changed_rows)
    print(f"Pushed {len(changed_rows)} corrected job records to Firestore.")


if __name__ == "__main__":
    main()
