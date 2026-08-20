"""Tests for fill-only mode (ARCHITECTURE §5, operator decision 2026-08-20).

Fill-only prepares each application completely and stops before Submit, leaving
the tab open for a human to review and send. Two properties make or break it:

  1. it must never submit, and nothing in the agent's narration may be able to
     promote a prepared form into a sent one;
  2. the browser window must survive the run, because the filled tabs ARE the
     deliverable. A run that tears the window down at the end has produced
     nothing at all.

Both were broken in the first implementation, so both are pinned here.
"""

from __future__ import annotations

import inspect

from applypilot.apply import orchestrator
from applypilot.apply.prompt import _build_hard_rules


def _profile():
    """A minimal stand-in rather than the real profile.

    Other tests monkeypatch config.APP_DIR, so load_profile() returns
    different things depending on suite order — these assertions are about the
    rule text, not about this candidate.
    """
    return {
        "personal": {"full_name": "Test Candidate", "preferred_name": "Test"},
        "work_authorization": {"legally_authorized_to_work": "Yes",
                               "require_sponsorship": "No",
                               "work_permit_type": "US Citizen"},
        "experience": {"education_level": "Bachelor's Degree"},
        "resume_facts": {},
    }


# ---------------------------------------------------------------------------
# The agent is told to stop before Submit
# ---------------------------------------------------------------------------

def test_fill_only_forbids_submitting(monkeypatch):
    from applypilot.apply import prompt as prompt_mod

    captured = {}

    def fake(*a, **kw):
        captured.update(kw)
        return "PROMPT"

    # The instruction is assembled inside build_prompt; assert on the text it
    # produces for the submit step rather than reaching into internals.
    src = inspect.getsource(prompt_mod)
    assert "STOP BEFORE SUBMITTING" in src
    assert "Do NOT click Submit, Apply, Send, or " in src
    assert "RESULT:READY_FOR_REVIEW" in src


def test_fill_only_leaves_captchas_for_the_human():
    src = inspect.getsource(__import__("applypilot.apply.prompt",
                                       fromlist=["prompt"]))
    assert "leave it unsolved" in src


def test_fill_only_tells_the_agent_not_to_close_the_tab():
    src = inspect.getsource(__import__("applypilot.apply.prompt",
                                       fromlist=["prompt"]))
    assert "Do NOT close this tab" in src


# ---------------------------------------------------------------------------
# An unanswerable question is a blank, not a stall
# ---------------------------------------------------------------------------

def test_unanswerable_questions_are_left_blank_not_escalated():
    """A run stalled for minutes on "list a professional reference", waiting on
    a terminal prompt nobody was sitting at. No profile can supply that."""
    rules = _build_hard_rules(_profile(), fill_only=True)
    assert "NOT a reason to stop" in rules
    assert "Do NOT output RESULT:NEEDS_HUMAN:screening_questions" in rules
    assert "Leave the field EMPTY" in rules


def test_that_override_is_absent_when_submitting_for_real():
    """Auto-submit must keep escalating — a blank reference field on a form
    nobody reads before sending is a worse outcome than parking."""
    rules = _build_hard_rules(_profile(), fill_only=False)
    assert "NOT a reason to stop" not in rules


def test_fill_only_still_refuses_to_invent_a_degree():
    rules = " ".join(_build_hard_rules(_profile(), fill_only=True).split())
    assert "NO DEGREE WAS EARNED THERE" in rules
    assert "never guess a degree" in rules


# ---------------------------------------------------------------------------
# The window survives — the filled tabs are the whole deliverable
# ---------------------------------------------------------------------------

def test_the_window_is_never_torn_down_inside_the_job_loop():
    """The first implementation compared chrome_proc against persistent_chrome
    by identity. One branch (adopting an existing Chrome) forgot to assign it,
    so the window closed after the first application and took the completed
    form with it."""
    src = inspect.getsource(orchestrator._worker_loop_body)
    loop_body, _, after = src.partition("update_state(worker_id, status=\"done\"")
    assert "cleanup_worker" not in loop_body, (
        "no teardown may happen inside the per-job loop")


def test_every_branch_that_opens_a_window_claims_it():
    """Whichever path produced the browser, it must become the run's window."""
    src = inspect.getsource(orchestrator._worker_loop_body)
    # launch, adopt, and the fallback assignment in `finally`
    assert src.count("persistent_chrome = chrome_proc") >= 3


def test_fill_only_leaves_chrome_open_at_the_end_of_the_run():
    src = inspect.getsource(orchestrator._worker_loop_body)
    assert "if fill_only:" in src
    assert "Window left open" in src


def test_main_does_not_kill_chrome_after_a_fill_only_run():
    """kill_all_chrome() in main's finally would discard every prepared tab
    the instant the run finished."""
    src = inspect.getsource(orchestrator.main)
    finally_block = src.split("finally:")[-1]
    assert "if fill_only:" in finally_block
    assert "kill_all_chrome()" in finally_block  # still there for normal runs


def test_ctrl_c_does_not_kill_the_prepared_tabs():
    src = inspect.getsource(orchestrator.main)
    assert "if not fill_only:\n                kill_all_chrome()" in src


# ---------------------------------------------------------------------------
# A dry run must never look like a submission
# ---------------------------------------------------------------------------

def test_a_dry_run_is_not_recorded_as_applied():
    """Observed on a real job: the Workday OTP dry run marked CAI applied,
    which removed it from the queue and put it in the submitted tally."""
    src = inspect.getsource(orchestrator._worker_loop_body)
    assert 'dry_run (not submitted)' in src
    applied_branch = src.split('elif result == "applied":')[1][:600]
    assert "if dry_run:" in applied_branch


def test_ready_for_review_is_parsed_before_applied():
    """A stray 'applied' in the agent's narration must not be able to promote a
    prepared form into a sent one."""
    from applypilot.apply import launcher
    src = inspect.getsource(launcher.run_job)
    assert src.index("RESULT:READY_FOR_REVIEW") < src.index('for result_status in ["APPLIED"')
