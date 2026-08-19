"""Review gate TUI.

Driven over SSH from a phone, so: one key per action, no mouse, no wide
tables, nothing that needs a scrollback buffer to make sense. Each item fills
one screen and asks one question.

Keys, uniform across item types:
    a  approve as shown
    e  edit the answer (or record a note on a letter)
    s  skip this application entirely
    p  park this application
    f  show the full text (cover letters)
    q  quit and leave the rest for later

Nothing is submitted from here. Approving the batch sets
`review_batches.status`, which is the only thing the submit phase looks at.
"""

from __future__ import annotations

import sqlite3

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from applypilot.review.batch import (
    apply_gate_correction,
    approve_batch,
    get_batch,
    get_pending_batch,
    record_cover_learning,
    resolve_item,
)

console = Console()

KIND_LABEL = {
    "high_value": "HIGH VALUE — one shot this season",
    "never_auto_guess": "SENSITIVE — confirm every time",
    "novel_question": "NEW QUESTION — no stored answer",
    "low_confidence": "LOW CONFIDENCE — confirm the match",
    "sameness_warning": "SAMENESS — two letters share a skeleton",
    "cover_delta": "COVER LETTER",
    "disqualified": "DISQUALIFIED",
    "parked": "PARKED",
}

KIND_STYLE = {
    "high_value": "bold magenta",
    "never_auto_guess": "bold red",
    "novel_question": "bold yellow",
    "low_confidence": "yellow",
    "sameness_warning": "bold cyan",
    "cover_delta": "green",
    "disqualified": "dim",
    "parked": "dim",
}

AWARENESS_ONLY = {"disqualified", "parked", "high_value"}


def _summary(batch: dict) -> Table:
    counts: dict[str, int] = {}
    for item in batch["items"]:
        if item["resolution"] is None:
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1

    table = Table(title=f"Batch {batch['id']} — {batch['status']}", show_header=True)
    table.add_column("Needs you")
    table.add_column("N", justify="right")
    for kind, label in KIND_LABEL.items():
        if counts.get(kind):
            table.add_row(f"[{KIND_STYLE.get(kind, '')}]{label}[/]", str(counts[kind]))
    if not counts:
        table.add_row("[green]nothing outstanding[/green]", "0")
    return table


def _render_question(payload: dict) -> Panel:
    lines = [
        f"[bold]{payload.get('question', '(no text)')}[/bold]",
        "",
        f"company:  {payload.get('company') or '?'}",
        f"role:     {payload.get('title') or '?'}",
        "",
        f"[bold]proposed:[/bold] {payload.get('proposed_answer') or '(none — needs an answer)'}",
        f"[dim]{payload.get('reason', '')}[/dim]",
    ]
    if payload.get("matched_canonical"):
        lines += ["",
                  f"[dim]matched: {payload['matched_canonical']}"
                  f"  (similarity {payload.get('similarity', 0):.2f})[/dim]"]
    return Panel("\n".join(lines), border_style="yellow")


def _render_cover(payload: dict) -> Panel:
    lines = [
        f"[bold]{payload.get('company') or '?'}[/bold] — {payload.get('title') or '?'}",
        "",
        f"[italic]{payload.get('opening', '')}[/italic]",
        "",
        f"hooks: {', '.join(payload.get('hooks') or []) or '(none found)'}",
        f"words: {payload.get('word_count', '?')}",
    ]
    repeats = payload.get("repeated_phrases") or []
    if repeats:
        lines.append(f"[yellow]repeated: {', '.join(repeats)}[/yellow]")
    return Panel("\n".join(lines), border_style="green")


def _render_sameness(payload: dict) -> Panel:
    return Panel(
        f"Shares a skeleton with:\n  {payload.get('similar_to')}\n\n"
        f"similarity {payload.get('similarity', 0):.2f}\n\n"
        f"[dim]{payload.get('note', '')}[/dim]",
        border_style="cyan")


def _render_awareness(kind: str, payload: dict) -> Panel:
    body = (f"{payload.get('company') or '?'} — {payload.get('title') or '?'}\n"
            f"[dim]{payload.get('reason') or payload.get('note') or ''}[/dim]")
    return Panel(body, border_style=KIND_STYLE.get(kind, "dim"))


