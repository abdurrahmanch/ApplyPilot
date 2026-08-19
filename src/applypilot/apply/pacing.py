"""Pacing, parking, proof capture, and per-ATS health.

Section 11. Four concerns that all exist to keep a residential-IP, headed-
browser operation looking like a person applying to jobs, and to make every
submission auditable afterwards:

- **Pacing.** Randomized delays between actions and between applications, and
  a batch spread across the run window rather than fired in a burst. Runtime
  is explicitly not a constraint; reliability is everything.
- **Parking.** Any CAPTCHA, MFA, login wall, OTP wait, or unanswerable question
  sends that one application to the parked queue and the run continues.
  Nothing ever waits for a human mid-run.
- **Proof.** Every submission records a screenshot, whatever confirmation text
  or number the page showed, a timestamp, the ATS, and the worker id.
- **ATS health.** A 403, 429, or Turnstile cools that ATS for the rest of the
  run. Separately, an ATS whose success rate drops below 50% over a rolling 30
  submissions gets flagged at the next gate.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Between individual actions inside one application.
ACTION_DELAY_RANGE = (0.4, 2.1)

# Between applications. Wide and long on purpose: seventeen submissions fired
# minutes apart from one residential IP is the pattern that gets an account
# restricted, and a slow run costs nothing.
APPLICATION_DELAY_RANGE = (45.0, 210.0)

# How long an ATS stays cooled after a block signal. Section 11 says the rest
# of the run; six hours comfortably covers a run window without leaking into
# the next scheduled one.
COOLDOWN_HOURS = 6

# Rolling window and floor for the per-ATS health check.
HEALTH_WINDOW = 30
HEALTH_FLOOR = 0.5

PARK_REASONS = frozenset({
    "captcha", "mfa", "login", "otp_wait", "form_error",
    "unanswerable_question", "account_creation", "other",
})

# Signals that mean "this ATS is pushing back, stop touching it".
BLOCK_SIGNALS = ("403", "429", "turnstile", "cloudflare", "rate limit",
                 "too many requests", "access denied", "bot detect")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Pacing ────────────────────────────────────────────────────────────────

def action_delay(rng: random.Random | None = None) -> float:
    """Sleep a human-ish interval between two actions. Returns seconds slept."""
    r = rng or random
    seconds = r.uniform(*ACTION_DELAY_RANGE)
    time.sleep(seconds)
    return seconds


def application_delay(rng: random.Random | None = None) -> float:
    """Sleep between two applications. Returns seconds slept."""
    r = rng or random
    seconds = r.uniform(*APPLICATION_DELAY_RANGE)
    log.info("Pacing: waiting %.0fs before the next application", seconds)
    time.sleep(seconds)
    return seconds


def spread_schedule(count: int, window_seconds: float,
                    rng: random.Random | None = None) -> list[float]:
    """Offsets, in seconds from the start, for `count` applications.

    Evenly spaced across the window with jitter, so the batch is spread rather
    than bursted. Never returns a negative or out-of-order offset.
    """
    if count <= 0:
        return []
    if count == 1:
        return [0.0]

    r = rng or random
    step = window_seconds / count
    offsets = []
    for i in range(count):
        jitter = r.uniform(-step * 0.3, step * 0.3)
        offsets.append(max(0.0, i * step + jitter))
    offsets.sort()
    return offsets


# ── Parking ───────────────────────────────────────────────────────────────

def park_application(conn: sqlite3.Connection, application_id: str, reason: str,
                     details: dict | None = None) -> int:
    """Send one application to the parked queue. The run continues."""
    import json

    if reason not in PARK_REASONS:
        log.warning("Unknown park reason '%s'; recording as 'other'", reason)
        details = {**(details or {}), "original_reason": reason}
        reason = "other"

    cur = conn.execute(
        "INSERT INTO parked (application_id, reason, details_json, parked_at) "
        "VALUES (?, ?, ?, ?)",
        (application_id, reason, json.dumps(details or {}), _now()))
    conn.commit()
    log.info("Parked %s (%s)", application_id, reason)
    return cur.lastrowid


def resolve_parked(conn: sqlite3.Connection, parked_id: int,
                   outcome: str = "resolved") -> None:
    conn.execute("UPDATE parked SET resolved_at = ?, outcome = ? WHERE id = ?",
                 (_now(), outcome, parked_id))
    conn.commit()


def open_parked(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM parked WHERE resolved_at IS NULL ORDER BY parked_at DESC")]


# ── Proof ─────────────────────────────────────────────────────────────────

def record_proof(conn: sqlite3.Connection, application_id: str, outcome: str,
                 ats: str | None = None, worker_id: int | None = None,
                 screenshot_path: str | None = None,
                 confirmation_text: str | None = None,
                 confirmation_id: str | None = None,
                 duration_ms: int | None = None) -> int:
    """Record what happened on one submission attempt.

    Written for every outcome, not just successes: a failed or parked attempt
    is exactly what you want evidence of when an ATS's numbers look wrong.
    """
    cur = conn.execute(
        "INSERT INTO submission_proof (application_id, ats, worker_id, outcome, "
        "screenshot_path, confirmation_text, confirmation_id, duration_ms, submitted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (application_id, ats, worker_id, outcome, screenshot_path,
         confirmation_text, confirmation_id, duration_ms, _now()))
    conn.commit()
    return cur.lastrowid


def proof_for(conn: sqlite3.Connection, application_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM submission_proof WHERE application_id = ? "
        "ORDER BY submitted_at DESC", (application_id,))]


# ── ATS cooldowns ─────────────────────────────────────────────────────────

def looks_like_a_block(text: str | None) -> bool:
    """True when an error string carries a rate-limit or bot-detection signal."""
    if not text:
        return False
    lowered = text.lower()
    return any(signal in lowered for signal in BLOCK_SIGNALS)


def cool_down_ats(conn: sqlite3.Connection, ats: str, reason: str,
                  hours: int = COOLDOWN_HOURS) -> None:
    """Stop touching one ATS for a while. Idempotent; the latest reason wins."""
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    conn.execute(
        "INSERT INTO ats_cooldowns (ats, reason, cooled_at, expires_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(ats) DO UPDATE SET reason = excluded.reason, "
        "cooled_at = excluded.cooled_at, expires_at = excluded.expires_at",
        (ats, reason, _now(), expires))
    conn.commit()
    log.warning("Cooling down ATS '%s' for %dh: %s", ats, hours, reason)


def is_cooled_down(conn: sqlite3.Connection, ats: str | None) -> bool:
    if not ats:
        return False
    row = conn.execute(
        "SELECT expires_at FROM ats_cooldowns WHERE ats = ?", (ats,)).fetchone()
    if row is None:
        return False
    expires = row["expires_at"]
    if expires and expires <= _now():
        conn.execute("DELETE FROM ats_cooldowns WHERE ats = ?", (ats,))
        conn.commit()
        return False
    return True


def active_cooldowns(conn: sqlite3.Connection) -> list[dict]:
    now = _now()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM ats_cooldowns WHERE expires_at IS NULL OR expires_at > ?",
        (now,))]


# ── ATS health ────────────────────────────────────────────────────────────

def ats_health(conn: sqlite3.Connection, window: int = HEALTH_WINDOW) -> list[dict]:
    """Success rate per ATS over its last `window` attempts.

    Only ATSes with a full window are judged: calling an ATS unhealthy off
    three attempts would flag normal variance as a problem.
    """
    rows = conn.execute(
        "SELECT DISTINCT ats FROM submission_proof WHERE ats IS NOT NULL").fetchall()
    report = []
    for row in rows:
        ats = row["ats"]
        recent = conn.execute(
            "SELECT outcome FROM submission_proof WHERE ats = ? "
            "ORDER BY submitted_at DESC LIMIT ?", (ats, window)).fetchall()
        total = len(recent)
        applied = sum(1 for r in recent if r["outcome"] == "applied")
        rate = applied / total if total else 0.0
        report.append({
            "ats": ats,
            "attempts": total,
            "applied": applied,
            "success_rate": round(rate, 3),
            "judged": total >= window,
            "unhealthy": total >= window and rate < HEALTH_FLOOR,
        })
    report.sort(key=lambda r: r["success_rate"])
    return report


def unhealthy_ats(conn: sqlite3.Connection, window: int = HEALTH_WINDOW) -> list[dict]:
    """ATSes to flag at the next gate, recommending prep-only."""
    return [r for r in ats_health(conn, window) if r["unhealthy"]]
