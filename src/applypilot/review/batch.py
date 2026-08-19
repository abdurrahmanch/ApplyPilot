"""Batch assembly, approval, and correction propagation for the review gate."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone

from applypilot.questions import (
    REVIEW_FLOOR,
    add_question,
    apply_correction,
    decide,
)
from applypilot.voice import append_learning, check_batch_sameness, repeated_phrases

log = logging.getLogger(__name__)

ITEM_KINDS = (
    "novel_question",
    "low_confidence",
    "never_auto_guess",
    "cover_delta",
    "sameness_warning",
    "disqualified",
    "parked",
    "high_value",
)

# Items requiring a decision come first; awareness-only items sink to the
# bottom. high_value outranks everything: Chicago trading firms enforce
# one-application-per-season rules and humans read those applications, so they
# get his attention while it is freshest (section 14).
KIND_ORDER = {kind: i for i, kind in enumerate((
    "high_value",
    "never_auto_guess",
    "novel_question",
    "low_confidence",
    "sameness_warning",
    "cover_delta",
    "disqualified",
    "parked",
))}

# Employers where a wasted application cannot be retried this season.
HIGH_VALUE_EMPLOYERS = (
    "imc", "ctc", "chicago trading", "drw", "peak6", "cboe", "ninjatrader",
    "jump trading", "citadel", "optiver", "akuna", "belvedere", "wolverine",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add_item(conn: sqlite3.Connection, batch_id: int, kind: str,
              application_id: str | None, payload: dict) -> int:
    if kind not in ITEM_KINDS:
        raise ValueError(f"Unknown review item kind '{kind}'")
    cur = conn.execute(
        "INSERT INTO review_items (batch_id, application_id, kind, payload_json) "
        "VALUES (?, ?, ?, ?)",
        (batch_id, application_id, kind, json.dumps(payload)))
    return cur.lastrowid


def is_high_value(company: str | None) -> bool:
    """True for employers whose applications cannot be retried this season."""
    if not company:
        return False
    name = company.lower()
    return any(marker in name for marker in HIGH_VALUE_EMPLOYERS)


def _cover_delta(letter: str) -> dict:
    """The ten-second view of a cover letter: opening line and its specifics.

    Not the full text. Full text is expandable on demand in the TUI; showing it
    by default turns a 15-minute review into an hour of reading.
    """
    body = [p.strip() for p in re.split(r"\n\s*\n", letter) if p.strip()]
    opening = ""
    for para in body:
        if para.lower().startswith("dear"):
            continue
        opening = " ".join(para.split()[:40])
        break

    # Hooks: capitalised multi-word names and concrete nouns are what make a
    # letter specific to one employer. Crude on purpose — this is a prompt for
    # his eyes, not a claim about the letter.
    hooks = re.findall(r"\b(?:[A-Z][a-z0-9.+#]+(?:\s+[A-Z][a-z0-9.+#]+)*)\b", letter)
    hooks = [h for h in hooks if len(h) > 2 and h not in ("Dear", "Hiring Manager",
                                                          "Abdur-Rahman", "I")]
    return {
        "opening": opening,
        "hooks": sorted(set(hooks))[:6],
        "word_count": len(letter.split()),
        "repeated_phrases": repeated_phrases(letter),
        "full_text": letter,
    }


def _add_member(conn: sqlite3.Connection, batch_id: int, app: dict) -> None:
    """Record that an application belongs to this batch.

    Every prepared application gets a row, including the clean ones that raise
    no review items at all. Without this, a flawless application is invisible
    to `submittable_applications` and the gate blocks it forever.
    """
    conn.execute(
        "INSERT OR REPLACE INTO review_batch_applications "
        "(batch_id, application_id, status, title, company, reason, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (batch_id, app.get("url"), app.get("status", "ready"),
         app.get("title"), app.get("company"), app.get("reason"), _now()))


def build_batch(conn: sqlite3.Connection, applications: list[dict],
                run_date: str | None = None,
                batch_questions: list[str] | None = None) -> int:
    """Assemble one pending review batch and return its id.

    Args:
        applications: one dict per prepared application, with keys:
            url, title, company, cover_letter (str|None),
            questions (list of raw question strings),
            status ('ready'|'disqualified'|'parked'),
            reason (str, for disqualified/parked).
        batch_questions: questions whose answer is the same for every
            application in the batch — work authorisation, sponsorship,
            salary. Raised once, against no application, and blocking all of
            them until answered.

    Escalation is decided here, not in the TUI, so the same rules apply
    whatever renders them.
    """
    cur = conn.execute(
        "INSERT INTO review_batches (run_date, status) VALUES (?, 'pending')",
        (run_date or _now(),))
    batch_id = cur.lastrowid

    for raw_question in batch_questions or []:
        decision = decide(conn, raw_question)
        match = decision.get("match") or {}
        _add_item(conn, batch_id, "never_auto_guess", None, {
            "question": raw_question,
            "proposed_answer": decision.get("answer"),
            "reason": decision.get("reason"),
            "similarity": round(match.get("similarity", 0.0), 3),
            "question_id": match.get("id"),
            "matched_canonical": match.get("canonical_text"),
            "category": match.get("category"),
            "scope": "batch",
        })

    letters: dict[str, str] = {}

    for app in applications:
        url = app.get("url")
        company = app.get("company")
        status = app.get("status", "ready")
        _add_member(conn, batch_id, app)

        if status == "disqualified":
            _add_item(conn, batch_id, "disqualified", url,
                      {"title": app.get("title"), "company": company,
                       "reason": app.get("reason")})
            continue
        if status == "parked":
            _add_item(conn, batch_id, "parked", url,
                      {"title": app.get("title"), "company": company,
                       "reason": app.get("reason")})
            continue

        if is_high_value(company):
            _add_item(conn, batch_id, "high_value", url,
                      {"title": app.get("title"), "company": company,
                       "note": "One application per season. Skipping is permanent."})

        for raw_question in app.get("questions") or []:
            decision = decide(conn, raw_question)
            match = decision.get("match") or {}
            payload = {
                "question": raw_question,
                "proposed_answer": decision.get("answer"),
                "reason": decision.get("reason"),
                "similarity": round(match.get("similarity", 0.0), 3),
                "question_id": match.get("id"),
                "matched_canonical": match.get("canonical_text"),
                "category": match.get("category"),
                "company": company,
                "title": app.get("title"),
            }
            if decision["action"] == "novel":
                _add_item(conn, batch_id, "novel_question", url, payload)
            elif match.get("sensitive"):
                _add_item(conn, batch_id, "never_auto_guess", url, payload)
            elif decision["action"] == "confirm":
                _add_item(conn, batch_id, "low_confidence", url, payload)
            # 'auto' is silent — that is the entire point of the confidence gate.

        letter = app.get("cover_letter")
        if letter:
            letters[url] = letter
            _add_item(conn, batch_id, "cover_delta", url,
                      dict(_cover_delta(letter), company=company,
                           title=app.get("title")))

    for pair in check_batch_sameness(letters):
        for side, other in ((pair["a"], pair["b"]), (pair["b"], pair["a"])):
            _add_item(conn, batch_id, "sameness_warning", side,
                      {"similar_to": other, "similarity": pair["similarity"],
                       "note": "Both letters share a skeleton. Rewrite one."})

    conn.commit()
    log.info("Built review batch %d from %d applications", batch_id, len(applications))
    return batch_id


def get_pending_batch(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM review_batches WHERE status = 'pending' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    return get_batch(conn, row["id"]) if row else None


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict | None:
    batch = conn.execute("SELECT * FROM review_batches WHERE id = ?",
                         (batch_id,)).fetchone()
    if batch is None:
        return None
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM review_items WHERE batch_id = ? ORDER BY id", (batch_id,))]
    for item in items:
        item["payload"] = json.loads(item["payload_json"] or "{}")
    items.sort(key=lambda i: (KIND_ORDER.get(i["kind"], 99), i["id"]))
    return {**dict(batch), "items": items}


def unresolved_items(batch: dict) -> list[dict]:
    """Items still needing a decision. Awareness-only kinds never block."""
    awareness_only = {"disqualified", "parked", "high_value"}
    return [i for i in batch["items"]
            if i["resolution"] is None and i["kind"] not in awareness_only]


def resolve_item(conn: sqlite3.Connection, item_id: int, resolution: str) -> None:
    conn.execute(
        "UPDATE review_items SET resolution = ?, resolved_at = ? WHERE id = ?",
        (resolution, _now(), item_id))
    conn.commit()


def apply_gate_correction(conn: sqlite3.Connection, item_id: int,
                          corrected_answer: str) -> None:
    """Propagate one of his edits back into the bank (section 9's learning loop).

    An edit to a matched question corrects that entry and cuts its confidence.
    An edit to a novel question creates a new canonical entry. Either way the
    same correction is never needed twice.
    """
    row = conn.execute("SELECT * FROM review_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ValueError(f"No review item with id {item_id}")

    payload = json.loads(row["payload_json"] or "{}")
    question_text = payload.get("question")
    question_id = payload.get("question_id")

    if question_id and payload.get("similarity", 0.0) >= REVIEW_FLOOR:
        apply_correction(conn, question_id, corrected_answer)
    elif question_text:
        category = payload.get("category") or "free_text"
        add_question(conn, question_text, corrected_answer, category=category)

    resolve_item(conn, item_id, f"corrected: {corrected_answer}")


def record_cover_learning(conn: sqlite3.Connection, item_id: int,
                          note: str) -> bool:
    """Append a cover-letter lesson to the voice register.

    The pattern, never the instance: 'letters padded to hit a word target', not
    'this letter said responsive design five times'.
    """
    stamped = f"\n### {datetime.now(timezone.utc).date().isoformat()} — gate correction\n\n{note}\n"
    ok = append_learning(stamped)
    resolve_item(conn, item_id, f"learning recorded: {note[:80]}")
    return ok


def approve_batch(conn: sqlite3.Connection, batch_id: int,
                  allow_partial: bool = True) -> dict:
    """Approve a batch. This is the ONLY trigger for the submit phase.

    Applications with unresolved items stay behind; the rest become
    submittable. A batch with nothing left outstanding is 'approved'; one with
    items still open is 'partial'.
    """
    batch = get_batch(conn, batch_id)
    if batch is None:
        raise ValueError(f"No review batch with id {batch_id}")

    outstanding = unresolved_items(batch)
    if outstanding and not allow_partial:
        raise ValueError(
            f"{len(outstanding)} item(s) still unresolved; "
            "resolve them or approve with allow_partial=True")

    status = "partial" if outstanding else "approved"
    conn.execute("UPDATE review_batches SET status = ?, approved_at = ? WHERE id = ?",
                 (status, _now(), batch_id))
    conn.commit()

    held_back = sorted({i["application_id"] for i in outstanding if i["application_id"]})
    submittable = submittable_applications(conn, batch_id)
    log.info("Batch %d %s: %d submittable, %d held back",
             batch_id, status, len(submittable), len(held_back))
    return {"batch_id": batch_id, "status": status,
            "submittable": submittable, "held_back": held_back}


# Batch states in which approval has already happened. Everything else means
# the gate is still shut for that batch.
OPENED_STATES = ("approved", "partial", "submitted")


def batch_members(conn: sqlite3.Connection, batch_id: int) -> list[dict]:
    """Every application in a batch, whatever its status.

    Falls back to deriving membership from `review_items` for batches written
    before the membership table existed.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM review_batch_applications WHERE batch_id = ? "
        "ORDER BY application_id", (batch_id,))]
    if rows:
        return rows

    batch = get_batch(conn, batch_id)
    if batch is None:
        return []
    derived: dict[str, dict] = {}
    for item in batch["items"]:
        url = item["application_id"]
        if not url:
            continue
        status = item["kind"] if item["kind"] in ("disqualified", "parked") else "ready"
        existing = derived.get(url)
        if existing is None or existing["status"] == "ready":
            derived[url] = {"batch_id": batch_id, "application_id": url,
                            "status": status,
                            "title": item["payload"].get("title"),
                            "company": item["payload"].get("company"),
                            "reason": item["payload"].get("reason")}
    return sorted(derived.values(), key=lambda m: m["application_id"])


