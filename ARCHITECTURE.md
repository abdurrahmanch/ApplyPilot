# ApplyPilot — Architecture v2

Supersedes the original `CLAUDE.md` where they conflict. This document is the
living design: **if you change the system, you edit this file in the same commit
as the code.** A change that isn't reflected here didn't happen.

Snapshot: 2026-08-19. Written after the M0–M8 build and the wiring session that
connected the review gate.

---

## 0. Governing principles

These are the tie-breakers. When a design question is genuinely close, these
decide it.

1. **Deterministic first, reasoning only on the unknown.** If the answer is
   already known — a selector, a stored answer, a profile field — no LLM call
   happens. An LLM call is an admission that something is genuinely novel.
   Every reasoning step in a hot path is a bug until proven otherwise.
2. **Don't rebuild what exists.** Reuse the fork's stage architecture, the
   existing browser session, the existing question bank. New code is a last
   resort, not a first move.
3. **Don't optimize before measuring.** Parallel workers, embedding models, and
   Postgres are all deferred until a measurement says they're needed.
4. **The human's attention is the scarce resource.** 17 applications cleared in
   under 15 minutes. Anything that adds items to the gate must justify itself
   against that budget.
5. **Honest positioning over volume.** Never fabricate. Where a claim can't be
   supported from `resume_facts`, park it rather than pad it.

---

## 1. What changed from the original plan, and why

Reality pushed back in seven places. These are settled — do not re-derive them.

| Plan | Reality | Resolution |
|---|---|---|
| hiring.cafe JSON API, plain HTTPS from residential IP | Documented API gone (405/404). Real route is the Next.js page-data endpoint, needs a logged-in session, `buildId` rotates per deploy | Discovery runs inside a persistent logged-in Chromium profile. **New: read the rendered results page rather than the data route** — drops the buildId dependency entirely (§3) |
| Descriptions inline, enrich optional | No descriptions returned by either route | Enrich is a **mandatory** stage for this source |
| `searchState` schema as documented | Wrong on four points: lat/lon are floats not strings; no `flexible_regions`; `workplace_types` is per-location; field is `jobTitleQuery` not `searchQuery` | Blobs committed verbatim from the live UI; schema doc corrected |
| Greenhouse/Lever/Ashby dominate | Workday, Oracle Cloud, SuccessFactors, iCIMS, Taleo, BrassRing dominate | OTP/email verification moved from nice-to-have to critical path (§6) |
| Gmail API + OAuth for tracking/OTP | OAuth setup never completed; blocked the whole queue for weeks | **Browser-session Gmail** (§6). OAuth is the eventual upgrade, not the prerequisite |
| Agent fills every form field | 400–860 seconds per application; 8,365 lines of apply code vs 1,053 of review | **Deterministic selector-map fillers** with agent fallback (§5). This is the single highest-leverage change in v2 |
| Notional API cost as the budget metric | No API key exists; everything runs on Claude Max via the `claude` CLI | Track **rate-limit headroom and wall-clock**, not dollars (§9) |

### Still true from the original design

Batch prepare → single review gate → unattended submit. Park-and-continue,
never stall. Cover letter on every application where the field exists.
Correction-driven learning, not outcome-driven. No CAPTCHA-solving services.
No LinkedIn automation. Headed browser, residential IP, no datacenter proxies.

---

## 2. Pipeline overview

```
discover → enrich → score → tailor → cover → pdf → gate
                                                     ↓
                                              [human approval]
                                                     ↓
                                            apply → track
```

Stages communicate only through SQLite (conveyor-belt pattern — preserve it).
`applypilot run` ends at `gate` by default. Approval is the only thing that
permits submission; this is enforced in code (`acquire_job(require_gate=True)`),
not by convention.

---

## 3. Discovery — simplify to the rendered page

**Current:** persistent logged-in Chromium profile → scrape `buildId` → call
`/_next/data/{buildId}/...` → paginate on `ssrIsLastPage`.

**Change:** read the rendered results page in the same session instead. This
removes the per-run `buildId` scrape and the coupling to an internal route that
can change on any deploy. Investigate whether the rendered page carries
description text — if it does, enrich shrinks to a fallback again.

Keep: the two committed `searchState` blobs (remote-USA, hybrid-Chicago) as
versioned repo artifacts. Keep: dedupe by canonical application URL plus
company/title fuzzy match. Keep: the twelve dormant scrapers, importable and
individually runnable, not deleted.

