"""One-time-code extraction and the apply-worker handoff.

Section 12. Workday, Oracle Cloud, SuccessFactors and friends require a per
company account, and account creation requires a verification code sent by
email. Without this the worker parks and the application never completes, so
these ATSes are unreachable — and in Abdur-Rahman's actual result mix they are
a large share of the volume, not an edge case.

Detection is pure pattern matching and runs before any LLM call in the triage
chain: a verification code is unambiguous, and paying a model to read one would
be silly.

The handoff is deliberately dumb. The poller writes codes into `email_events`;
a worker blocked on account creation calls `wait_for_code` and polls that table
until a fresh, unconsumed code shows up or the wait expires. No sockets, no
callbacks, no shared memory between processes — just the database both sides
already use.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# How long a code stays usable. ATS codes typically expire in 10-15 minutes,
# and accepting an older one means typing a dead code into a form.
CODE_TTL_MINUTES = 15

# How long a worker waits for a code before parking permanently for the session.
DEFAULT_WAIT_SECONDS = 600
POLL_INTERVAL_SECONDS = 20

# Subject/snippet markers that mean "this mail carries a code".
OTP_MARKERS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\bverification code\b",
        r"\bverify your (?:email|account|address)\b",
        r"\bone[- ]time (?:code|password|pin)\b",
        r"\bsecurity code\b",
        r"\bconfirmation code\b",
        r"\baccess code\b",
        r"\byour code is\b",
        r"\bcode to (?:verify|confirm|activate)\b",
        r"\bactivate your account\b",
        r"\bconfirm your email\b",
        r"\botp\b",
    )
]

# The code itself. Ordered: an explicitly labelled code wins over a bare number,
# because bodies are full of bare numbers (dates, req IDs, phone fragments).
_LABELLED_CODE = re.compile(
    r"(?:code|otp|pin|password)\D{0,20}?\b([0-9]{4,8})\b", re.IGNORECASE)
_BARE_CODE = re.compile(r"\b([0-9]{6})\b")

# Numbers that look like codes but are not.
_NOT_A_CODE = re.compile(r"^(?:19|20)\d{2}$")  # years


def _now() -> datetime:
    return datetime.now(timezone.utc)


def looks_like_otp(subject: str | None, snippet: str | None = None,
                   body: str | None = None) -> bool:
    """True when an email advertises a verification code."""
    haystack = " ".join(filter(None, (subject, snippet, body)))
    return any(marker.search(haystack) for marker in OTP_MARKERS)


def extract_code(*texts: str | None) -> str | None:
    """Pull the verification code out of an email.

    Prefers an explicitly labelled code ("Your code is 123456") over a bare
    six-digit run, and rejects things that are obviously not codes.
    """
    haystack = " ".join(filter(None, texts))
    if not haystack:
        return None

    for match in _LABELLED_CODE.finditer(haystack):
        candidate = match.group(1)
        if not _NOT_A_CODE.match(candidate):
            return candidate

    for match in _BARE_CODE.finditer(haystack):
        candidate = match.group(1)
        if not _NOT_A_CODE.match(candidate):
            return candidate

    return None


def record_code(conn: sqlite3.Connection, gmail_msg_id: str, code: str,
                company_guess: str | None = None,
                application_id: str | None = None,
                received_at: str | None = None) -> int | None:
    """Store one extracted code. Idempotent on gmail_msg_id."""
    try:
        cur = conn.execute(
            "INSERT INTO email_events (gmail_msg_id, classified_as, company_guess, "
            "application_id, otp_code, received_at) VALUES (?, 'otp', ?, ?, ?, ?)",
            (gmail_msg_id, company_guess, application_id, code,
             received_at or _now().isoformat()))
        conn.commit()
        log.info("Recorded OTP from %s (company=%s)", gmail_msg_id, company_guess)
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # already recorded


def latest_unconsumed_code(conn: sqlite3.Connection,
                           company_hint: str | None = None,
                           ttl_minutes: int = CODE_TTL_MINUTES) -> dict | None:
    """Newest unconsumed, unexpired code, optionally preferring one company.

    A company hint is a preference, not a filter: several ATSes send from a
    generic no-reply address that carries no company name at all, and refusing
    those would make the whole loop useless.
    """
    cutoff = (_now() - timedelta(minutes=ttl_minutes)).isoformat()

    if company_hint:
        row = conn.execute(
            "SELECT * FROM email_events WHERE classified_as = 'otp' "
            "AND otp_consumed_at IS NULL AND received_at >= ? "
            "AND LOWER(COALESCE(company_guess, '')) LIKE ? "
            "ORDER BY received_at DESC LIMIT 1",
            (cutoff, f"%{company_hint.lower()}%")).fetchone()
        if row:
            return dict(row)

    row = conn.execute(
        "SELECT * FROM email_events WHERE classified_as = 'otp' "
        "AND otp_consumed_at IS NULL AND received_at >= ? "
        "ORDER BY received_at DESC LIMIT 1", (cutoff,)).fetchone()
    return dict(row) if row else None


def consume_code(conn: sqlite3.Connection, event_id: int) -> None:
    """Mark a code used so two workers cannot type the same one."""
    conn.execute("UPDATE email_events SET otp_consumed_at = ? WHERE id = ?",
                 (_now().isoformat(), event_id))
    conn.commit()


def wait_for_code(conn: sqlite3.Connection, company_hint: str | None = None,
                  timeout_seconds: int = DEFAULT_WAIT_SECONDS,
                  poll_seconds: int = POLL_INTERVAL_SECONDS,
                  sleep=time.sleep) -> str | None:
    """Block until a code arrives, or give up.

    Returns the code and marks it consumed, or None on timeout — at which point
    the caller parks the application permanently for this session rather than
    holding a browser open indefinitely. Nothing in a run ever waits forever.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        event = latest_unconsumed_code(conn, company_hint)
        if event:
            consume_code(conn, event["id"])
            log.info("Consumed OTP %s for %s", event["otp_code"], company_hint or "?")
            return event["otp_code"]

        if time.monotonic() >= deadline:
            log.warning("No OTP arrived within %ss for %s",
                        timeout_seconds, company_hint or "?")
            return None
        sleep(min(poll_seconds, max(0, deadline - time.monotonic())))


