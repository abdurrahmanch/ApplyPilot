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

**Tests: 559 passed, 3 skipped.**
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

## The dependency that gates everything — first link now built

`myworkdayjobs.com` is on the manual-ATS skip list (`config/sites.yaml:11`,
"requires account login, can't automate"). Workday is 24% of the queue, so a
quarter of all discovered work is excluded from auto-apply before anything else
is considered. The chain:

```
Gmail auth  →  OTP retrieval  →  Workday account creation succeeds
            →  Workday comes off the manual-ATS list
            →  the Workday selector map starts paying off
            →  24% of the queue becomes applyable
```

**Gmail auth and OTP retrieval are now done** (D23). The operator signed in;
the session verifies headlessly; `applypilot otp --once` reads from it. What
remains on this chain is proving that a Workday account creation actually
completes with a retrieved code, and only then taking Workday off the skip
list. Do not remove it from `sites.yaml` before that is demonstrated on one
real application.

Auto-applyable **today**: Ashby, Greenhouse, Lever, Jobvite, Oracle and the
long tail — no account required on most. Four above-threshold jobs sit there
now, untailored.

## Next up

1. **Verify the Workday selector map against a live form.** Everything else in
   the filler is built and tested. Open any Workday application in the
   discovery browser, run the generated script by hand, record which of the
   eight selectors match, then set `last_verified` and `verified_against` in
   `src/applypilot/apply/selector_maps/workday.yaml`. This is the one step that
   turns the filler from plumbing into a measured win, and it unblocks the
   `ats-form-fill` skill.
2. **Map Paylocity.** Nine rows, and it is the ATS of the only successful
   submission this system has made. No map exists: unlike Workday's
   `data-automation-id` convention or Greenhouse's long-stable field ids, I have
   no real knowledge of Paylocity's markup, and inventing selectors would add
   noise rather than coverage. Needs one look at a live form.
3. **Prove one Workday account creation end to end**, using a retrieved OTP.
   Only after that succeeds, remove `myworkdayjobs.com` from `manual_ats` in
   `config/sites.yaml`. That single line is what gates 24% of the queue.
4. **Two live cover letters currently fail validation.** Both predate the
   150–200 word target so the length errors are expected, but one also claims
   the employer has "100+ offices nationwide" — a number that appears nowhere
   in that job description. Regenerate both:
   `applypilot run cover` after clearing `cover_letter_path` for those rows.
5. **Discovery: read the rendered page** instead of the `_next/data` route
   (§3). Needs a live logged-in session to check whether the rendered page
   carries description text — if it does, enrich drops back to a fallback.
6. **hiring.cafe apply-marking click** (§3), keyed off `submission_proof`,
   never off a park or a failure.
7. **Scoring calibration** (§4): read 20 of the 117 sub-threshold jobs and
   decide whether the scorer is right. Do not tune the prompt on intuition.
8. **Dual state truth** (§13.2): migrate reads off `apply_status` /
   `apply_category` / `tracking_status` onto `jobs.state`, then writes, then
   drop. Worth doing before 50/week.

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

- **Gmail unblocked** (D23, D24). `scripts/gmail_login.py` establishes the
  session; `tracking/gmail_browser.py` reads it. Read-only. Both the OTP poller
  and `run_tracking` prefer it and fall back to MCP. Verified on live mail: a
  tracking dry run fetched 20 emails and matched a real confirmation to the one
  submitted application.

- **M9 complete.** `reports.py` — per-stage JSONL (`logs/<stage>.jsonl`) plus
  `applypilot report`, written automatically at the end of every run. systemd
  units in `deploy/systemd/`, validated with `systemd-analyze verify`, **not
  enabled** (D21). The apply stage now stamps `fill_path` on every attempt,
  which is what makes the filler measurable.
- **Fabricated-quantity check** (D19). Closed the known gap in §4. Caught a real
  invented claim on its first run against live output.
- **Two skills authored** (D22): `cover-letter-review` (wraps the pipeline's own
  validator, so its verdict cannot drift) and `gate-triage` (taxonomy,
  thresholds, five invariants). `ats-form-fill` deliberately deferred until the
  maps are verified.
- **Deterministic filler built** (§5, D15–D18): `apply/form_fill.py` plus three
  selector maps, wired additively into the apply prompt. Unmapped ATS families
  fall straight through to the existing agent path.
- **`detect_ats` gaps closed** (D17). UNKNOWN 101 rows (44%) → 62 (27%), which
  is what revealed Workday rather than Greenhouse as the family worth mapping
  first.
- **Architecture v2 adopted** (D14) with the standing rule in force.
- **Review gate wired end to end** (previous session): binding, not advisory.