**Session maintenance:** discovery cookies expire. A scripted browser login
refreshes them. If discovery returns zero rows, check session validity before
anything else.

**New — apply-marking loop.** After a *confirmed* submission (keyed off the
`submission_proof` row, never off a park or a failure), click the corresponding
"applied" marker in the authenticated hiring.cafe session. Log the click result;
a silent UI failure there must not quietly desync the counts. This gives an
independent cross-check on submission totals.

**Query tuning:** monthly, manually invoked, proposes diffs only. Never edits
queries autonomously.

---

## 4. Scoring, tailoring, cover letters

**Scoring.** Batched at 10 jobs per call — this is load-bearing and measured
(~4× reduction; each `claude -p` re-sends ~26k tokens of system prompt).
**Do not un-batch.** Descriptions truncated to 1,800 chars. Keyword pre-filter
shortcuts obviously irrelevant postings with no LLM call at all. Ordering is
`COALESCE(posted_at, discovered_at) DESC`, `fit_score` as tiebreaker, with a
per-company round-robin window.

**The floor is 4, not 8** (D25). The two curated hiring.cafe blobs carry 134
quoted job titles and are the real keyword filter; the scorer's remaining job is
to strip what a query cannot see — seniority, clearance, non-US — not to
re-decide relevance. At floor 8 only 3 rows were auto-applyable; at floor 4, 18
are, and what sits below is Senior/Staff/Lead titles. Pass `--min-score 4`; the
shipped default stays 8 so upstream behaviour is untouched.

**Open calibration question, still open.** The floor is not the calibration.
80 of 124 rows scored 1–2, which looks right by title but has not been read.
Resolve empirically — sample 20 rejects, read them, decide. Don't tune the
prompt on intuition.

**Tailor.** Full description (this is where truncation-missed disqualifiers
surface). Validates against the `resume_facts` allowlist.

**Cover letters.** 150–200 words. `display_company()` resolver so a job board is
never pitched as the employer. Hard refusal to ship an invalid letter — rejected
drafts go to a `_rejected.txt` sidecar and the job parks as `cover_failed`.
Stem-based error-tier regexes, structural paragraph check, batch-level sameness
check.

**Fabricated quantities — closed.** `validator.find_unsupported_numbers`
extracts every quantity from the letter and from the allowed sources (resume,
job description, `resume_facts.real_metrics`), and compares them at the same
scale, so "12B" is not satisfied by an unrelated "12". Error tier, forcing
regeneration. Pure Python, no LLM.

The tuning that matters is what it *doesn't* fire on: years, and bare integers
under three digits. A check that flags "2026" or "three teams" forces endless
regeneration and gets switched off. Verified on real output — it caught a
letter claiming the employer had "100+ offices nationwide", a number that
appears nowhere in that job description.

---

## 5. The form filler — deterministic first ★

**This is the centrepiece of v2.** Currently an agent reasons about every field:
one LLM round trip per field, 400–860 seconds per application. Most of those
fields are questions whose answers are already known.

### Design

**Per-ATS selector maps.** One file per family — Greenhouse, Lever, Ashby,
Workday, Oracle Cloud, SuccessFactors, iCIMS, Taleo, BrassRing. Each maps a
field to a CSS selector plus the source that feeds it (a `profile.json` key or a
question-bank category). Every map carries a `last_verified` date; these rot.

**Fill pass — no LLM anywhere.** Straight DOM writes for the known set: name,
email, phone, address, LinkedIn, GitHub, resume upload, cover letter upload,
work authorization, sponsorship, salary expectation, start date, relocation,
degree, years of experience, background check consent, EEO defaults. Target:
single-digit seconds.

**Coverage check.** After filling, walk every required field. Anything still
empty or unmapped goes on a list. **If the list is empty, nothing reasons at
all** — that's the common case and the whole point.

**Agent fallback, narrowly scoped.** The agent sees only the unresolved fields,
not the page. One call per unknown. The answer is written back to the question
bank so the same field is deterministic next time.

### Two rules that keep the bloat from growing back

1. The agent never touches a field the map already covers.
2. A map miss is a bug to fix in the map, not something the agent papers over
   every run. Log every fallback invocation; a field that triggers fallback
   twice gets a map entry.

### Rollout

Start with the **top two ATS families by actual queue volume**. Measure before
and after. Extend one family at a time. Keep the current agent path intact as
the fallback for unmapped families — this is a fast path added alongside, not a
replacement that risks the working system.

