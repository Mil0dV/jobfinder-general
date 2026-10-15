"""Cross-device sync of the jobs DB via Cloud Firestore (free tier).

Mirrors the three local stores to three Firestore collections, so the phone app
and this CLI/TUI share one set of jobs and swipe decisions:

    jobs/<key>          <- jobs.csv rows            (content-addressed, merge)
    decisions/<key>     <- job_decisions.json       (last swipe wins, by ts)
    descriptions/<key>  <- job_descriptions.json    (pushed on fetch, pulled on miss)
    replies/<sha1 msgid> -> job_replies.json        (pull only; the Android app writes)

The replies collection is the Android app's inbox scan: it reads Gmail over IMAP,
classifies what companies wrote back, and publishes the verdicts. This side only
reads them, to show the interview banner in the TUI and shell — no mail
credentials live here.

<key> is sha1 of the job's normalized URL (or "company|title" when there is no
URL); every record carries its original key in a "key"/"url" field for the
reverse mapping. The Android app (data/Sync.kt) uses the Firebase SDK against
the same collections with the same keys.

Auth is Firebase email/password over REST — no SDK. Configure in .env:
    FIREBASE_API_KEY, FIREBASE_PROJECT_ID, FIREBASE_EMAIL, FIREBASE_PASSWORD
Sync is entirely optional: with no config every hook is a silent no-op.

Run `uv run python sync.py` for a full two-way sync (and to print your auth
UID for the Firestore security rules). Set FIRESTORE_EMULATOR_HOST (and
FIREBASE_API_KEY=emulator to skip sign-in) to run against a local emulator.
"""

import hashlib
import json
import os
import threading
import time

from dotenv import load_dotenv

load_dotenv()

_project_root = os.path.dirname(os.path.abspath(__file__))
DECISIONS_FILE = os.path.join(_project_root, "job_decisions.json")
REPLIES_FILE = os.path.join(_project_root, "job_replies.json")

_TIMEOUT = 15
_BATCH = 400  # Firestore commit limit is 500 writes


def norm_key(url: str, company: str = "", title: str = "") -> str:
    """The job's cross-device identity — the id behind every Firestore doc and
    every swipe decision. Company|title-first (lowercased) so the SAME role found
    by two sources under two URLs is ONE job on every device: identity, dedup key
    and decision key are all this string, which is what keeps decisions convergent
    across devices. Falls back to the normalized URL only when there is no real
    company+title (company missing or "Unknown")."""
    c = (company or "").strip().lower()
    t = (title or "").strip().lower()
    if c and c != "unknown" and t:
        return f"{c}|{t}"
    u = (url or "").split("?")[0].strip().rstrip("/")
    return u or (f"{c}|{t}" if c and t else "")


def job_key(job: dict) -> str:
    """Deterministic Firestore document id for a job/decision/description."""
    ident = norm_key(job.get("url", ""), job.get("company", ""), job.get("title", ""))
    return hashlib.sha1(ident.encode("utf-8")).hexdigest()


