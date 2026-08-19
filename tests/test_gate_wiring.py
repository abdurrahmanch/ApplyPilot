"""Tests for the wire-up between the review gate and the rest of the pipeline.

`test_review_gate.py` covers what escalates and what approval means. This file
covers the two things that made the gate advisory rather than binding:

1. nothing ever called `build_batch`, so the prepare run ended at the cover
   stage and the gate was never populated;
2. `acquire_job` selected straight from the jobs table, so approval had no
   effect on what got submitted.

Both are behavioural: a regression here means either applications go out
unreviewed, or reviewed applications never go out.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _setup_apply_env(monkeypatch) -> None:
    """Wide-open company caps, so only the gate decides."""
    from applypilot import config
    monkeypatch.setattr(config, "get_company_limit", lambda key: (-1, 30),
                        raising=False)


def _approve_everything(conn, batch_id: int) -> dict:
    from applypilot.review.batch import approve_batch, get_batch, resolve_item
    for item in get_batch(conn, batch_id)["items"]:
        resolve_item(conn, item["id"], "approved")
    return approve_batch(conn, batch_id)


# ---------------------------------------------------------------------------
# Membership: a clean application must still be able to reach submission
# ---------------------------------------------------------------------------

def test_flawless_application_is_submittable(tmp_db):
    """An application that raises no review items at all is the best case.

    Before batch membership was recorded, it was also the case the gate
    blocked forever: with no `review_items` row there was nothing to derive
    membership from, so it never appeared in `submittable_applications`.
    """
    from applypilot.review.batch import build_batch, approve_batch, submittable_applications

    conn = tmp_db()
    bid = build_batch(conn, [{"url": "https://x/clean", "title": "Backend Engineer",
                              "company": "Acme", "cover_letter": None,
                              "questions": [], "status": "ready"}])
    assert submittable_applications(conn, bid) == []   # still pending

    approve_batch(conn, bid)
    assert submittable_applications(conn, bid) == ["https://x/clean"]


def test_parked_membership_is_never_submittable(tmp_db):
    from applypilot.review.batch import build_batch, approve_batch, submittable_applications

    conn = tmp_db()
    bid = build_batch(conn, [{"url": "https://x/parked", "title": "SRE",
                              "company": "Acme", "status": "parked",
                              "reason": "captcha", "questions": []}])
    approve_batch(conn, bid)
    assert submittable_applications(conn, bid) == []


# ---------------------------------------------------------------------------
# gate_state: the submit phase's view of the gate
# ---------------------------------------------------------------------------

def test_gate_is_shut_when_no_batch_exists(tmp_db):
    from applypilot.review.batch import gate_state

    state = gate_state(tmp_db())
    assert state["open"] is False
    assert "no review batch" in state["reason"]


def test_gate_is_shut_while_a_batch_awaits_review(tmp_db):
    from applypilot.review.batch import build_batch, gate_state

    conn = tmp_db()
    bid = build_batch(conn, [{"url": "https://x/1", "title": "T", "company": "Acme",
                              "questions": [], "status": "ready"}])
    state = gate_state(conn)
    assert state["open"] is False
    assert f"batch {bid}" in state["reason"]


def test_gate_opens_on_approval(tmp_db):
    from applypilot.review.batch import build_batch, gate_state

    conn = tmp_db()
    bid = build_batch(conn, [{"url": "https://x/1", "title": "T", "company": "Acme",
                              "questions": [], "status": "ready"}])
    _approve_everything(conn, bid)

    state = gate_state(conn)
    assert state["open"] is True
    assert state["cleared"] == {"https://x/1"}


def test_cleared_urls_unions_across_batches(tmp_db):
    """An older approved batch is not invalidated by a newer one."""
    from applypilot.review.batch import build_batch, cleared_urls

    conn = tmp_db()
    first = build_batch(conn, [{"url": "https://x/1", "title": "T", "company": "A",
                                "questions": [], "status": "ready"}])
    _approve_everything(conn, first)
    second = build_batch(conn, [{"url": "https://x/2", "title": "T", "company": "B",
                                 "questions": [], "status": "ready"}])
    _approve_everything(conn, second)

    assert cleared_urls(conn) == {"https://x/1", "https://x/2"}


# ---------------------------------------------------------------------------
# acquire_job: approval is binding, not advisory
# ---------------------------------------------------------------------------

def test_acquire_job_refuses_when_the_gate_is_shut(tmp_db, seed_job, monkeypatch):
    _setup_apply_env(monkeypatch)
    from applypilot.apply.launcher import acquire_job

    conn = tmp_db()
    seed_job(conn, url_suffix="ungated", fit_score=10)

    assert acquire_job(min_score=8, max_age_days=0) is None, (
        "A perfectly eligible job must not be acquired while no batch has "
        "cleared it — that is the whole point of the gate."
    )


def test_acquire_job_returns_only_cleared_applications(tmp_db, seed_job, monkeypatch):
    _setup_apply_env(monkeypatch)
    from applypilot.apply.launcher import acquire_job
    from applypilot.review.batch import build_batch

    conn = tmp_db()
    cleared = seed_job(conn, url_suffix="cleared", fit_score=10, company="alpha",
                       application_url="https://boards.greenhouse.io/alpha/jobs/1")
    seed_job(conn, url_suffix="unreviewed", fit_score=10, company="beta",
             application_url="https://boards.greenhouse.io/beta/jobs/1")

    bid = build_batch(conn, [{"url": cleared["url"], "title": "T",
                              "company": "alpha", "questions": [],
                              "status": "ready"}])
    _approve_everything(conn, bid)

    job = acquire_job(min_score=8, max_age_days=0)
    assert job is not None
    assert job["url"] == cleared["url"]


def test_an_unresolved_item_holds_its_application_back(tmp_db, seed_job, monkeypatch):
    _setup_apply_env(monkeypatch)
    from applypilot.apply.launcher import acquire_job
    from applypilot.review.batch import approve_batch, build_batch

    conn = tmp_db()
    held = seed_job(conn, url_suffix="held", fit_score=10)

    bid = build_batch(conn, [{"url": held["url"], "title": "T", "company": "acme",
                              "questions": ["Describe a time you disagreed."],
                              "status": "ready"}])
    approve_batch(conn, bid, allow_partial=True)   # nothing resolved

    assert acquire_job(min_score=8, max_age_days=0) is None


def test_no_gate_bypass_still_works(tmp_db, seed_job, monkeypatch):
    """The escape hatch must stay usable — but only when asked for."""
    _setup_apply_env(monkeypatch)
    from applypilot.apply.launcher import acquire_job

    conn = tmp_db()
    seed_job(conn, url_suffix="bypass", fit_score=10)

    assert acquire_job(min_score=8, max_age_days=0, require_gate=False) is not None


def test_explicit_target_url_bypasses_the_gate(tmp_db, seed_job, monkeypatch):
    """Naming one job is itself a human decision, and it is how the
    crash-reconnect probe finishes a job that was already cleared."""
    _setup_apply_env(monkeypatch)
    from applypilot.apply.launcher import acquire_job

    conn = tmp_db()
    row = seed_job(conn, url_suffix="targeted", fit_score=10)

    job = acquire_job(target_url=row["url"], min_score=8, max_age_days=0)
    assert job is not None and job["url"] == row["url"]


# ---------------------------------------------------------------------------
# Batch closeout
# ---------------------------------------------------------------------------

def test_batch_closes_only_once_everything_has_gone_out(tmp_db, seed_job):
    from applypilot.review.batch import (
        build_batch, close_completed_batches, get_batch)

    conn = tmp_db()
    a = seed_job(conn, url_suffix="a", fit_score=10)
    b = seed_job(conn, url_suffix="b", fit_score=10)
    bid = build_batch(conn, [
        {"url": a["url"], "title": "T", "company": "acme", "questions": [],
         "status": "ready"},
        {"url": b["url"], "title": "T", "company": "acme", "questions": [],
         "status": "ready"},
    ])
    _approve_everything(conn, bid)

    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE jobs SET applied_at = ? WHERE url = ?", (now, a["url"]))
    conn.commit()
    assert close_completed_batches(conn) == []      # b is still outstanding

    conn.execute("UPDATE jobs SET applied_at = ? WHERE url = ?", (now, b["url"]))
    conn.commit()
    assert close_completed_batches(conn) == [bid]
    assert get_batch(conn, bid)["status"] == "submitted"


# ---------------------------------------------------------------------------
# review.prepare: what the prepare run actually puts in the batch
# ---------------------------------------------------------------------------

def test_prepare_collects_ready_applications(tmp_db, seed_job, tmp_path):
    from applypilot.review.prepare import collect_applications

    conn = tmp_db()
    letter = tmp_path / "acme_CL.txt"
    letter.write_text("Dear Hiring Manager,\n\nSpring Boot at Duha Media.\n\nA")
    row = seed_job(conn, url_suffix="ready", fit_score=9,
                   cover_letter_path=str(letter))

    apps = collect_applications(conn, min_score=8, max_age_days=0)
    assert [a["url"] for a in apps] == [row["url"]]
    assert apps[0]["status"] == "ready"
    assert "Spring Boot" in apps[0]["cover_letter"]


def test_cover_letter_is_read_from_the_txt_beside_the_pdf(tmp_db, seed_job, tmp_path):
    """The pdf stage rewrites `cover_letter_path` to the converted file. The
    gate still has to find the text, or no batch ever shows a cover delta."""
    from applypilot.review.prepare import collect_applications

    conn = tmp_db()
    (tmp_path / "acme_CL.txt").write_text(
        "Dear Hiring Manager,\n\nKafka consumers at Duha Media.\n\nA")
    seed_job(conn, url_suffix="converted", fit_score=9,
             cover_letter_path=str(tmp_path / "acme_CL.pdf"))

    apps = collect_applications(conn, min_score=8, max_age_days=0)
    assert "Kafka" in apps[0]["cover_letter"]


def test_manual_ats_jobs_are_awareness_not_ready(tmp_db, seed_job, monkeypatch):
    """`acquire_job` marks manual-ATS URLs `manual_only` on sight. Clearing one
    through the gate would spend his attention on an application the submit
    phase is guaranteed to refuse."""
    from applypilot import config
    from applypilot.review.prepare import collect_applications

    monkeypatch.setattr(config, "is_manual_ats",
                        lambda url: "manualats" in url, raising=False)
    conn = tmp_db()
    seed_job(conn, url_suffix="manual-ats", fit_score=9,
             application_url="https://manualats.example.com/apply/1")

    apps = collect_applications(conn, min_score=8, max_age_days=0)
    assert [a["status"] for a in apps] == ["disqualified"]
    assert "manual" in apps[0]["reason"]


def test_prepare_excludes_already_applied_and_low_score(tmp_db, seed_job):
    from applypilot.review.prepare import collect_applications

    conn = tmp_db()
    seed_job(conn, url_suffix="applied", fit_score=10,
             applied_at=datetime.now(timezone.utc).isoformat())
    seed_job(conn, url_suffix="low", fit_score=3)

    assert collect_applications(conn, min_score=8, max_age_days=0) == []


def test_prepare_surfaces_parked_and_disqualified(tmp_db, seed_job):
    from applypilot.apply.pacing import park_application
    from applypilot.review.prepare import collect_applications

    conn = tmp_db()
    parked = seed_job(conn, url_suffix="parked", fit_score=9)
    park_application(conn, parked["url"], "captcha")
    seed_job(conn, url_suffix="manual", fit_score=9, state="manual_only",
             apply_error="ATS blocks automation")

    by_status = {a["status"]: a for a in
                 collect_applications(conn, min_score=8, max_age_days=0)}
    assert "parked" in by_status
    assert by_status["parked"]["reason"] == "captcha"
    assert "disqualified" in by_status


def test_sensitive_questions_are_raised_once_per_category(tmp_db):
    """Work auth, sponsorship, and salary must reach him every single batch —
    but as three confirmations, not as one per stored phrasing per application.
    The bank holds many wordings of the same question; he answers the topic."""
    from applypilot.questions import add_question
    from applypilot.review.prepare import sensitive_questions

    conn = tmp_db()
    add_question(conn, "Will you require sponsorship?", "No", category="sponsorship")
    add_question(conn, "Do you need visa sponsorship?", "No", category="sponsorship")
    add_question(conn, "What is your desired salary?", "$95k", category="salary")
    add_question(conn, "How did you hear about us?", "Job board", category="other")

    questions = sensitive_questions(conn)
    assert len(questions) == 2, questions
    assert any("sponsorship" in q.lower() for q in questions)
    assert any("salary" in q.lower() for q in questions)
    assert "How did you hear about us?" not in questions


def test_batch_questions_block_every_application_until_answered(tmp_db):
    """A sponsorship answer applies to the whole batch, so an unanswered one
    holds the whole batch — not merely some application it was filed under."""
    from applypilot.review.batch import (
        approve_batch, build_batch, get_batch, resolve_item,
        submittable_applications)

    conn = tmp_db()
    bid = build_batch(
        conn,
        [{"url": "https://x/1", "title": "T", "company": "A", "questions": [],
          "status": "ready"}],
        batch_questions=["Will you require sponsorship?"])
    approve_batch(conn, bid, allow_partial=True)
    assert submittable_applications(conn, bid) == []

    batch_item = next(i for i in get_batch(conn, bid)["items"]
                      if i["application_id"] is None)
    resolve_item(conn, batch_item["id"], "No")
    assert submittable_applications(conn, bid) == ["https://x/1"]


def test_ats_history_drives_per_application_questions(tmp_db):
    """Per-application anticipation comes from what this ATS has actually
    asked, not from the bank at large."""
    from applypilot.questions import add_question, record_sighting
    from applypilot.review.prepare import anticipated_questions

    conn = tmp_db()
    qid = add_question(conn, "How did you hear about us?", "Job board",
                       category="other")
    record_sighting(conn, qid, "How did you hear about us?",
                    application_id="https://x/1", ats="greenhouse")

    greenhouse = anticipated_questions(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert "How did you hear about us?" in greenhouse
    assert anticipated_questions(conn, None) == []


def test_build_run_batch_reports_what_it_built(tmp_db, seed_job):
    from applypilot.review.prepare import build_run_batch

    conn = tmp_db()
    seed_job(conn, url_suffix="one", fit_score=9)
    summary = build_run_batch(conn, min_score=8, max_age_days=0)

    assert summary["batch_id"] is not None
    assert summary["counts"]["ready"] == 1


def test_build_run_batch_builds_nothing_from_an_empty_run(tmp_db):
    from applypilot.review.prepare import build_run_batch

    summary = build_run_batch(tmp_db(), min_score=8, max_age_days=0)
    assert summary["batch_id"] is None


# ---------------------------------------------------------------------------
# Pipeline stage
# ---------------------------------------------------------------------------

def test_gate_is_the_last_stage_and_runs_after_cover(tmp_db):
    from applypilot.pipeline import STAGE_ORDER, _STAGE_RUNNERS, _UPSTREAM

    assert STAGE_ORDER[-1] == "gate"
    assert _UPSTREAM["gate"] == "pdf"
    assert "gate" in _STAGE_RUNNERS


def test_gate_stage_builds_a_batch(tmp_db, seed_job):
    from applypilot.pipeline import _run_gate
    from applypilot.review.batch import get_pending_batch

    conn = tmp_db()
    seed_job(conn, url_suffix="staged", fit_score=9)

    result = _run_gate(min_score=8, max_age_days=0)
    assert result["status"] == "ok"
    assert get_pending_batch(conn)["id"] == result["batch_id"]