### Expected shape of the win

Greenhouse/Lever/Ashby near-fully deterministic. Workday partially — it's
multi-page and per-tenant, so expect map + agent hybrid. Seventeen applications
in minutes rather than hours.

**Corollary: parallel workers are deferred.** Once filling takes seconds,
concurrency optimizes a solved problem. Revisit only if measurement says
otherwise — and cap at 2–3 staggered workers when you do, because simultaneous
submissions from one residential IP is exactly the burst signature anti-bot
systems look for.

---

## 6. Email, OTP, and the Chrome extension question

**Gmail via browser session, not OAuth. Built and working** —
`tracking/gmail_browser.py`, profile at `~/.applypilot/gmail-profile`,
established once by `scripts/gmail_login.py`.

Read-only by construction: nothing sends, deletes, or labels. `available()`
gates every call on the Google auth cookie set actually being present, so a
missing or expired login degrades to "no results" rather than launching a
browser at nothing.

Two shapes, deliberately: `fetch()` returns the OTP shape that
`otp.process_email` consumes, and `search_application_emails` /
`read_email_bodies` mirror the MCP client's signatures and dict shape so
`run_tracking` cannot tell which source it read from. It prefers the browser
and falls back to MCP.

**Cost characteristic:** every call launches a headless Chromium (~10s) and
each body read is one navigation. One context serves all three tracking
queries — a launch per query turned a routine run into minutes. Bodies are
still sequential; that is the next thing to fix if tracking gets slow, not the
launch count.

Tradeoffs: browser reading is more fragile than the API (markup shifts, session
expiry) and the OTP window is ~10 minutes. Failure mode is acceptable — the
application parks, nothing breaks. **The upgrade path is `gws auth login`**
(`googleworkspace/cli`, browser OAuth with no Google Cloud project), which
would let the existing API client work as-is. That is strictly better than more
selectors if the markup starts churning.

**OTP class stays pure-Python pattern matching**, placed before any LLM call.
Classification of ambiguous/interview/offer mail is the only part that reasons.

**Claude Code + Chrome extension.** Verified: Claude Code integrates with the
Claude in Chrome extension via MCP, driving a real browser from the CLI. The
genuine appeal is that it borrows a browser session you're already
authenticated in — exactly the hiring.cafe and Gmail problem.

Constraints that decide where it fits:

- The window must stay visible; it pauses for login, 2FA, and CAPTCHA rather
  than pushing through, and some high-risk actions require confirmation
  regardless of mode.
- It is one visible session, not a worker pool — it does not parallelize the way
  separate Playwright profiles do.

**Verdict:** good fit for discovery, enrichment, gate-adjacent work, and any
watched session. For unattended submission, **test whether "submit this form"
trips a confirmation prompt before betting a batch on it.** Playwright remains
the default for the MWF run until that test passes.

---

## 7. The review gate

Unchanged in design, now actually wired as control. Escalation taxonomy:

| Kind | Trigger | Blocks? |
|---|---|---|
| `high_value` | One-application-per-season employers (Chicago prop trading) | No — sorted first |
| `never_auto_guess` | Work auth / sponsorship / salary | Yes — whole batch |
| `novel_question` | No bank match ≥ 0.75 | Yes — its application |
| `low_confidence` | Match 0.75–0.90 | Yes — its application |
| `cover_delta` | Opening line + extracted hooks, not full text | Yes — its application |
| `sameness_warning` | Two letters share a skeleton | Yes — both |
| `disqualified` / `parked` | — | No — one awareness line |

Auto-answer silently only when similarity ≥ 0.90 **and** `times_seen ≥ 3`
**and** `times_corrected = 0` **and** not sensitive.

Sensitive questions are **batch-scoped, one per category** (12 items → 4 on the
live batch). Batch membership is **recorded** in `review_batch_applications`,
never inferred from `review_items` — the inference inverted the intent and
blocked exactly the applications the gate had no objection to.

The gate reads the **converted** document path (the PDF stage rewrites `.txt` →
`.docx`), or every batch silently shows zero cover deltas.

The gate mirrors the acquisition predicate exactly, including the manual-ATS
skip list. A batch that clears work the submit phase would refuse is a batch
that lies.

**Escalation budget:** ~20 items at 17 applications (3 batch-wide + 1 cover
delta each). That fits 15 minutes. If the budget is exceeded as the bank grows,
the fix is raising `times_seen` confidence faster, not hiding items.

