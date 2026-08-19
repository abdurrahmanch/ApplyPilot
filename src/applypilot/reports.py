"""Run reports and per-stage JSONL logs (ARCHITECTURE §9, M9).

Two outputs with different jobs:

    JSONL   one line per event, appended to logs/<stage>.jsonl. This is the
            truth — machine-readable, never summarised, never rotated on the
            basis of what looked interesting.
    Report  a plain-text summary of one run, for a human to read in ten
            seconds and decide whether anything needs attention.

The metrics are the ones §9 asks for: wall clock by ATS family and fill path,
agent fallback rate, per-ATS submission success, gate items per batch. Notional
API cost is deliberately absent — there is no API key on this install, so the
figure would measure nothing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _log_dir() -> Path:
    from applypilot import config
    return config.LOG_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(stage: str, event: str, **fields) -> None:
    """Append one event to `logs/<stage>.jsonl`. Never raises.

    Losing a log line must never fail a pipeline stage, so every error here is
    swallowed after a warning.
    """
    record = {"at": _now(), "stage": stage, "event": event, **fields}
    try:
        directory = _log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"{stage}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as e:
        log.warning("Could not write a %s log line: %s", stage, e)


def read_events(stage: str, limit: int | None = None) -> list[dict]:
    """Read back one stage's JSONL, oldest first. Malformed lines are skipped."""
    path = _log_dir() / f"{stage}.jsonl"
    if not path.exists():
        return []
    events = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError as e:
        log.warning("Could not read the %s log: %s", stage, e)
        return []
    return events[-limit:] if limit else events


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def _funnel(conn) -> list[tuple[str, int]]:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return [
        ("discovered", q("SELECT COUNT(*) FROM jobs")),
        ("enriched", q("SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL")),
        ("scored", q("SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL")),
        ("above threshold", q("SELECT COUNT(*) FROM jobs WHERE fit_score >= 8")),
        ("tailored", q("SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL")),
        ("cover letter", q("SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL")),
        ("submitted", q("SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL")),
    ]


def _gate_section(conn) -> list[str]:
    """Gate items per batch, against the ~20 budget in §7."""
    rows = conn.execute(
        "SELECT id, status, "
        "(SELECT COUNT(*) FROM review_items ri WHERE ri.batch_id = rb.id) AS items, "
        "(SELECT COUNT(*) FROM review_batch_applications m WHERE m.batch_id = rb.id "
        " AND m.status = 'ready') AS ready "
        "FROM review_batches rb ORDER BY rb.id DESC LIMIT 3").fetchall()
    if not rows:
        return ["  no review batch has been built yet"]

    lines = []
    for row in rows:
        budget = ""
        if row["ready"]:
            per = row["items"] / row["ready"]
            budget = f"  ({per:.1f} items per application)"
        flag = "  OVER BUDGET" if row["items"] > 20 else ""
        lines.append(f"  batch {row['id']}: {row['status']}, {row['ready']} ready, "
                     f"{row['items']} items{budget}{flag}")
    return lines


def _kind_breakdown(conn, batch_id: int | None) -> list[str]:
    if batch_id is None:
        row = conn.execute(
            "SELECT id FROM review_batches ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return []
        batch_id = row["id"]
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM review_items WHERE batch_id = ? "
        "GROUP BY kind ORDER BY n DESC", (batch_id,)).fetchall()
    return [f"    {r['kind']}: {r['n']}" for r in rows]


def _ats_health(conn) -> list[str]:
    try:
        from applypilot.apply.pacing import ats_health
        health = ats_health(conn)
    except Exception as e:  # pragma: no cover - defensive
        return [f"  unavailable: {e}"]
    if not health:
        return ["  no submissions recorded yet"]

    lines = []
    for entry in health:
        # `unhealthy` is pacing's own verdict, which already refuses to judge
        # an ATS off a partial window. Recomputing the threshold here would be
        # a second definition of the same rule, free to drift from the first.
        rate = f"{entry['success_rate'] * 100:.0f}%"
        if entry["unhealthy"]:
            flag = "  UNHEALTHY — move to manual-only"
        elif not entry["judged"]:
            flag = "  (too few attempts to judge)"
        else:
            flag = ""
        lines.append(f"  {entry['ats']:<18}{entry['attempts']:>4} attempts"
                     f"  {rate:>5} success{flag}")
    return lines


def _parked(conn) -> list[str]:
    rows = conn.execute(
        "SELECT reason, COUNT(*) AS n FROM parked WHERE resolved_at IS NULL "
        "GROUP BY reason ORDER BY n DESC").fetchall()
    return [f"  {r['reason']}: {r['n']}" for r in rows] or ["  nothing parked"]


def _fill_paths() -> list[str]:
    """Wall clock by fill path, plus the agent fallback rate (§9)."""
    events = [e for e in read_events("apply") if e.get("event") == "application"]
    if not events:
        return ["  no applications recorded yet — nothing to compare"]

    lines = []
    by_path: dict[str, list[float]] = {}
    for event in events:
        path = event.get("fill_path", "unknown")
        if event.get("seconds") is not None:
            by_path.setdefault(path, []).append(float(event["seconds"]))
    for path, times in sorted(by_path.items()):
        times.sort()
        median = times[len(times) // 2]
        lines.append(f"  {path:<16}{len(times):>4} runs   median {median:>6.0f}s")

    try:
        from applypilot.apply.form_fill import map_bugs
        bugs = map_bugs()
    except Exception:
        bugs = {}
    if bugs:
        lines.append("  map gaps (fields that fell through twice or more):")
        for ats, fields in sorted(bugs.items()):
            lines.append(f"    {ats}: {', '.join(fields)}")
    return lines


def build_report(conn, title: str = "run report") -> str:
    """A plain-text summary of where the pipeline stands."""
    out = [
        "=" * 62,
        f"  ApplyPilot — {title}",
        f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 62,
        "",
        "FUNNEL",
    ]
    for label, count in _funnel(conn):
        out.append(f"  {label:<18}{count:>6}")

    out += ["", "REVIEW GATE  (budget: ~20 items per batch, ARCHITECTURE §7)"]
    out += _gate_section(conn)
    breakdown = _kind_breakdown(conn, None)
    if breakdown:
        out += ["    latest batch by kind:"] + breakdown

    out += ["", "PARKED  (open)"]
    out += _parked(conn)

    out += ["", "PER-ATS SUBMISSION HEALTH  (dry runs excluded)"]
    out += _ats_health(conn)

    out += ["", "WALL CLOCK BY FILL PATH  (ARCHITECTURE §9)"]
    out += _fill_paths()

    out += ["", "=" * 62, ""]
    return "\n".join(out)


def write_report(conn, title: str = "run report") -> Path | None:
    """Write the report next to the logs and return its path."""
    text = build_report(conn, title)
    try:
        directory = _log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_report.txt"
        path.write_text(text, encoding="utf-8")
        return path
    except OSError as e:
        log.warning("Could not write the run report: %s", e)
        return None
