"""Assemble the review batch from the database at the end of a prepare run.

The gate itself (`review.batch`) knows how to escalate; it does not know how to
read the pipeline. This module is the seam. It answers one question — "what did
this run actually prepare?" — and hands the answer to `build_batch`.

Three things go into a batch:

  ready         applications with a tailored resume, a cover letter, and an
                apply URL, that nothing has submitted yet
  disqualified  applications that got far and then stopped — tailor or cover
                failed, the ATS is manual-only, or the role turned out to be
                non-US. Awareness only; one line each
  parked        open rows in the `parked` table, carried forward from earlier
                runs so nothing sits there unseen

Questions are *anticipated*, not observed. A form's real questions are not
known until a browser is in front of it, which is after the gate. So the batch
shows the two classes that can be predicted: the sensitive ones, which must be
confirmed every batch forever, and the ones this ATS has actually asked before.
Anything genuinely novel still surfaces at apply time through the HITL path.
"""

from __future__ import annotations

import logging
from pathlib import Path

from applypilot.review.batch import build_batch

log = logging.getLogger(__name__)

# Failure states worth one awareness line. A job that never scored well enough
# to be tailored is not a surprise and does not belong in his 15 minutes.
DISQUALIFYING_STATES = ("tailor_failed", "cover_failed", "manual_only")

# How many previously-seen questions to anticipate per ATS. The bank grows
# without bound; the gate must not.
MAX_ANTICIPATED_PER_ATS = 12


def _read_letter(path: str | None) -> str | None:
    """The cover letter's text, whatever `cover_letter_path` currently points at.

    The cover stage writes `.txt`, then the pdf stage converts it and rewrites
    the column to the `.pdf` or `.docx`. The source `.txt` stays on disk beside
    it, and it is the only form the gate can read — so fall back to the sibling
    rather than silently producing a batch with no cover deltas in it.
    """
    if not path:
        return None
    p = Path(path)
    if p.suffix.lower() != ".txt":
        p = p.with_suffix(".txt")
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError as e:
        log.warning("Could not read cover letter %s: %s", p, e)
        return None
    return text or None


def sensitive_questions(conn) -> list[str]:
    """The confirmations section 9 requires in every batch, forever.

    One per sensitive category, not one per stored phrasing. The bank holds
    five ways of asking about work authorisation and six about salary; asking
    him all eleven, for every application, is 187 keystrokes in a batch of 17
    and it is the same three answers each time. The category is the question.
    """
    rows = conn.execute(
        "SELECT category, canonical_text FROM questions WHERE sensitive = 1 "
        "ORDER BY category, times_seen DESC, confidence DESC, id")
    best: dict[str, str] = {}
    for row in rows:
        best.setdefault(row["category"], row["canonical_text"])
    return [best[category] for category in sorted(best)]


def anticipated_questions(conn, application_url: str | None) -> list[str]:
    """Questions this particular application will probably be asked.

    Whatever its ATS has asked before, most-seen first. Sensitive questions are
    deliberately absent: they are identical across every application in the
    batch, so they are raised once at batch level by `sensitive_questions`.
    """
    if not application_url:
        return []
    try:
        from applypilot.apply.chrome import detect_ats
        ats = detect_ats(application_url)
    except Exception:  # pragma: no cover - import guard only
        ats = None
    if not ats:
        return []

    questions: list[str] = []
    seen: set[str] = set()
    for row in conn.execute(
            "SELECT raw_text, COUNT(*) AS n FROM question_sightings "
            "WHERE ats = ? GROUP BY LOWER(raw_text) "
            "ORDER BY n DESC, MAX(seen_at) DESC LIMIT ?",
            (ats, MAX_ANTICIPATED_PER_ATS)):
        text = row["raw_text"]
        if text and text.lower() not in seen:
            seen.add(text.lower())
            questions.append(text)
    return questions


def _is_blocked_site(site: str | None, url: str | None) -> bool:
    """Whether the submit phase would refuse this row as a blocked employer.

    Matched case-insensitively: sites.yaml lists 'google', the scraper stores
    'Google', and a case-sensitive comparison let an explicitly blocked
    employer through into a review batch.
    """
    try:
        from applypilot.config import load_blocked_sites
        blocked, patterns = load_blocked_sites()
    except Exception:  # pragma: no cover - config guard only
        return False
    if site and site.lower() in {b.lower() for b in blocked}:
        return True
    lowered = (url or "").lower()
    return any(p.strip("%").lower() in lowered
               for p in patterns if p.strip("%"))


def _is_manual_ats(application_url: str | None) -> bool:
    """Whether the submit phase would refuse this URL as manual-only."""
    if not application_url:
        return True
    try:
        from applypilot.config import is_manual_ats
        return bool(is_manual_ats(application_url))
    except Exception:  # pragma: no cover - import guard only
        return False