---

## 8. Continuity — how the system remembers across sessions

Three files at the repo root. This is the answer to "I want to clear the chat
and pick up cleanly."

### `PROGRESS.md`
Current state, next actions, blockers. Rewritten (not appended) at the end of
each working session. Structure:

```markdown
## Current state
[what runs, what's measured, what's half-built]

## Next up
1. [most important thing, with enough context to start cold]
2. ...

## Blocked
- [item] — blocked on [what], [what would unblock it]

## Recently finished
- [last 3–5 items, so the next session doesn't redo them]
```

A new session reads this first and should be able to start work without asking
you anything.

### `DECISIONS.md`
**Append-only.** One entry per non-obvious decision:

```markdown
### D14 — [title]  (2026-08-20)
**Decision:** what was decided.
**Why:** the reasoning.
**Replaces:** what this supersedes, if anything.
**Rejected:** the alternative and why not.
```

Never edit or delete an entry — supersede it with a new one that names the old.
This is the file that makes architectural drift visible.

### `ARCHITECTURE.md`
This document. **Editing it is part of the change, not documentation after the
fact.** A PR that changes behaviour without touching this file is incomplete.

### The standing rule

> Any change to behaviour appends to `DECISIONS.md` and edits `ARCHITECTURE.md`
> in the same commit. Any session that ends updates `PROGRESS.md`.

This is what lets you talk directly to Claude Code, change the architecture, and
have the change persist rather than evaporate when context clears.

---

## 9. Metrics — what to actually track

Notional API cost is meaningless here (no API key; Claude Max subscription).
Track instead:

- **Wall clock per application**, by ATS family and by fill path (deterministic
  vs agent fallback). This is the number the form-filler work is optimizing.
- **Rate-limit headroom** — whether a 17-application batch fits comfortably
  inside subscription limits.
- **Agent fallback rate** — how often the deterministic filler misses. Should
  trend toward zero per family as maps mature. A rising rate means a site
  changed.
- **Per-ATS submission success**, from `submission_proof` (dry runs excluded).
  Below ~50% over a rolling 30, flag that family for manual-only.
- **Gate items per batch**, against the ~20 budget.

Keep the scoring cost measurement (`$0.0063/job` batched) as the one place the
notional figure is still useful — it justifies keeping batching.

**Implemented** in `src/applypilot/reports.py`. `logs/<stage>.jsonl` is the
truth, one line per event; `applypilot report` is the ten-second read. Every
`applypilot run` writes a report at the end. The apply stage records
`fill_path` (`deterministic` when the ATS has a selector map, `agent`
otherwise) on every attempt, which is the before/after comparison §5 asks for
— it needs real applications before it says anything.

---

## 10. Subagents

One per stage, each owning its section of this document. A subagent may not
modify code outside its area; cross-cutting changes go to the orchestrator.
Same pattern as the Shopify pipeline: PM orchestrator, specialist subagents,
manual review gates, state continuity through the three files above.

| Subagent | Owns | Must not |
|---|---|---|
| **orchestrator** | Milestone sequencing, cross-cutting changes, `PROGRESS.md`, `DECISIONS.md` | Write stage code directly |
| **discovery** | hiring.cafe adapter, session maintenance, dedupe, query blobs, apply-marking | Touch scoring or apply |
| **enrichment** | Description fetch, `application_url` resolution, embedded-ATS canonicalization | Touch generation |
| **scoring** | Score prompt, batching, keyword pre-filter, ordering, calibration | Touch tailor/cover prompts |
| **tailor** | Resume tailoring, `resume_facts` validation | Touch cover letters |
| **cover** | Cover letter generation, banned-phrase filters, sameness check, voice register reads | Edit the core voice profile (read-only, mode 444) |
| **filler** | Selector maps, DOM fill pass, coverage check, fallback scoping | Change the question bank schema |
| **apply** | Browser orchestration, HITL, parking, proof capture, cooldowns, anti-detection | Bypass the gate |
| **gate** | Escalation taxonomy, batch assembly, TUI, correction propagation | Submit anything, ever |
| **tracking** | Email classification, OTP, ghosting detection | Touch apply |

**Every subagent reads:** `ARCHITECTURE.md` (this file), `PROGRESS.md`, and the
governing principles in §0. **No subagent may:** fabricate content, bypass the
gate, add an LLM call to a hot path without recording it in `DECISIONS.md`, or
loosen its own confidence thresholds.

