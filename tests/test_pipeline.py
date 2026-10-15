"""Tests for the search pipeline: dedup ordering, truncated validation, location policy."""
from datetime import datetime, timedelta, timezone

import agent


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


# --- dedup / staleness ordering (the poisoned-key bug) ------------------------------

def test_stale_variant_does_not_poison_fresh_repost():
    """Old code registered the dedup key BEFORE the age check: an ancient variant
    of a job swallowed a fresh repost from another source entirely."""
    stale = {"url": "https://old.io/j/1", "company": "Acme", "title": "Head of Product",
             "posted_date": _days_ago(200)}
    fresh = {"url": "https://better.io/j/9", "company": "Acme", "title": "Head of Product",
             "posted_date": _days_ago(3)}
    kept = agent._dedup_and_filter_stale([stale, fresh], set(), max_age_days=90)
    assert [j["url"] for j in kept] == ["https://better.io/j/9"]


def test_same_key_variants_resolve_to_freshest():
    old_variant = {"url": "https://a.io/1", "company": "Acme", "title": "Lead",
                   "posted_date": _days_ago(80)}
    new_variant = {"url": "https://b.io/2", "company": "Acme", "title": "Lead",
                   "posted_date": _days_ago(2)}
    kept = agent._dedup_and_filter_stale([old_variant, new_variant], set(), max_age_days=90)
    assert [j["url"] for j in kept] == ["https://b.io/2"]


def test_dated_beats_undated_variant():
    undated = {"url": "https://a.io/1", "company": "Acme", "title": "Lead", "posted_date": ""}
    dated = {"url": "https://b.io/2", "company": "Acme", "title": "Lead",
             "posted_date": _days_ago(5)}
    kept = agent._dedup_and_filter_stale([undated, dated], set(), max_age_days=90)
    assert [j["url"] for j in kept] == ["https://b.io/2"]


def test_seen_keys_still_suppress_known_jobs():
    seen = {"acme|lead"}
    job = {"url": "https://a.io/1", "company": "Acme", "title": "Lead", "posted_date": _days_ago(1)}
    assert agent._dedup_and_filter_stale([job], seen, max_age_days=90) == []


def test_unidentifiable_jobs_are_never_deduped_away():
    a = {"url": "", "company": "", "title": "", "posted_date": ""}
    b = {"url": "", "company": "", "title": "", "posted_date": ""}
    assert len(agent._dedup_and_filter_stale([a, b], set(), max_age_days=90)) == 2


# --- truncated validation replies ----------------------------------------------------

def test_salvage_recovers_complete_evaluations():
    truncated = (
        '{"evaluations": [{"id": 0, "fit": 5, "startup": 4, "climate": 4, '
        '"reason": "core mission"}, {"id": 1, "fit": 4, "startup": 3, "climate": 5, '
        '"reason": "cut off here'
    )
    data = agent._salvage_evaluations_json(truncated)
    assert data is not None
    evals = data["evaluations"]
    assert len(evals) == 1 and evals[0]["id"] == 0


def test_salvage_returns_none_for_unparseable():
    assert agent._salvage_evaluations_json("sorry, I cannot evaluate these") is None


def test_salvage_handles_complete_json():
    complete = '{"evaluations": [{"id": 0, "fit": 5, "startup": 4, "climate": 4, "reason": "ok"}]}'
    data = agent._salvage_evaluations_json(complete)
    assert data and len(data["evaluations"]) == 1


def test_missing_evaluations_fall_back_individually_not_whole_batch():
    """validate_jobs_batch: only the jobs the LLM actually omitted get fallback
    scores; salvaged ones keep their real scores."""
    jobs = [
        {"title": "A", "company": "C1", "source": "climatebase"},
        {"title": "B", "company": "C2", "source": "climatebase"},
    ]
    real = {"0": {"fit": 5, "startup": 4, "climate": 5, "reason": "real"}}
    agent._validation_json = lambda prompt: real
    out = agent.validate_jobs_batch(jobs, "profile")
    assert out[0]["fit_score"] == 5 and out[0]["climate_score"] == 5
    assert out[1]["reason"] == ""  # the genuinely-missing tail falls back


# --- location policy: Germany / Freiburg ---------------------------------------------

def _verdict(location, snippet="", wfh=""):
    return agent._preferred_location_verdict(
        {"location": location, "snippet": snippet, "wfh_policy": wfh}
    )


def test_de_remote_kept():
    ok, _ = _verdict("Germany", snippet="fully remote, work from anywhere")
    assert ok


def test_de_freiburg_hybrid_kept():
    ok, _ = _verdict("Freiburg im Breisgau, Germany", snippet="hybrid, 2 days in office")
    assert ok


def test_de_berlin_on_site_dropped():
    ok, reason = _verdict("Berlin, Germany", snippet="on-site, 5 days in office")
    assert not ok and "Freiburg" in reason


def test_de_hybrid_outside_freiburg_dropped():
    ok, _ = _verdict("Munich, Germany", snippet="hybrid working model")
    assert not ok


def test_ch_and_nl_behaviour_unchanged():
    assert _verdict("Zurich, Switzerland", snippet="on-site")[0]
    assert _verdict("Amsterdam, Netherlands", snippet="hybrid")[0]
    # Note: "no remote" counts as a remote hint in the existing policy (substring
    # match) — kept as-is here; worth a separate look later.
    assert not _verdict("Amsterdam, Netherlands", snippet="on-site only, must be in office")[0]