def _prepared_rows(conn, min_score: int, max_age_days: int | None,
                   limit: int | None = None) -> list:
    """Rows with a tailored resume, an apply URL, and nothing submitted yet."""
    # A job parked for a human reason, or sitting in a terminal failure state,
    # is not ready however complete its paperwork looks. Both would otherwise
    # pass every column check below and be cleared for submission.
    states = ",".join("?" * len(DISQUALIFYING_STATES))
    params: list = [*DISQUALIFYING_STATES, min_score]

    age_filter = ""
    if max_age_days and max_age_days > 0:
        age_filter = "AND discovered_at > datetime('now', ?)"
        params.append(f"-{max_age_days} days")

    sql = f"""
        SELECT url, title, company, site, application_url, fit_score,
               cover_letter_path, tailored_resume_path
        FROM jobs
        WHERE tailored_resume_path IS NOT NULL
          AND application_url IS NOT NULL AND application_url != ''
          AND applied_at IS NULL
          AND (apply_status IS NULL OR apply_status = 'failed')
          AND (eligibility IS NULL OR eligibility = 'eligible')
          AND (state IS NULL OR state NOT IN ({states}))
          AND NOT EXISTS (
              SELECT 1 FROM parked p
              WHERE p.application_id = jobs.url AND p.resolved_at IS NULL
          )
          AND fit_score >= ?
          {age_filter}
        ORDER BY fit_score DESC, discovered_at DESC, url
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    return conn.execute(sql, params).fetchall()


def ready_applications(conn, min_score: int, max_age_days: int | None,
                       limit: int | None = None) -> list[dict]:
    """Applications prepared end to end and waiting on nothing but the gate.

    The readiness predicate mirrors `apply.launcher.acquire_job` deliberately:
    a batch that clears work the submit phase would not have picked up is a
    batch that lies about what is going out. That includes the manual-ATS skip
    list — `acquire_job` marks those `manual_only` on sight, so clearing one
    spends his attention on an application that can never be submitted.
    """
    return [{
        "url": row["url"],
        "title": row["title"],
        "company": row["company"] or row["site"],
        "cover_letter": _read_letter(row["cover_letter_path"]),
        "questions": anticipated_questions(conn, row["application_url"]),
        "status": "ready",
    } for row in _prepared_rows(conn, min_score, max_age_days, limit)
        if not _is_manual_ats(row["application_url"])
        and not _is_blocked_site(row["site"], row["url"])]


def manual_only_applications(conn, min_score: int,
                             max_age_days: int | None) -> list[dict]:
    """Prepared applications whose ATS the submit phase will not automate.

    Worth one awareness line each: the paperwork is written and he can send it
    by hand, but nothing in the pipeline will.
    """
    return [{
        "url": row["url"],
        "title": row["title"],
        "company": row["company"] or row["site"],
        "status": "disqualified",
        "reason": ("employer is on the blocked list"
                   if _is_blocked_site(row["site"], row["url"])
                   else "ATS requires a manual application"),
    } for row in _prepared_rows(conn, min_score, max_age_days)
        if _is_manual_ats(row["application_url"])
        or _is_blocked_site(row["site"], row["url"])]


def disqualified_applications(conn, min_score: int,
                              max_age_days: int | None) -> list[dict]:
    """Applications that got far enough to matter and then stopped."""
    age_filter, params = "", []
    if max_age_days and max_age_days > 0:
        age_filter = "AND discovered_at > datetime('now', ?)"
        params.append(f"-{max_age_days} days")

    placeholders = ",".join("?" * len(DISQUALIFYING_STATES))
    rows = conn.execute(f"""
        SELECT url, title, company, site, state, apply_error, eligibility
        FROM jobs
        WHERE applied_at IS NULL
          AND (state IN ({placeholders})
               OR (eligibility = 'non_us_only' AND fit_score >= ?))
          {age_filter}
        ORDER BY fit_score DESC, url
    """, (*DISQUALIFYING_STATES, min_score, *params))

    apps = []
    for row in rows:
        if row["eligibility"] == "non_us_only":
            reason = "non-US role"
        else:
            reason = row["apply_error"] or (row["state"] or "").replace("_", " ")
        apps.append({
            "url": row["url"],
            "title": row["title"],
            "company": row["company"] or row["site"],
            "status": "disqualified",
            "reason": reason,
        })
    return apps


def parked_applications(conn) -> list[dict]:
    """Open parked rows, joined back to whatever the job table still knows."""
    rows = conn.execute("""
        SELECT p.application_id, p.reason, j.title, j.company, j.site
        FROM parked p
        LEFT JOIN jobs j ON j.url = p.application_id
        WHERE p.resolved_at IS NULL
        ORDER BY p.parked_at DESC
    """)
    return [{
        "url": row["application_id"],
        "title": row["title"],
        "company": row["company"] or row["site"],
        "status": "parked",
        "reason": row["reason"],
    } for row in rows]


def collect_applications(conn, min_score: int, max_age_days: int | None,
                         limit: int | None = None) -> list[dict]:
    """Everything this run should put in front of him, ready ones first."""
    ready = ready_applications(conn, min_score, max_age_days, limit)
    known = {app["url"] for app in ready}

    extra = []
    for app in (manual_only_applications(conn, min_score, max_age_days)
                + disqualified_applications(conn, min_score, max_age_days)
                + parked_applications(conn)):
        if app["url"] and app["url"] not in known:
            known.add(app["url"])
            extra.append(app)

    return ready + extra


def build_run_batch(conn, min_score: int, max_age_days: int | None = None,
                    limit: int | None = None) -> dict:
    """Close a prepare run by assembling its review batch.

    Returns a summary rather than a bare id: the pipeline prints it, and a
    caller that got zero ready applications needs to know that without
    re-querying.
    """
    applications = collect_applications(conn, min_score, max_age_days, limit)
    counts = {"ready": 0, "disqualified": 0, "parked": 0}
    for app in applications:
        counts[app["status"]] = counts.get(app["status"], 0) + 1

    if not applications:
        log.info("Nothing prepared this run; no review batch built.")
        return {"batch_id": None, "counts": counts, "items": 0}

    batch_id = build_batch(conn, applications,
                           batch_questions=sensitive_questions(conn))
    items = conn.execute(
        "SELECT COUNT(*) FROM review_items WHERE batch_id = ?",
        (batch_id,)).fetchone()[0]
    return {"batch_id": batch_id, "counts": counts, "items": items}