def _render(item: dict) -> Panel:
    kind, payload = item["kind"], item["payload"]
    if kind in ("novel_question", "low_confidence", "never_auto_guess"):
        return _render_question(payload)
    if kind == "cover_delta":
        return _render_cover(payload)
    if kind == "sameness_warning":
        return _render_sameness(payload)
    return _render_awareness(kind, payload)


def _keys_for(kind: str) -> str:
    if kind == "cover_delta":
        return "[a]pprove [e]dit-note [f]ull [s]kip [p]ark [q]uit"
    if kind in AWARENESS_ONLY:
        return "[a]cknowledge [q]uit"
    return "[a]pprove [e]dit [s]kip [p]ark [q]uit"


def review_batch(conn: sqlite3.Connection, batch_id: int | None = None) -> dict:
    """Walk one batch item by item. Returns a summary of what happened."""
    batch = get_batch(conn, batch_id) if batch_id else get_pending_batch(conn)
    if batch is None:
        console.print("[yellow]No pending review batch.[/yellow]")
        return {"status": "none"}

    console.print(_summary(batch))
    console.print()

    pending = [i for i in batch["items"] if i["resolution"] is None]
    skipped_apps: set[str] = set()
    parked_apps: set[str] = set()
    counts = {"approved": 0, "edited": 0, "skipped": 0, "parked": 0}

    for index, item in enumerate(pending, 1):
        app_id = item["application_id"]
        if app_id and (app_id in skipped_apps or app_id in parked_apps):
            resolve_item(conn, item["id"], "application skipped")
            continue

        kind = item["kind"]
        console.rule(f"[{KIND_STYLE.get(kind, '')}]{KIND_LABEL.get(kind, kind)}[/] "
                     f"({index}/{len(pending)})")
        console.print(_render(item))

        while True:
            choice = Prompt.ask(_keys_for(kind), default="a").strip().lower()[:1]

            if choice == "q":
                console.print("[dim]Stopped. Nothing submitted; the rest stay pending.[/dim]")
                return {"status": "interrupted", **counts}

            if choice == "f" and kind == "cover_delta":
                console.print(Panel(item["payload"].get("full_text", ""),
                                    border_style="green"))
                continue

            if choice == "a":
                resolve_item(conn, item["id"], "approved")
                counts["approved"] += 1
                break

            if choice == "e":
                if kind == "cover_delta":
                    note = Prompt.ask("what should change, as a general rule")
                    if note:
                        record_cover_learning(conn, item["id"], note)
                        counts["edited"] += 1
                        break
                    continue
                answer = Prompt.ask("correct answer")
                if answer:
                    apply_gate_correction(conn, item["id"], answer)
                    counts["edited"] += 1
                    break
                continue

            if choice == "s":
                resolve_item(conn, item["id"], "application skipped")
                if app_id:
                    skipped_apps.add(app_id)
                counts["skipped"] += 1
                break

            if choice == "p":
                reason = Prompt.ask("park reason", default="needs a human")
                resolve_item(conn, item["id"], f"parked: {reason}")
                if app_id:
                    parked_apps.add(app_id)
                    conn.execute(
                        "INSERT INTO parked (application_id, reason, parked_at) "
                        "VALUES (?, ?, datetime('now'))", (app_id, reason))
                    conn.commit()
                counts["parked"] += 1
                break

    result = approve_batch(conn, batch["id"], allow_partial=True)
    console.print()
    console.print(f"[bold green]Batch {result['batch_id']} {result['status']}[/bold green]")
    console.print(f"  {len(result['submittable'])} application(s) cleared to submit")
    if result["held_back"]:
        console.print(f"  [yellow]{len(result['held_back'])} held back[/yellow]")
    console.print(f"  [dim]{counts['approved']} approved, {counts['edited']} edited, "
                  f"{counts['skipped']} skipped, {counts['parked']} parked[/dim]")
    console.print("\n[dim]Run `applypilot apply` to submit the approved batch.[/dim]")
    return {"status": result["status"], **counts,
            "submittable": result["submittable"], "held_back": result["held_back"]}
