"""Tests for the TUI decision store: tombstone-aware read/write round-trip."""
import json

import pytest

import jobfinder.ui.tui as tui


@pytest.fixture()
def decisions_file(monkeypatch, tmp_path):
    path = tmp_path / "job_decisions.json"
    monkeypatch.setattr(tui, "DECISIONS_FILE", str(path))
    return path


def test_undo_writes_tombstone_and_live_dict_stays_clean(decisions_file):
    live = {"acme|x": {"decision": "yes", "ts": "2026-10-12T10:00:00Z"}}
    tui.record_decision(live, "acme|x", {"decision": "yes", "company": "Acme", "title": "X"})
    tui.record_decision(live, "acme|x", None)  # undo

    on_disk = json.loads(decisions_file.read_text())
    assert on_disk["acme|x"]["decision"] == "deleted"  # tombstone persisted for sync
    assert "acme|x" not in live  # UI never sees the plumbing


def test_load_decisions_hides_tombstones(decisions_file):
    decisions_file.write_text(json.dumps({
        "a": {"decision": "yes"},
        "b": {"decision": "deleted"},
    }))
    loaded = tui.load_decisions()
    assert list(loaded) == ["a"]


def test_new_decision_overwrites_tombstone(decisions_file):
    decisions_file.write_text(json.dumps({"acme|x": {"decision": "deleted"}}))
    live = {}
    tui.record_decision(live, "acme|x", {"decision": "yes", "company": "Acme", "title": "X"})
    on_disk = json.loads(decisions_file.read_text())
    assert on_disk["acme|x"]["decision"] == "yes"
    assert live["acme|x"]["decision"] == "yes"
