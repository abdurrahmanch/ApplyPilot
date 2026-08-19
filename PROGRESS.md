# PROGRESS.md

**Rewritten at the end of each working session** (ARCHITECTURE §8). Read this
first, then `ARCHITECTURE.md` for the design and `DECISIONS.md` for why.

**Last updated:** 2026-08-19, end of the architecture-v2 adoption session.

---

## Current state

**Runs today:** discover → enrich → score → tailor → cover → pdf → gate.
`applypilot run` ends at the gate. Approval is enforced in code
(`acquire_job(require_gate=True)`), not by convention. Submission is manual:
`applypilot apply` after the gate clears a batch.

**Tests: 531 passed, 3 skipped.**
```bash
.venv/bin/python -m pytest -q tests --ignore=tests/test_extension_server.py
```
`test_extension_server.py` segfaults in X11 code. Pre-existing, unrelated.

**Measured this session (this is the data §5 and §9 were missing):**

| ATS family | rows with an application_url | share |
|---|---|---|
| UNKNOWN (genuine long tail) | 62 | 26.7% |
| **workday** | **56** | **24.1%** |
| ashby | 17 | 7.3% |
| greenhouse | 15 | 6.5% |
| icims | 10 | 4.3% |
| paylocity | 9 | 3.9% |
| lever | 3 | 1.3% |

232 rows total. Greenhouse + Lever together are 7.8% — see D15.

**Not yet measured:** wall clock per application, rate-limit headroom, agent
fallback rate. `submission_proof` is empty, so the 400–860s figure in §5 comes
from earlier notes, not from this system. The filler's before/after comparison
cannot be run until an application goes out.

**Half-built:** the deterministic filler. Planning, script generation, the
fallback ledger, and the prompt block are done and tested (22 tests, no browser
needed). Three selector maps exist — workday, ashby, greenhouse — all
`status: unverified`. No map has been checked against a live page, which is
safe by construction (D16) but means the real fill rate is unknown.

---

## Next up

1. **Verify the Workday selector map against a live form.** Everything else in
   the filler is done. Open any Workday application in the discovery browser,
   run the generated script by hand, and record which of the eight selectors
   match. Then set `last_verified` and `verified_against` in
   `src/applypilot/apply/selector_maps/workday.yaml`. This is the single step
   that turns the filler from plumbing into a measured win.
2. **Map Paylocity.** Nine rows, and it is the ATS of the only successful
   submission this system has made, so a working path through it already
   exists. No map file yet.
3. **Gmail — resolve the OTP blocker.** ARCHITECTURE §6 chose browser-session
   scraping because the OAuth console setup kept stalling. That premise may be
   obsolete: `googleworkspace/cli` (skills.sh, 71K installs, official Google
   source) offers `gws auth login` — interactive browser OAuth with **no Google
   Cloud project setup**. Test it before building the scraper; a real API beats
   reading markup. If it works, §6 needs rewriting and a D19 recording the
   change.
4. **Finish M9.** systemd user timers (prepare Mon/Wed/Fri, which now cannot
   submit even by accident; OTP poller), plain-text run reports, per-stage
   JSONL logs in `logs/`.
5. **Discovery: read the rendered page** instead of the `_next/data` route
   (§3). Needs a live logged-in session to check whether the rendered page
   carries description text — if it does, enrich drops back to a fallback.
6. **hiring.cafe apply-marking click** (§3), keyed off `submission_proof`, never
   off a park or a failure.
7. **Fabricated-metric check** (§4, §13.6): extract every numeral from a cover
   letter and require it to appear in `resume_facts.real_metrics` or the job
   description. Pure Python, no LLM. Not started.
8. **Author the three skills** (§11) — `ats-form-fill`, `cover-letter-review`,
   `gate-triage`. Deferred until the filler maps are verified; a skill encoding
   a design that is mid-change is worse than none.

---

## Blocked

- **OTP / email tracking** — blocked on Gmail auth. Unblocked by either
  `gws auth login` (test this first, see Next up #3) or a logged-in browser
  profile. This gates the Workday-heavy queue, which is 24% of everything.
- **Filler before/after measurement** — blocked on a live application running.
  Nothing to measure until then.
- **Scoring calibration** (§4) — 7 of 124 above threshold. Blocked on a human
  reading a sample of 20 rejects. Do not tune the prompt on intuition.
- **Credential rotation** — needs the operator: the sudo password and the live
  third-party tokens in `~/.claude/projects` logs cannot be rotated from here.
  Log scrubbing is destructive and needs explicit go-ahead.

---

## Recently finished

- **Architecture v2 adopted.** `ARCHITECTURE.md` at the repo root, the standing
  rule in force, D14–D18 recorded.
- **Deterministic filler built** (§5): `apply/form_fill.py` plus three selector
  maps, wired additively into the apply prompt. An unmapped ATS produces an
  empty block and falls straight through to the existing agent path.
- **`detect_ats` gaps closed** (D17). UNKNOWN went from 101 rows (44%) to 62
  (27%) with eleven dict entries. This is what revealed that Workday, not
  Greenhouse, is the family worth mapping first.
- **§12 audit run** (report only, no changes): enrichment already tiers
  JSON-LD → deterministic selectors → LLM, so the suspected waste there does
  not exist. The real waste is the apply stage, which the filler now addresses.
- **Review gate wired end to end** (previous session): the gate is binding, not
  advisory. Three real defects found and fixed in the process.