def process_email(conn: sqlite3.Connection, email: dict) -> str | None:
    """Classify and store one email if it carries a code. Returns the code."""
    subject = email.get("subject")
    snippet = email.get("snippet")
    body = email.get("body_text")

    if not looks_like_otp(subject, snippet, body):
        return None

    code = extract_code(subject, snippet, body)
    if not code:
        log.debug("Email %s looks like an OTP but no code parsed",
                  email.get("email_id"))
        return None

    record_code(conn, email.get("email_id") or email.get("id"), code,
                company_guess=email.get("company_guess"),
                received_at=email.get("received_at"))
    return code


# ── Fast rejects ──────────────────────────────────────────────────────────

# A rejection inside this window is a knockout answer or a keyword screen, not
# a human decision (section 12). The point is not the label, it is that the
# question-bank answers used on that application get inspected at the next gate.
FAST_REJECT_HOURS = 48


def tag_fast_rejects(conn: sqlite3.Connection,
                     hours: int = FAST_REJECT_HOURS) -> list[dict]:
    """Find rejections that landed suspiciously soon after submission.

    Returns one row per fast reject, with the Q&A answers used on that
    application attached so the gate can show what might have knocked it out.
    """
    rows = conn.execute(
        "SELECT e.id, e.application_id, e.received_at, j.title, j.site, j.applied_at "
        "FROM email_events e JOIN jobs j ON j.url = e.application_id "
        "WHERE e.classified_as = 'rejection' AND j.applied_at IS NOT NULL "
        "AND julianday(e.received_at) - julianday(j.applied_at) <= ?",
        (hours / 24.0,)).fetchall()

    out = []
    for row in rows:
        answers = conn.execute(
            "SELECT question_text, answer_text FROM qa_knowledge WHERE job_url = ?",
            (row["application_id"],)).fetchall()
        out.append({
            "application_id": row["application_id"],
            "company": row["site"],
            "title": row["title"],
            "applied_at": row["applied_at"],
            "rejected_at": row["received_at"],
            "answers_used": [dict(a) for a in answers],
        })
    if out:
        log.info("%d fast reject(s) inside %dh — answers flagged for review",
                 len(out), hours)
    return out
