"""Tests for the cross-device identity, decision-merge and description-key layer."""
import hashlib

import pytest

import jobfinder.sync.firestore as sync


# --- identity keys ----------------------------------------------------------------

def test_norm_key_company_title_first():
    assert sync.norm_key("https://x.io/j/1", "Acme", "Product Lead") == "acme|product lead"


def test_norm_key_url_fallback_without_company():
    key = sync.norm_key("https://x.io/j/1?utm=a#frag", "Unknown", "Product Lead")
    assert key == "https://x.io/j/1"


def test_description_key_matches_across_push_and_read():
    """push_jobs must write descriptions under the exact key get_description reads.

    The old identity key (sha1(company|title)) made job-pushed descriptions
    unreachable for every reader and for the Android app (sha1(normalizeUrl(url))).
    """
    job = {
        "url": "https://climatebase.io/jobs/42?utm=1",
        "company": "Acme Carbon",
        "title": "Head of Product",
        "_desc": "a real description",
    }
    s = sync.FirebaseSync.__new__(sync.FirebaseSync)  # no network: only key math here
    s._doc_root = "projects/test/databases/(default)/documents"
    batches = []
    s._commit = batches.append
    s.push_jobs([job])

    writes = [w for batch in batches for w in batch]
    desc_writes = [w for w in writes if "/descriptions/" in w["update"]["name"]]
    assert len(desc_writes) == 1
    expected = hashlib.sha1(sync.norm_key(job["url"]).encode("utf-8")).hexdigest()
    assert desc_writes[0]["update"]["name"].endswith(f"descriptions/{expected}")


# --- tombstones -------------------------------------------------------------------

def test_tombstone_shape():
    t = sync.tombstone()
    assert sync.is_tombstone(t)
    assert t["ts"].endswith("Z") and "T" in t["ts"]
    assert not sync.is_tombstone({"decision": "yes"})


def test_filter_tombstones_hides_plumbing():
    decisions = {"a": {"decision": "yes"}, "b": sync.tombstone()}
    kept = sync._filter_tombstones(decisions)
    assert list(kept) == ["a"]


def test_push_decision_writes_tombstone_instead_of_delete():
    s = sync.FirebaseSync.__new__(sync.FirebaseSync)
    s._doc_root = "projects/test/databases/(default)/documents"
    calls = []
    s._commit = calls.append
    s._request = lambda *a, **k: calls.append(("request", a))
    s.push_decision("acme|head of product", None)
    writes = [w for batch in calls for w in batch]
    assert len(writes) == 1 and "update" in writes[0]
    fields = writes[0]["update"]["fields"]
    assert fields["decision"] == {"stringValue": "deleted"}


# --- timestamp comparison ----------------------------------------------------------

def test_ts_rank_normalizes_legacy_local_strings():
    legacy = sync._ts_rank("2026-10-12 18:40")
    assert legacy == "2026-10-12T16:40:00Z"  # Europe/Zurich = UTC+2 on that date


def test_ts_rank_passes_utc_through():
    assert sync._ts_rank("2026-10-12T16:40:00Z") == "2026-10-12T16:40:00Z"


def test_ts_rank_orders_across_timezones():
    # A Zurich-local 18:40 swipe is EARLIER than a UTC 17:00 swipe — the old
    # lexicographic string compare got this backwards.
    assert sync._ts_rank("2026-10-12 18:40") < sync._ts_rank("2026-10-12T17:00:00Z")


# --- sync_down merge (fake sync client, no network) ---------------------------------

class FakeSync:
    def __init__(self, remote_decisions):
        self.remote = remote_decisions

    def pull_decisions(self):
        return self.remote

    def pull_jobs(self):
        return []

    def pull_replies(self):
        return {}


def _run_merge(monkeypatch, tmp_path, local, remote):
    decisions_file = tmp_path / "job_decisions.json"
    decisions_file.write_text(__import__("json").dumps(local))
    monkeypatch.setattr(sync, "DECISIONS_FILE", str(decisions_file))
    monkeypatch.setattr(sync, "REPLIES_FILE", str(tmp_path / "replies.json"))
    monkeypatch.setattr(sync, "get_sync", lambda: FakeSync(remote))
    import agent  # sync_down imports save_jobs from agent

    monkeypatch.setattr(agent, "save_jobs", lambda jobs, _push=True: {"added": 0})
    assert sync.sync_down() is True
    return __import__("json").loads(decisions_file.read_text())


def test_newer_remote_decision_wins(monkeypatch, tmp_path):
    merged = _run_merge(
        monkeypatch,
        tmp_path,
        local={"acme|x": {"decision": "no", "ts": "2026-10-12T10:00:00Z"}},
        remote={"acme|x": {"decision": "yes", "company": "Acme", "title": "X", "ts": "2026-10-12T12:00:00Z"}},
    )
    assert merged["acme|x"]["decision"] == "yes"


def test_tombstone_kills_local_record(monkeypatch, tmp_path):
    merged = _run_merge(
        monkeypatch,
        tmp_path,
        local={"acme|x": {"decision": "yes", "ts": "2026-10-12T10:00:00Z"}},
        remote={"acme|x": {"decision": "deleted", "ts": "2026-10-12T12:00:00Z"}},
    )
    assert merged["acme|x"]["decision"] == "deleted"


def test_tombstone_tie_wins(monkeypatch, tmp_path):
    merged = _run_merge(
        monkeypatch,
        tmp_path,
        local={"acme|x": {"decision": "yes", "ts": "2026-10-12T10:00:00Z"}},
        remote={"acme|x": {"decision": "deleted", "ts": "2026-10-12T10:00:00Z"}},
    )
    assert merged["acme|x"]["decision"] == "deleted"


def test_older_tombstone_does_not_erase_newer_decision(monkeypatch, tmp_path):
    merged = _run_merge(
        monkeypatch,
        tmp_path,
        local={"acme|x": {"decision": "yes", "ts": "2026-10-12T14:00:00Z"}},
        remote={"acme|x": {"decision": "deleted", "ts": "2026-10-12T10:00:00Z"}},
    )
    assert merged["acme|x"]["decision"] == "yes"