def submittable_applications(conn: sqlite3.Connection, batch_id: int) -> list[str]:
    """Application URLs cleared to submit: in the batch, with nothing unresolved."""
    batch = get_batch(conn, batch_id)
    if batch is None or batch["status"] not in OPENED_STATES:
        return []

    outstanding = unresolved_items(batch)
    # An item raised against no application is a batch-wide confirmation —
    # work authorisation, sponsorship, salary. Until it is answered, nothing in
    # the batch is cleared, because the answer applies to all of it.
    if any(i["application_id"] is None for i in outstanding):
        return []

    blocked = {i["application_id"] for i in outstanding}
    ready = {m["application_id"] for m in batch_members(conn, batch_id)
             if m["status"] == "ready"}
    return sorted(ready - blocked)


def cleared_urls(conn: sqlite3.Connection) -> set[str]:
    """Every application any opened batch has cleared for submission.

    The union across batches, not just the newest one: a partial batch whose
    held-back items are resolved later must not be invalidated by the next
    run's batch, and an interrupted apply run must be resumable.
    """
    urls: set[str] = set()
    for row in conn.execute(
            "SELECT id FROM review_batches WHERE status IN (?, ?, ?)",
            OPENED_STATES):
        urls.update(submittable_applications(conn, row["id"]))
    return urls