---

## 11. Skills

Skills are directories with a `SKILL.md` plus optional executable scripts that
run via bash — deterministic operations that never load into context. The
official docs use `fill_form.py` as the canonical example, which is precisely
the §5 design.

**Build our own, don't install generic ones.** Third-party browser-automation
skills wrap what's already built here and would sit above your own code doing a
worse version of it. The selector maps and question bank are specific to this
profile and this ATS mix.

**Skills to author:**

1. **`ats-form-fill`** — **deferred, deliberately.** The selector maps are all
   `status: unverified`; a skill encoding them would teach a design that is
   about to change. Author it once the Workday map is verified against a live
   form. The code it would wrap already exists at `apply/form_fill.py`.
2. **`cover-letter-review`** — **done**, at `.claude/skills/cover-letter-review`.
   Wraps the pipeline's own validator, so its verdict is identical to what
   shipping would decide. Found a fabricated "100+ offices nationwide" in a
   live letter on its first run.
3. **`gate-triage`** — **done**, at `.claude/skills/gate-triage`. The taxonomy,
   the four auto-answer conditions, and the five invariants.

**User-supplied skills:** the operator is adding skills of his own (confirmed
2026-08-19: `ponytail` — a code-writing skill enforcing minimal solutions — plus
its five companions, and `find-skills`). Treat user-supplied skills in
`/mnt/skills/user` or the repo's skills directory as authoritative and read them
before the built-ins.

Author skills **after** the architecture settles — a skill encoding a design
that's mid-change is worse than none.

---

## 12. Audit task — find the unnecessary reasoning

A standing job for Claude Code, to run before the filler work:

> Enumerate every LLM call in the codebase. For each, record: file, purpose,
> input size, whether the output is genuinely open-ended, and whether a
> deterministic alternative exists (regex, JSON-LD, selector, lookup table,
> stored answer). Report before changing anything.

Known suspects beyond the form filler:

- Enrichment doing AI extraction where JSON-LD or selectors would work
- Any per-field validation pass
- The dead Gemini → OpenAI → DeepSeek → Anthropic cascade in `llm.py` — fully
  tested, completely unreachable on this install. Decide: delete or document as
  intentionally dormant.

---

## 13. Known problems, ranked

1. **Credential leak (do today, unrelated to the pipeline).** Local Claude Code
   session logs contain a sudo password and live third-party API tokens in
   plaintext. Rotate the tokens, change the sudo password, scrub or purge the
   logs.
2. **Dual state truth.** `jobs.state` (validated, audited) coexists with three
   deprecated status columns that live code still reads and writes. Migrate
   before scaling to 50/week — reads first, then writes, then drop.
3. **Migration strategy.** No version table; every SQL migration re-executes on
   startup, safety resting entirely on `IF NOT EXISTS`, alongside a parallel
   Python column-registry doing `ALTER TABLE`. Acceptable now; add a version
   table before the schema gets more complex.
4. **Question matching.** Hand-rolled trigram cosine, 26 entries, zero recorded
   sightings. Leave it. Revisit only when sightings accumulate and the gate
   budget is actually exceeded — an embedding model is premature.
5. **Anti-detection maintenance.** ~30 launch flags, MAIN-world init scripts,
   per-install random extension key. An arms race with no end state. The
   deterministic filler reduces exposure (fewer, faster page interactions);
   beyond that, accept the burden or accept more parking.
6. **Fabricated metrics.** See §4 — numeral extraction against `resume_facts`.

---

## 14. Immediate order of work

1. Rotate the leaked credentials.
2. Gmail via browser session → unblocks OTP → unblocks the Workday-heavy queue.
3. Run the §12 audit. Report only.
4. Deterministic filler for the top two ATS families. Measure before/after.
5. Simplify discovery to the rendered page; check for inline descriptions.
6. Add the hiring.cafe apply-marking click.
7. Establish the three continuity files and the standing rule (§8).
8. Author skills (§11).
9. Finish M9: systemd timers (prepare Mon/Wed/Fri, never submits; OTP poller),
   plain-text run reports, per-stage JSONL logs. **Done** — units in
   `deploy/systemd/` (validated, not enabled; enabling a recurring job that
   spends subscription quota is the operator's call), reports and JSONL in
   `reports.py`.

Scoring calibration (§4) and the parallel-worker question (§5) stay open,
pending measurement. Don't touch either until there's data.