def tombstone(ts: str = "") -> dict:
    """Delete marker carried in the decisions store (instead of dropping the entry).

    Deletes used to be raw Firestore deletes: sync_down never saw them, so an
    undone swipe on one device resurrected on the next pull. A tombstone is a
    decision record every device can see and honour; a later real decision
    simply overwrites it.
    """
    return {
        "decision": "deleted",
        "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def is_tombstone(record) -> bool:
    return isinstance(record, dict) and record.get("decision") == "deleted"


def _filter_tombstones(decisions: dict) -> dict:
    """Decisions visible to the UI/CLI: tombstones are sync plumbing, not swipes."""
    return {k: v for k, v in decisions.items() if not is_tombstone(v)}


def _ts_rank(ts) -> str:
    """Normalize a decision timestamp for cross-device comparison.

    Old records carry minute-granularity *local* strings ("2026-10-12 18:40")
    written with each device's own clock — comparing them lexicographically
    across timezones inverts the ordering. New records are ISO-8601 UTC
    ("2026-10-12T18:40:00Z"). Mapping the legacy format to the same UTC shape
    (assuming it was written in the laptop's timezone, TIMEZONE) makes old and
    new records sort consistently.
    """
    s = str(ts or "").strip()
    if not s:
        return ""
    from datetime import datetime

    try:
        from zoneinfo import ZoneInfo

        from candidate_profile import TIMEZONE
    except Exception:
        ZoneInfo, TIMEZONE = None, None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%dT%H:%M:%SZ":
            return s  # already canonical UTC
        if ZoneInfo is not None and TIMEZONE is not None:
            try:
                dt = dt.replace(tzinfo=ZoneInfo(TIMEZONE)).astimezone(
                    datetime.now(ZoneInfo("UTC")).tzinfo
                )
            except Exception:
                pass
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return s  # unrecognized format: compare as-is


def _enc_fields(record: dict) -> dict:
    """Encode a flat dict into Firestore REST typed fields."""
    fields = {}
    for k, v in record.items():
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            fields[k] = {"booleanValue": v}
        elif isinstance(v, int):
            fields[k] = {"integerValue": str(v)}
        else:
            fields[k] = {"stringValue": str(v)}
    return fields


def _dec_fields(fields: dict) -> dict:
    """Decode Firestore REST typed fields into a flat dict."""
    out = {}
    for k, v in (fields or {}).items():
        if "stringValue" in v:
            out[k] = v["stringValue"]
        elif "integerValue" in v:
            out[k] = int(v["integerValue"])
        elif "booleanValue" in v:
            out[k] = v["booleanValue"]
        elif "doubleValue" in v:
            out[k] = v["doubleValue"]
    return out


class FirebaseSync:
    """Minimal Firebase Auth + Firestore REST client."""

    def __init__(self, api_key: str, project_id: str, email: str, password: str):
        self.api_key = api_key
        self.project_id = project_id
        self.email = email
        self.password = password
        emu = os.getenv("FIRESTORE_EMULATOR_HOST")
        host = f"http://{emu}" if emu else "https://firestore.googleapis.com"
        self._doc_root = f"projects/{project_id}/databases/(default)/documents"
        self._base = f"{host}/v1/{self._doc_root}"
        self._id_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        self._lock = threading.Lock()
        self.uid = None

    # --- auth ---------------------------------------------------------------

    def _sign_in(self):
        import requests

        resp = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={self.api_key}",
            json={"email": self.email, "password": self.password, "returnSecureToken": True},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            msg = resp.json().get("error", {}).get("message", resp.text[:200])
            raise RuntimeError(f"Firebase sign-in failed: {msg}")
        data = resp.json()
        self._id_token = data["idToken"]
        self._refresh_token = data.get("refreshToken")
        self._expires_at = time.time() + int(data.get("expiresIn", 3600)) - 60
        self.uid = data.get("localId")

    def _token(self) -> str:
        if self.api_key == "emulator":  # Firestore emulator: no auth
            self.uid = "emulator"
            return "owner"
        with self._lock:
            if self._id_token is None or time.time() >= self._expires_at:
                self._sign_in()
            return self._id_token

    # --- REST primitives ------------------------------------------------------

    def _request(self, method: str, path: str, payload=None, params=None, absolute=False):
        import requests

        url = path if absolute else f"{self._base}/{path}"
        resp = requests.request(
            method,
            url,
            params=params,
            json=payload,
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=max(_TIMEOUT, 30),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def _commit(self, writes: list):
        for i in range(0, len(writes), _BATCH):
            self._request(
                "POST",
                f"{self._base}:commit",
                payload={"writes": writes[i : i + _BATCH]},
                absolute=True,
            )

    def _doc_name(self, collection: str, doc_id: str) -> str:
        return f"{self._doc_root}/{collection}/{doc_id}"

    def _list_all(self, collection: str, key_only: bool = False) -> list:
        """All documents in a collection as (doc_id, record) pairs."""
        out, token = [], None
        while True:
            params = {"pageSize": 300}
            if key_only:
                params["mask.fieldPaths"] = "key"
            if token:
                params["pageToken"] = token
            data = self._request("GET", collection, params=params) or {}
            for doc in data.get("documents", []):
                doc_id = doc["name"].rsplit("/", 1)[-1]
                out.append((doc_id, _dec_fields(doc.get("fields", {}))))
            token = data.get("nextPageToken")
            if not token:
                return out

    # --- domain operations ------------------------------------------------------

    @staticmethod
    def _job_record(job: dict) -> dict:
        record = {
            key: val
            for key, val in job.items()
            if not key.startswith("_") and key != "snippet" and val not in (None, "")
        }
        for score in ("fit_score", "startup_score", "climate_score"):
            try:
                record[score] = int(record[score])
            except (KeyError, TypeError, ValueError):
                record.pop(score, None)
        record["key"] = norm_key(job.get("url", ""), job.get("company", ""), job.get("title", ""))
        return record

    def push_jobs(self, jobs: list):
        """Upsert job records (and their fetched descriptions) in batches."""
        writes = []
        for job in jobs:
            k = job_key(job)
            writes.append(
                {
                    "update": {
                        "name": self._doc_name("jobs", k),
                        "fields": _enc_fields(self._job_record(job)),
                    }
                }
            )
            desc = job.get("_desc")
            if desc and job.get("url"):
                # Descriptions are keyed by normalized URL (page content is
                # per-URL) — the same key getDescription/push_description use and
                # the Android app writes with (Sync.kt sha1(normalizeUrl(url))).
                # The old identity key (sha1(company|title)) made descriptions
                # pushed with a job unreachable by every reader, and orphaned a
                # dupe whenever the same role appeared under two URLs.
                writes.append(
                    {
                        "update": {
                            "name": self._doc_name(
                                "descriptions",
                                hashlib.sha1(norm_key(job["url"]).encode("utf-8")).hexdigest(),
                            ),
                            "fields": _enc_fields({"url": job["url"], "description": desc}),
                        }
                    }
                )
        self._commit(writes)

    def pull_jobs(self) -> list:
        return [record for _, record in self._list_all("jobs")]

    def remote_job_keys(self) -> set:
        return {doc_id for doc_id, _ in self._list_all("jobs", key_only=True)}

    def push_decision(self, local_key: str, record: dict | None):
        """Write one swipe decision; on undo write a tombstone instead of deleting.

        A raw DELETE was invisible to sync_down, so undone swipes resurrected on
        the next pull from any device that still had the old record.
        """
        k = hashlib.sha1(local_key.encode("utf-8")).hexdigest()
        if record is None:
            record = tombstone()
        self._commit(
            [
                {
                    "update": {
                        "name": self._doc_name("decisions", k),
                        "fields": _enc_fields({**record, "key": local_key}),
                    }
                }
            ]
        )

    def push_decisions(self, decisions: dict):
        writes = [
            {
                "update": {
                    "name": self._doc_name(
                        "decisions", hashlib.sha1(local_key.encode("utf-8")).hexdigest()
                    ),
                    "fields": _enc_fields({**record, "key": local_key}),
                }
            }
            for local_key, record in decisions.items()
        ]
        self._commit(writes)

    def pull_decisions(self) -> dict:
        """Remote decisions as {local_key: record}."""
        out = {}
        for _, record in self._list_all("decisions"):
            local_key = record.pop("key", "")
            if local_key:
                out[local_key] = record
        return out

    def pull_replies(self) -> dict:
        """Classified inbox replies as {doc_id: record}. Read-only on this side —
        the Android app's inbox scan is the only writer."""
        out = {}
        for doc_id, record in self._list_all("replies"):
            record.pop("id", None)
            out[doc_id] = record
        return out

    def get_description(self, url: str) -> str | None:
        k = hashlib.sha1(norm_key(url).encode("utf-8")).hexdigest()
        try:
            doc = self._request("GET", f"descriptions/{k}")
        except Exception:
            return None
        if doc:
            return _dec_fields(doc.get("fields", {})).get("description") or None
        return None

    def push_description(self, url: str, text: str):
        k = hashlib.sha1(norm_key(url).encode("utf-8")).hexdigest()
        self._commit(
            [
                {
                    "update": {
                        "name": self._doc_name("descriptions", k),
                        "fields": _enc_fields({"url": url, "description": text}),
                    }
                }
            ]
        )

    def remote_description_keys(self) -> set:
        return {doc_id for doc_id, _ in self._list_all("descriptions", key_only=True)}


# --- module-level singleton ----------------------------------------------------

_sync = None
_sync_failed = False


def get_sync() -> FirebaseSync | None:
    """The shared client, or None when unconfigured (or auth already failed once)."""
    global _sync, _sync_failed
    if _sync_failed:
        return None
    if _sync is None:
        api_key = os.getenv("FIREBASE_API_KEY")
        project_id = os.getenv("FIREBASE_PROJECT_ID")
        email = os.getenv("FIREBASE_EMAIL")
        password = os.getenv("FIREBASE_PASSWORD")
        if not all((api_key, project_id, email, password)):
            _sync_failed = True
            return None
        _sync = FirebaseSync(api_key, project_id, email, password)
        try:
            _sync._token()
        except Exception as e:
            print(f"   Sync disabled: {e}")
            _sync, _sync_failed = None, True
            return None
    return _sync


def _in_thread(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def push_jobs_async(jobs: list):
    """Best-effort background push of new jobs (called from save_jobs)."""
    sync = get_sync()
    if sync and jobs:
        _in_thread(lambda: _swallow(sync.push_jobs, jobs))


def push_decision_async(local_key: str, record: dict | None):
    """Best-effort background push of one swipe decision (record=None deletes)."""
    sync = get_sync()
    if sync:
        _in_thread(lambda: _swallow(sync.push_decision, local_key, record))


def push_description_async(url: str, text: str):
    sync = get_sync()
    if sync and url and text:
        _in_thread(lambda: _swallow(sync.push_description, url, text))


def get_remote_description(url: str) -> str | None:
    sync = get_sync()
    return sync.get_description(url) if sync else None


def _swallow(fn, *args):
    try:
        fn(*args)
    except Exception:
        pass


# --- high-level sync ------------------------------------------------------------


def _load_local_decisions() -> dict:
    try:
        with open(DECISIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_local_decisions(decisions: dict):
    with open(DECISIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(decisions, f, indent=2, ensure_ascii=False)


def load_replies() -> dict:
    """Locally cached inbox replies as {doc_id: record}, newest first.

    Populated by sync_down from the Android app's scan. Empty when sync is off or
    the phone hasn't scanned yet — every caller treats that as "nothing to show".
    """
    try:
        with open(REPLIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def replies_sorted(status: str = "") -> list:
    """Reply records, newest first, optionally filtered to one status."""
    records = [r for r in load_replies().values() if not status or r.get("status") == status]
    return sorted(records, key=lambda r: str(r.get("ts", "")), reverse=True)


def invites_waiting() -> int:
    """How many replies invited the candidate to talk."""
    return len(replies_sorted("invited"))


def actions_waiting() -> int:
    """Applications parked until the candidate does something — screening questions,
    an assessment, a re-uploaded CV. Not an invitation, but ignoring one kills the
    application just as dead, so it carries the same weight in the badge."""
    return len(replies_sorted("action_needed"))


def waiting_count() -> int:
    """Everything that needs the candidate — the badge count."""
    return invites_waiting() + actions_waiting()


def sync_down(verbose: bool = False) -> bool:
    """Merge remote decisions and jobs into the local files. Returns success."""
    sync = get_sync()
    if not sync:
        return False
    try:
        # Decisions: union, later "ts" wins on conflict (compared as UTC — see
        # _ts_rank). Deletes travel as tombstones: a tombstone newer than a
        # record kills it, and on an exact tie the delete wins so removals
        # converge instead of resurrecting. Canonicalize the incoming key
        # (company|title, URL fallback) so a raw/legacy remote key never enters
        # local state — otherwise it would just get pushed back up on the next sync.
        local = _load_local_decisions()
        changed = False
        for key, record in sync.pull_decisions().items():
            ck = norm_key(key, record.get("company", ""), record.get("title", "")) or key
            if ck not in local:
                local[ck] = record
                changed = True
                continue
            remote_ts, local_ts = _ts_rank(record.get("ts")), _ts_rank(local[ck].get("ts"))
            if remote_ts > local_ts or (remote_ts == local_ts and is_tombstone(record)):
                local[ck] = record
                changed = True
        if changed:
            _save_local_decisions(local)

        # Jobs: append remote rows jobs.csv doesn't know (save_jobs dedupes by URL).
        from agent import save_jobs

        remote_jobs = sync.pull_jobs()
        result = save_jobs(remote_jobs, _push=False) if remote_jobs else {"added": 0}

        # Replies: a straight mirror of the phone's scan — it owns the collection,
        # so there is nothing to merge and nothing to push back.
        replies = sync.pull_replies()
        if replies:
            with open(REPLIES_FILE, "w", encoding="utf-8") as f:
                json.dump(replies, f, indent=2, ensure_ascii=False)

        if verbose:
            invites = sum(1 for r in replies.values() if r.get("status") == "invited")
            todo = sum(1 for r in replies.values() if r.get("status") == "action_needed")
            print(
                f"   Sync: pulled {len(remote_jobs)} jobs "
                f"({result.get('added', 0)} new), decisions merged, "
                f"{len(replies)} replies ({invites} invite(s), {todo} to finish)"
            )
        return True
    except Exception as e:
        if verbose:
            print(f"   Sync down failed: {e}")
        return False


def sync_up(verbose: bool = False) -> bool:
    """Push local jobs/decisions/descriptions the remote DB doesn't have yet."""
    sync = get_sync()
    if not sync:
        return False
    try:
        from cover_letter import load_jobs_from_csv

        remote_keys = sync.remote_job_keys()
        missing = [j for j in load_jobs_from_csv() if job_key(j) not in remote_keys]
        if missing:
            sync.push_jobs(missing)

        decisions = _load_local_decisions()
        sync.push_decisions(decisions)  # commit is an upsert; cheap

        # Backfill locally cached descriptions the other devices don't have.
        try:
            with open(os.path.join(_project_root, "job_descriptions.json"), encoding="utf-8") as f:
                desc_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            desc_cache = {}
        have = sync.remote_description_keys()
        pushed = 0
        for url, entry in desc_cache.items():
            k = hashlib.sha1(norm_key(url).encode("utf-8")).hexdigest()
            if k not in have and entry.get("description"):
                sync.push_description(url, entry["description"])
                pushed += 1

        if verbose:
            print(
                f"   Sync: pushed {len(missing)} jobs, {len(decisions)} decisions, "
                f"{pushed} descriptions"
            )
        return True
    except Exception as e:
        if verbose:
            print(f"   Sync up failed: {e}")
        return False


def full_sync():
    sync = get_sync()
    if not sync:
        print(
            "Sync not configured. Set FIREBASE_API_KEY, FIREBASE_PROJECT_ID, "
            "FIREBASE_EMAIL, FIREBASE_PASSWORD in .env (see .env.example)."
        )
        return
    print(f"Signed in as {sync.email} (uid: {sync.uid})")
    print("That uid belongs in firestore.rules (deploy: firebase deploy --only firestore).")
    sync_down(verbose=True)
    sync_up(verbose=True)
    print("Done.")


if __name__ == "__main__":
    full_sync()
