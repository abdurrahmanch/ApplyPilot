"""Deterministic form filling — ARCHITECTURE §5.

The apply agent used to reason about every field: one round trip each, 400–860
seconds per application. Most of those fields have answers that are already
known — a profile key, a file path, a stored question-bank answer. Reasoning
about them is waste.

This module turns "what goes in this form" into a lookup. Nothing here calls an
LLM. The split that makes it testable:

    plan()                pure. Map + context in, actions out. No browser.
    render_fill_script()  pure. Actions in, one JS blob out.
    (execution)           the agent runs that blob in a single tool call.

**Unverified maps are safe.** The generated script probes every selector before
it writes and reports what it actually touched. A selector that is wrong or
stale simply does not match, and its field comes back as unresolved for the
agent to handle — the same path an unmapped field takes. Nothing is filled
blind, so a map can ship before anyone has checked it against a live page.

Sources a field can draw from:

    profile.<dotted.path>   a value from profile.json
    file.resume             the tailored resume path for this job
    file.cover_letter       the cover letter path for this job
    question.<category>     the question bank's best answer for a category
    const.<literal>         a fixed string
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

MAPS_DIR = Path(__file__).parent / "selector_maps"

# A field that falls through to the agent this many times is a map bug, not a
# fact of life (ARCHITECTURE §5, rule 2).
FALLBACK_BUG_THRESHOLD = 2


def _fallback_ledger_path() -> Path:
    from applypilot import config
    return config.APP_DIR / "fill_fallbacks.json"


def available_maps() -> list[str]:
    return sorted(p.stem for p in MAPS_DIR.glob("*.yaml"))


def load_map(ats: str | None) -> dict | None:
    """Load one ATS selector map. Returns None when the family is unmapped."""
    if not ats:
        return None
    path = MAPS_DIR / f"{ats}.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        log.warning("Selector map %s is malformed: %s", path.name, e)
        return None
    if not isinstance(data.get("fields"), dict):
        log.warning("Selector map %s has no fields block", path.name)
        return None
    return data


def _dig(data: dict, dotted: str):
    """profile.personal.email → data['personal']['email'], or None."""
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _split_name(profile: dict, want: str) -> str | None:
    """Derive first/last name from `full_name` when they aren't stored apart.

    profile.json holds `full_name` and `preferred_name` but no split fields,
    and most ATSes want them separately. The surname is deliberately kept
    truncated wherever it appears, so this splits rather than expands.
    """
    full = (_dig(profile, "personal.full_name") or "").strip()
    if not full:
        return None
    parts = full.split()
    if len(parts) < 2:
        return full if want == "first_name" else None
    return parts[0] if want == "first_name" else " ".join(parts[1:])


def resolve(source: str, ctx: dict) -> str | None:
    """Turn one source expression into a value. Pure lookup, never a guess."""
    if not source or "." not in source:
        return None
    kind, _, rest = source.partition(".")

    if kind == "const":
        return rest

    if kind == "profile":
        profile = ctx.get("profile") or {}
        value = _dig(profile, rest)
        if value is None and rest in ("personal.first_name", "personal.last_name"):
            value = _split_name(profile, rest.split(".")[-1])
        if isinstance(value, (list, dict)):
            return None
        return None if value is None else str(value)

    if kind == "file":
        path = (ctx.get("files") or {}).get(rest)
        return str(path) if path else None

    if kind == "question":
        conn = ctx.get("conn")
        if conn is None:
            return None
        try:
            from applypilot.questions import _best_in_category
            match = _best_in_category(conn, rest)
        except Exception as e:  # pragma: no cover - defensive
            log.debug("Question lookup failed for %s: %s", rest, e)
            return None
        return (match or {}).get("canonical_answer")

    return None


def plan(ats: str | None, ctx: dict) -> dict:
    """Work out every field that can be filled without reasoning.

    Returns ``{ats, mapped, actions, unresolved, has_map}``. ``unresolved`` is
    the list of mapped field names whose value could not be resolved — those
    still need the agent, and each one is a gap in the profile or the question
    bank rather than a gap in the map.
    """
    spec = load_map(ats)
    if spec is None:
        return {"ats": ats, "mapped": 0, "actions": [], "unresolved": [],
                "has_map": False}

    actions, unresolved = [], []
    for name, field in (spec.get("fields") or {}).items():
        if not isinstance(field, dict) or not field.get("selector"):
            continue
        value = resolve(field.get("source", ""), ctx)
        if value is None or value == "":
            unresolved.append(name)
            continue
        actions.append({
            "field": name,
            "selector": field["selector"],
            "value": value,
            "kind": field.get("kind", "text"),
        })

    return {"ats": ats, "mapped": len(spec.get("fields") or {}),
            "actions": actions, "unresolved": unresolved,
            "has_map": True, "status": spec.get("status", "unverified")}


def render_fill_script(actions: list[dict]) -> str:
    """One JS blob that fills every resolved field and reports what it did.

    File inputs are deliberately skipped: a file cannot be attached by script
    without a real user gesture, so uploads stay with the agent's own upload
    tool. They are still listed in the result so the caller knows they are
    outstanding rather than done.
    """
    payload = json.dumps([a for a in actions if a["kind"] != "file"])
    files = json.dumps([{"field": a["field"], "selector": a["selector"],
                         "value": a["value"]}
                        for a in actions if a["kind"] == "file"])
    return f"""(() => {{
  const plan = {payload};
  const files = {files};
  const filled = [], missing = [];
  for (const step of plan) {{
    const el = document.querySelector(step.selector);
    if (!el) {{ missing.push(step.field); continue; }}
    const proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    // React and friends track value on the node; a bare assignment is
    // reverted on the next render. Go through the native setter and dispatch
    // the events the framework is listening for.
    if (setter) setter.call(el, step.value); else el.value = step.value;
    el.dispatchEvent(new Event('input', {{bubbles: true}}));
    el.dispatchEvent(new Event('change', {{bubbles: true}}));
    filled.push(step.field);
  }}
  const uploads = files.filter(f => document.querySelector(f.selector));
  return JSON.stringify({{filled, missing, uploads}});
}})()"""


def record_fallbacks(ats: str | None, fields: list[str]) -> dict:
    """Count how often each field fell through to the agent.

    A JSON file rather than a table: this is bookkeeping for whoever maintains
    the maps, it is never read in a hot path, and it does not deserve a
    migration.
    """
    if not ats or not fields:
        return {}
    path = _fallback_ledger_path()
    try:
        ledger = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        ledger = {}

    bucket = ledger.setdefault(ats, {})
    for field in fields:
        bucket[field] = bucket.get(field, 0) + 1

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as e:  # pragma: no cover - never fail an application over this
        log.warning("Could not write the fallback ledger: %s", e)
    return bucket


def map_bugs() -> dict[str, list[str]]:
    """Fields that have fallen through often enough to need a map entry."""
    path = _fallback_ledger_path()
    if not path.exists():
        return {}
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {ats: sorted(f for f, n in fields.items() if n >= FALLBACK_BUG_THRESHOLD)
            for ats, fields in ledger.items()
            if any(n >= FALLBACK_BUG_THRESHOLD for n in fields.values())}


def prompt_block(plan_result: dict) -> str:
    """The instruction the apply agent receives instead of reasoning per field.

    Empty string when there is no map, so an unmapped ATS falls straight
    through to the existing agent path — this is a fast lane added alongside
    the working system, not a replacement for it (ARCHITECTURE §5).
    """
    actions = plan_result.get("actions") or []
    if not plan_result.get("has_map") or not actions:
        return ""

    typed = [a for a in actions if a["kind"] != "file"]
    uploads = [a for a in actions if a["kind"] == "file"]

    lines = [
        "## DETERMINISTIC FILL — do this first, and do not reason about it",
        "",
        f"This is a known ATS ({plan_result['ats']}). The fields below have "
        "already been resolved. Fill them by running the script in ONE "
        "browser_evaluate call rather than field by field:",
        "",
        "```js",
        render_fill_script(actions),
        "```",
        "",
        "The script returns `{filled, missing, uploads}`.",
        "",
        "- `filled` — done. Do not revisit these fields.",
        "- `missing` — the selector did not match. Handle those yourself.",
        "- `uploads` — file inputs, which a script cannot set. Attach these "
        "with your upload tool:",
    ]
    for upload in uploads:
        lines.append(f"    - {upload['field']}: {upload['value']}")
    if not uploads:
        lines.append("    - (none)")

    if plan_result.get("unresolved"):
        lines += [
            "",
            "Mapped but with no stored answer — reason about only these: "
            + ", ".join(plan_result["unresolved"]),
        ]

    lines += [
        "",
        "Then handle whatever the form asks that is not covered above. Never "
        "re-enter a field the script reported as filled.",
    ]
    return "\n".join(lines)
