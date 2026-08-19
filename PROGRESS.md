# PROGRESS.md — session handoff

**Last updated:** 2026-08-19, end of second build session.
**Read this with `CLAUDE.md` (architecture) and `DECISIONS.md` (why things are
the way they are).** Where this file and CLAUDE.md disagree about how the world
works, this file is newer — CLAUDE.md was written before the code was read.

---

## Where things stand

| Milestone | State | Notes |
|---|---|---|
| M0 fork + verify | **done** | All six section-3 VERIFY items answered in DECISIONS.md |
| M1 Claude consolidation | **done** | `claude_cli` provider in `llm.py`; no API key anywhere |
| M2 hiring.cafe discovery | **done** | 268 real jobs landed, 219 companies |
| M3 scoring patch | **done** | Prompt retargeted, batched, recency ordering |
| M4 schema + question bank | **done** | 6 tables, similarity matching, 25 seeded answers |
| M5 register + letters | **done** | Voice register written and wired, sameness check |
| M6 review gate | **done** | `applypilot review`, full escalation logic |
| M7 apply hardening | **done** | Pacing, parking, proof, per-ATS cooldowns |
| M8 Gmail + OTP | **code done, BLOCKED on auth** | See blockers |
| M9 timers + reports | **in progress** | Gate wired end to end; timers + reports next |

**Tests: 508 passed, 3 skipped.** Run with:
```bash
.venv/bin/python -m pytest -q tests --ignore=tests/test_extension_server.py
```
`test_extension_server.py` segfaults in X11 code (`chrome.py:_raise_x11_window`).
Pre-existing, unrelated to any of this work, hence the ignore.

**One real application was submitted** on 2026-08-19: BisectHosting, Web
Developer, via Paylocity. Confirmed "Application Successful".

---

## Done this session — the gate is now binding

The two wire-ups that made the gate advisory are closed. See DECISIONS.md
D9-D13 for why each is shaped the way it is.

- **`gate` is a pipeline stage** (`STAGE_ORDER[-1]`, downstream of `pdf`). It
  calls `review.prepare.build_run_batch` and never submits, so
  `applypilot run` now ends at the human touchpoint instead of past it.
  `applypilot run gate` runs it alone.
- **`acquire_job(require_gate=True)` is the default.** Nothing cleared means
  nothing acquired, and `apply` refuses before launching a browser. Bypasses:
  `--no-gate` (labelled in the banner) and an explicit `--url`.
- **`review/prepare.py`** is the new seam between the pipeline and the gate:
  ready / disqualified / parked, with the readiness predicate mirroring
  `acquire_job` — manual-ATS URLs, open `parked` rows, and terminal failure
  states are all diverted to awareness lines rather than cleared.
- **`migrations/003_review_membership.sql`** records batch membership
  explicitly. Without it a flawless application (no questions escalated, no
  cover delta) raised zero `review_items` and could never be cleared.
- **Sensitive questions are batch-scoped, one per category.** Filed with
  `application_id = NULL`; an unresolved one blocks the whole batch. This took
  the first live batch from 12 items to 4.
- **23 new tests** in `tests/test_gate_wiring.py`.

Live check on his DB: the one prepared job (IMEG, Workday) is on the
manual-ATS skip list, so the queue is honestly empty — `acquire_job` would
have refused it anyway. Batch 2 exists and is pending; it holds three
sensitive confirmations and one cover delta. Nothing has been approved.

## Do this next (rest of M9)

1. **systemd user timers.**
   - `jobpipe-prepare.timer` — Mon/Wed/Fri early morning. Runs
     `applypilot run` (which now ends at `gate`) and exits. It must NEVER
     submit; that property is now enforced in code, not just by convention.
   - `jobpipe-otp-poller.timer` — every 60s during an apply phase, every 15min
     otherwise (see `applypilot otp --interval`).
   - Submission stays manual: `applypilot apply` after gate approval.
2. **Run reports.** Plain text per run: discovered N, deduped N, gated N,
   prepared N, review items by kind; after approval, submitted N, parked N with
   reasons, per-ATS outcomes. `apply/pacing.ats_health()` already computes the
   per-ATS part, and `review.prepare.build_run_batch` already returns the
   prepared/gated counts.