def gate_state(conn: sqlite3.Connection) -> dict:
    """What the submit phase needs to know before it acquires anything.

    Returns ``{"open": bool, "cleared": set[str], "reason": str}``. The gate is
    shut whenever nothing has been cleared, and the reason distinguishes the
    two ways that happens — no batch was ever built, or one was built and is
    still waiting on him — because the fix is different for each.
    """
    cleared = cleared_urls(conn)
    if cleared:
        return {"open": True, "cleared": cleared, "reason": ""}

    pending = conn.execute(
        "SELECT id FROM review_batches WHERE status = 'pending' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if pending is not None:
        return {"open": False, "cleared": cleared,
                "reason": f"batch {pending['id']} is still awaiting review "
                          "(run `applypilot review`)"}

    any_batch = conn.execute("SELECT COUNT(*) FROM review_batches").fetchone()[0]
    if not any_batch:
        return {"open": False, "cleared": cleared,
                "reason": "no review batch has been built "
                          "(run `applypilot run gate`)"}
    return {"open": False, "cleared": cleared,
            "reason": "every reviewed application is held back or already submitted"}


def mark_batch_submitted(conn: sqlite3.Connection, batch_id: int) -> None:
    """Close out a batch after its cleared applications have been attempted."""
    conn.execute(
        "UPDATE review_batches SET status = 'submitted' WHERE id = ? "
        "AND status IN ('approved', 'partial')", (batch_id,))
    conn.commit()


def close_completed_batches(conn: sqlite3.Connection) -> list[int]:
    """Mark every open batch whose cleared applications have all gone out.

    Called at the end of a submit run. A batch with anything still unsubmitted
    stays open, so an interrupted run resumes from where it stopped instead of
    needing a second trip through the gate.
    """
    closed: list[int] = []
    for row in conn.execute(
            "SELECT id FROM review_batches WHERE status IN ('approved', 'partial')"):
        cleared = submittable_applications(conn, row["id"])
        if not cleared:
            continue
        placeholders = ",".join("?" * len(cleared))
        outstanding = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE url IN ({placeholders}) "
            "AND applied_at IS NULL", cleared).fetchone()[0]
        if outstanding == 0:
            mark_batch_submitted(conn, row["id"])
            closed.append(row["id"])
    return closed