3. **JSONL logs per stage** in `logs/`. The run report is the summary; the
   JSONL is the truth.
4. **Anticipated questions are only as good as `question_sightings`.** That
   table is still nearly empty, so per-application questions almost never
   surface at the gate and novel ones land at apply time via HITL instead.
   This corrects itself as real applications run; do not "fix" it by widening
   what the gate guesses.

---

## Blockers for Abdur-Rahman

1. **Gmail OAuth is not set up.** `applypilot otp --once` fails with
   "Gmail MCP session failed". M8 code is written and tested but cannot run
   until this exists. Needs:
   - a dedicated job-search Gmail account with a password unique to this system
   - `npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth` run once, which
     writes `~/.gmail-mcp/credentials.json`
   - Google Cloud OAuth desktop credentials saved to
     `~/.gmail-mcp/gcp-oauth.keys.json`
   He offered to authenticate in a browser rather than hand over credentials,
   so drive this the same way the hiring.cafe login was done.
2. **hiring.cafe session expires.** Re-auth with
   `.venv/bin/python scripts/hc_login.py` when discovery reports a session
   error. Profile lives at `~/.applypilot/hiringcafe-profile`.
3. **Anasheed conflict was resolved but the resume still overstates it.** He
   confirmed only the database schema exists. `~/.applypilot/resume.txt` was
   trimmed to three design-verb bullets; the master resume he pasted still has
   six. Ask before touching the master.
4. **Open questions he has not answered:**
   - Verbatim resume bullets from the "Test" three-resume chat
   - LinkedIn headline / About copy
   - Whether the three public links (github, linkedin, duhamedia.com) render
     publicly
   - Whether to trim the master resume's Anasheed section to match
5. **Cost:** ~$1.69 per application at the apply stage. 17/run is ~$29/run,
   ~$87/week. He has not been asked to approve that rate.
6. **Secrets exposure (raised, not yet acted on):** `~/.claude/projects` logs
   contain his sudo password and live Shopify tokens in plaintext. Rotation
   recommended.

---

## Things that surprised us (do not re-derive these)

- **hiring.cafe's documented API is gone.** `POST /api/search-jobs` 405s. Real
  endpoint is `GET /_next/data/{buildId}/index.json?searchState=...&page=N`
  with header `x-nextjs-data: 1`, from inside a logged-in browser. `buildId`
  changes on every deploy, so it is scraped per run.
- **hiring.cafe returns no job descriptions.** CLAUDE.md says enrich is a no-op
  for these rows. It is not — enrich is required and scrapes `apply_url`.
- **The searchState schema in CLAUDE.md §5 is wrong on four points** (lat/lon
  are floats not strings, no `flexible_regions`, `workplace_types` is
  per-location, the field is `jobTitleQuery` not `searchQuery`).
- **The inherited scoring prompt described a different person** — Seattle,
  senior, Go/Kotlin. Rewritten around profile.json.
- **The inherited cover-letter target (250-400 words) produced padding.**
  Dropped to 150-200 per section 7.
- **A dry run polluted the Q&A bank**, and the next real submission replayed a
  fabricated answer from it. Dry runs no longer write to the bank.
- **The ATS mix is harder than section 11 assumes.** Greenhouse/Lever/Ashby are
  a minority; Workday, Oracle Cloud, SuccessFactors, iCIMS, Taleo and BrassRing
  dominate. This is why M8 matters more than the build order implies.

---

## Operating notes

- Everything runs through the Claude Code CLI. No `ANTHROPIC_API_KEY` exists,
  and `config.get_tier()` treats the `claude` binary as the LLM provider.
- Scoring is batched at 10 jobs/call. Unbatched it costs ~$25/month against a
  $10 ceiling; batched it is ~$9/month. Do not un-batch it.
- Address him as **Abdur-Rahman**, never "Arch" (CLAUDE.md uses the old name
  throughout). His surname is written **"Ch"** and never expanded.
- **Keep replies short.** He asked for this directly.
- Key paths: `~/.applypilot/` (profile.json mode 600, resume.txt,
  candidate-dossier.md, applypilot.db), `~/Documents/Repositories/voice-profile/`
  (read-only core + registers).
