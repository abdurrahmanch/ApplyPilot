# ApplyPilot — Project Brief for External Review

**Purpose of this document:** a self-contained description of the system for an
expert reviewer who has no access to the repository. It covers what the system
does, how it is built, what is actually running, what is known to be broken or
unverified, and the specific questions worth an expert opinion.

**Status as of 2026-08-19:** milestones M0–M8 complete, M9 in progress.
~29,800 lines of Python, 508 tests passing, 1 real job application submitted.

---

## 1. What this is

An automated job-application pipeline. It discovers software engineering job
postings, scores them for fit, generates a tailored resume and cover letter for
each high-scoring one, presents a single batch to the candidate for review, and
then drives a real browser to fill out and submit the applications.

**Target throughput:** ~50 applications per week, behind exactly one human
review gate.

**Design constraint that shapes everything:** the candidate's time is the
scarce resource, not compute. The gate is meant to clear a 17-application batch
in under 15 minutes. Every design decision about what to escalate, what to
answer automatically, and what to hide is downstream of that number.

**Origin:** a fork of the open-source `ibarrajo/ApplyPilot`. Substantial parts
have been rewritten; the inherited code assumed a different candidate, a
different LLM provider setup, and a friendlier ATS landscape than reality.

---

## 2. Cost model — and why the "$87/week" figure is misleading

This is the first thing worth an expert eye, because the number that has been
carried in the project notes is **an unverified estimate denominated in a
currency the project does not actually spend.**

### The arithmetic as it was written down

```
$1.69 per application  ×  17 applications per run  =  $28.73 per run
$28.73 per run         ×  3 runs per week (Mon/Wed/Fri)  =  $86.19/week
```

### Why each term is shaky

**`$1.69 per application` was never measured on this system.** The apply stage
records cost by reading `total_cost_usd` out of the Claude Code CLI's
stream-JSON output. A search of every log file this system has ever written
found **zero** cost records. The figure appears to be carried over from an
earlier note about a different job on a different ATS (a Databricks/Greenhouse
run recorded at ~$1.58), not from this candidate's runs.

**`17 per run` is a target, not an observation.** It is 50/week ÷ 3 runs. The
system has submitted **one** application in its lifetime.

**The dollars are not dollars.** There is no `ANTHROPIC_API_KEY`, no
`GEMINI_API_KEY`, no `OPENAI_API_KEY`, and no `~/.applypilot/.env` file on this
machine. Every LLM call in the system — scoring, resume tailoring, cover-letter
generation, and the browser agent that fills out forms — is executed by
spawning the `claude` CLI, which is authenticated against a **Claude Max
subscription**. The `total_cost_usd` the CLI reports is an API-equivalent
notional price. It is consumed against subscription rate limits, not billed.

So the real resource being spent is **Max-plan quota and wall-clock time**, not
money. The honest version of the constraint is: *"can a 17-application run
complete inside the plan's rate limits, and how long does it take?"* — and that
has never been measured, because 17 applications have never been run.

### What is genuinely known about cost

One measured, load-bearing fact: **LLM scoring is batched at 10 jobs per call.**
Unbatched it was projected at ~$25/month against a $10 ceiling; batched, ~$9.
That measurement predates the move to the Max-plan CLI, so it too is now
denominated in notional dollars, but the 10× call-count reduction is real and
should not be undone.

### Questions for the reviewer

1. Is there a reason to track notional API cost at all under a subscription, or
   should the metric be rate-limit headroom and wall-clock time per application?
2. The apply stage runs a full agentic browser loop per application. Prior notes
   record 400–860 seconds per application on a well-understood ATS. At 17
   applications that is 2–4 hours of wall clock even with parallel workers. Is
   the per-application agent loop the right architecture, or should common ATS
   families be handled by deterministic form-fillers with the agent as fallback?

---

## 3. Runtime topology

Everything runs locally on one Arch Linux machine. There is no server, no
container, no cloud component.

```
  applypilot CLI (Python, Typer)
        │
        ├── SQLite database (WAL mode, thread-local connections)
        │
        ├── LLM calls ──> `claude` CLI subprocess ──> Claude Max subscription
        │                 (stream-json output parsed for text + cost)
        │
        └── Apply stage ──> Chrome for Testing (per-worker profile + extension)
                            driven by a `claude` CLI subprocess with
                            Playwright MCP tools, over CDP
```

### The three tiers

The codebase has an inherited "tier" concept that gates features on installed
dependencies:

| Tier | Name | Requires | Stages |
|---|---|---|---|
| 1 | Discovery | nothing | discover, enrich, status, dashboard |
| 2 | AI processing | an LLM provider | score, tailor, cover, pdf |
| 3 | Auto-apply | LLM + Chrome | apply |

In the original design, tier 2 used Gemini/OpenAI API keys and tier 3 used the
Claude CLI — two separate billing systems. **That split has been collapsed:**
`config.get_tier()` now treats the presence of the `claude` binary as
satisfying the LLM requirement, and `llm.py` falls back to a `claude_cli`
provider chain when no API keys are present. Which is always, here.

A consequence worth flagging: the fallback cascade in `llm.py` still contains
all its original Gemini → OpenAI → Anthropic-API logic. That code is dead on
this install but still maintained and tested.

### Critical subprocess detail

When spawning the `claude` CLI for the apply stage, the launcher **strips
`ANTHROPIC_API_KEY` from the subprocess environment**. If present, it would
override Max-plan auth and silently switch to per-token API billing. It also
passes `--strict-mcp-config`, because Docker Desktop's MCP Toolkit (if
installed) exposes a competing Playwright tool set that runs inside a container
and therefore cannot read host files — which breaks resume uploads.

---

## 4. Data model

One SQLite file, `~/.applypilot/applypilot.db`, WAL mode, 16 tables.

### `jobs` — the spine (50 columns)

One row per discovered posting, keyed by URL. Columns accrete by stage:

| Stage | Columns written |
|---|---|
| Discovery | `url`, `title`, `salary`, `description`, `location`, `site`, `strategy`, `discovered_at`, `company` |
| Enrichment | `full_description`, `application_url`, `detail_scraped_at`, `detail_error`, `enrich_attempts` |
| Scoring | `fit_score` (1–10), `score_reasoning`, `scored_at`, `eligibility` |
| Tailoring | `tailored_resume_path`, `tailored_at`, `tailor_attempts` |
| Cover | `cover_letter_path`, `cover_letter_at`, `cover_attempts` |
| Apply | `applied_at`, `apply_status`, `apply_error`, `apply_attempts`, `agent_id`, `apply_duration_ms`, `verification_confidence` |

Schema migration is handled two ways, which is a wart:

- A Python column registry (`_ALL_COLUMNS`) diffed against `PRAGMA table_info`,
  applied via `ALTER TABLE` on every startup. Also handles one-shot renames.
- A `migrations/*.sql` directory, every statement `IF NOT EXISTS`, re-executed
  in filename order on every startup. There is no version table; idempotency is
  the only safety mechanism.

### Explicit state machine

`jobs.state` holds one of 23 canonical values, with transitions validated
against a `VALID_TRANSITIONS` map and audited into `job_state_transitions`:

```
discovered → enriched → scored → tailoring → tailored
          → cover_writing → ready_to_apply → applying → applied
          → responded → interview → offer
```

plus failure branches (`enrich_failed`, `score_failed`, `low_score`,
`tailor_failed`, `cover_failed`, `apply_failed`, `needs_human`, `manual_only`)
and terminals (`rejected`, `ghosted`, `archived`).

Three older columns (`apply_status`, `apply_category`, `tracking_status`) are
retained for backward compatibility and are formally deprecated, but a lot of
live code still reads and writes them. **This dual-truth situation is a known
design smell and a good candidate for expert comment.**

### Question bank (the interesting part)

| Table | Purpose |
|---|---|
| `questions` | Canonical screening questions with a stored answer, a category, a `sensitive` flag, and a `confidence` score (0–1) |
| `question_sightings` | One row per time a question was actually seen on a form — the audit trail that makes threshold tuning possible |
| `qa_knowledge` | The inherited exact-hash store (MD5 of normalized text). Still live; `questions.legacy_qa_id` links back for incremental migration |

Matching is a hand-rolled character-trigram cosine similarity with a small
synonym table — no embeddings model, no external service. Thresholds:

- `similarity >= 0.90` **and** `times_seen >= 3` **and** not sensitive → answer
  silently
- `0.75 <= similarity < 0.90` → escalate as "low confidence, confirm the match"
- `similarity < 0.75` → escalate as a novel question
- `category in {work_auth, sponsorship, salary}` → escalate **always**,
  regardless of confidence or history

A correction from the human halves the entry's confidence and rewrites the
canonical answer, so the same correction is never needed twice.

**Reviewer note:** the trigram-cosine approach was chosen to avoid a dependency
and to keep everything local. Whether it is good enough to keep the escalation
rate low as the bank grows is untested — the bank currently holds 26 entries
and `question_sightings` is **empty**, because only one application has run.

### Review gate tables

| Table | Purpose |
|---|---|
| `review_batches` | One row per prepared run. `status ∈ {pending, approved, partial, submitted}` |
| `review_items` | What needs human eyes, typed by `kind`, with a JSON payload |
| `review_batch_applications` | Explicit batch membership (added 2026-08-19) |

### Apply-stage tables

| Table | Purpose |
|---|---|
| `parked` | Work that stopped for a human reason (captcha, MFA, login, OTP wait, form error). The run continues; nothing waits |
| `submission_proof` | Durable per-attempt evidence: outcome, screenshot path, confirmation text, duration |
| `ats_cooldowns` | Per-ATS backoff. A 403/429/Turnstile cools that ATS for the rest of the run |
| `accounts` | Credentials created on ATS sites during account-creation flows |

### Tracking tables

`tracking_emails`, `tracking_people`, `email_events` — classified inbound mail,
including the OTP class that closes the account-creation loop on Workday- and
Oracle-style ATSes.

---

## 5. The pipeline

Seven stages, run sequentially by default or concurrently with `--stream`
(each stage in a thread, the database acting as the conveyor belt, each stage
polling for pending work until its upstream is done and its own queue is
empty).

```
discover → enrich → score → tailor → cover → pdf → gate
```

### discover

Thirteen source scrapers exist (JobSpy aggregator, Workday, Greenhouse, Lever,
Ashby, Amazon, Costco, BuiltIn, HackerNews "Who is Hiring", a generic
AI-powered smart-extract, hiring.cafe). **Only hiring.cafe runs by default.**
The others remain importable and individually runnable but are dormant.

**A significant reverse-engineering finding:** hiring.cafe's documented API is
gone. `POST /api/search-jobs` returns 405. The real endpoint is the Next.js
data route — `GET /_next/data/{buildId}/index.json?searchState=...&page=N` with
an `x-nextjs-data: 1` header — and it must be called from inside a logged-in
browser session. `buildId` changes on every deploy of their site, so it is
scraped fresh on each run. Session cookies expire and are re-established via a
scripted browser login against a persistent Chrome profile.

hiring.cafe also **returns no job descriptions**, contrary to the inherited
documentation, so the enrich stage is mandatory rather than a no-op.

### enrich

Fetches the full description and resolves the true `application_url` by
scraping the listing page.

**Embedded-ATS canonicalization** happens here and at insert time: many
companies embed a Greenhouse application form in an iframe on their own careers
page, with the real job ID in a `?gh_jid=N` query parameter. Iframe forms are
2–3× slower for the browser agent to fill. Those URLs are rewritten to the
direct `job-boards.greenhouse.io/{slug}/jobs/{id}` form, using a curated
host→slug map plus a runtime cache that learns new slugs by scraping iframe
`src` attributes. A one-time backfill rewrote ~450 rows.

### score

LLM assigns a fit score 1–10 plus a reasoning string and an `eligibility` tag
(`eligible` | `non_us_only`). **Batched at 10 jobs per call** — load-bearing,
see §2. A keyword pre-filter shortcuts obviously irrelevant postings (retail
roles from the Costco scraper, for example) to a low score without an LLM call.

The inherited scoring prompt described an entirely different person — Seattle,
senior level, Go/Kotlin. It was rewritten around the candidate's actual
profile.

### tailor

Generates a tailored resume per job, then runs it through a validator that
pins facts against a `resume_facts` allowlist. Failures retry up to 5 times.

### cover

Generates a cover letter. This stage was substantially overhauled after an
audit of 3,823 generated letters found systemic problems: median length 138
words against a 300–400 target, 87 letters that pitched *LinkedIn* as the
prospective employer (the job board mistaken for the company), 62% containing
the prompt's own example sentence verbatim, and 28% containing the phrase
"aligns with".

Fixes: a `display_company()` resolver that never presents a job board as the
employer; a hard refusal to ship a letter that fails validation (rejected
drafts are written to a `_rejected.txt` sidecar and the job parks as
`cover_failed`); stem-based banned-pattern regexes that force regeneration; and
a structural check requiring at least three substantial paragraphs.

The target was subsequently reduced to **150–200 words** because 250–400
produced padding.

**Known unfixed gap:** the validator can catch fabricated *tools* (against the
facts allowlist) but not fabricated *metrics*. A letter claiming an invented
"12B+ workflows/month" would pass. Human review of generated documents is
still required.

### pdf

Converts the `.txt` resume and cover letter to `.docx` (default) or `.pdf`, and
rewrites the path column to point at the converted file. The source `.txt`
remains on disk beside it.

### gate

Assembles the review batch and stops. **Never submits.** See §6.

---

## 6. The review gate

This is the architectural centrepiece and the part most worth expert scrutiny.

### Principle

Every run prepares its applications end to end and then stops. Approving a
batch is the *only* thing that permits submission. A gate that shows everything
is a dump the human stops reading; a gate that hides a judgement call is worse.

### What escalates

An item appears if and only if one of these holds:

| Kind | Trigger | Blocks submission? |
|---|---|---|
| `high_value` | Employer with a one-application-per-season rule (Chicago trading firms) | No — awareness, sorted first |
| `never_auto_guess` | Work auth / sponsorship / salary | **Yes — blocks the entire batch** |
| `novel_question` | No bank match above 0.75 | Yes — blocks its application |
| `low_confidence` | Match between 0.75 and 0.90 | Yes — blocks its application |
| `cover_delta` | Opening line + extracted specifics, not the full text | Yes — blocks its application |
| `sameness_warning` | Two letters in the batch share a skeleton | Yes — blocks both |
| `disqualified` | Prepared but unsubmittable | No — one awareness line |
| `parked` | Stopped for a human reason | No — one awareness line |

Everything else is silent by design — that is the entire point of the
confidence gate.

**Sensitive questions are batch-scoped, one per category.** Read literally
("surface work auth, sponsorship and salary every batch, regardless of
confidence"), the first live run produced 11 items for a *single* application,
because the bank holds five phrasings of the work-authorization question and
six of the salary question. At 17 applications that is 187 confirmations of the
same three answers. They are now filed once per batch with a null
application ID, and any unresolved one blocks the whole batch — which is what
"the answer applies to all of it" actually means.

### Interface

A one-key-per-action terminal UI, deliberately designed to be driven over SSH
from a phone: no mouse, no wide tables, one item per screen. Keys are uniform
across item types (`a`pprove / `e`dit / `s`kip / `p`ark / `f`ull text / `q`uit).

### Learning loop

An edit to a matched question corrects the bank entry and halves its
confidence. An edit to a novel question creates a new canonical entry. A note
on a cover letter is appended to a persistent "voice register" file as a
*pattern*, never an instance — "letters padded to hit a word target", not "this
letter said responsive design five times".

### Voice profile

A separate read-only repository holds a core voice profile plus per-context
registers. It is loaded into the generation prompts, and the gate's cover-letter
learnings are appended to the relevant register.

---

## 7. The apply stage — and the ATS reality

This is the messiest part of the system, and the messiness is not accidental.

### Architecture

`applypilot apply` spawns N parallel workers. Each worker:

1. Atomically acquires a job (see the acquisition predicate below)
2. Launches Chrome for Testing with a per-worker profile and a custom extension
3. Builds a prompt containing the candidate profile, the job description, file
   paths for the resume and cover letter, and — if one exists — a *prior
   successful path* for this ATS family
4. Spawns a `claude` CLI subprocess with Playwright MCP tools pointed at that
   Chrome instance over CDP
5. Parses the agent's output for a result, records proof, and moves on

### The acquisition predicate

A job is acquirable only if **all** of these hold:

- an approved review batch has cleared it (added 2026-08-19; see §8)
- it has a tailored resume and a non-empty `application_url`
- `fit_score >= 8` and `discovered_at` within 14 days
- `apply_status` is null or `failed`, with fewer than 3 attempts
- `eligibility` is `eligible` or null
- its ATS is not on the manual-only skip list
- its ATS is not cooling down from a 403/429/Turnstile
- no sibling row shares its `application_url` and is already in flight
  (aggregators repost the same posting under many listing URLs — one Overstory
  job was measured at 17 repostings)
- its company is under the per-company in-flight cap (default 3 per 30 days)
- no other worker is currently on the same company or the same ATS family

Candidates are then re-sorted so that companies with the fewest in-flight
applications fire first — a round-robin that cycles through every eligible
employer once before taking a second role at any of them.

### The ATS landscape (a planning assumption that proved wrong)

The build plan assumed Greenhouse, Lever, and Ashby — clean JSON APIs, simple
forms, no accounts. In this candidate's actual queue those are a **minority**.
The dominant families are **Workday, Oracle Cloud, SAP SuccessFactors, iCIMS,
Taleo, and BrassRing** — all of which typically require creating an account,
verifying an email, and completing a multi-page form.

This is why the Gmail/OTP milestone matters far more than the original build
order implied: without automated email verification, most of the queue cannot
be completed at all.

### Human-in-the-loop

When the agent hits a captcha, an MFA prompt, or a login wall, the worker
injects a banner into the page and pauses. The browser extension records what
the human then does — clicks, navigations, form submissions with field values
(passwords masked) — into a ring buffer, and that timeline is fed back into the
agent's resume prompt so it does not redo or contradict completed steps.

A `--no-hitl` mode parks such jobs instead of waiting, for unattended runs, and
the mode can be toggled live from the extension popup without restarting.

### Anti-detection

Because a large fraction of the queue is behind bot detection, the system
carries a substantial stealth layer: ~30 Chromium launch flags copied from
Patchright, MAIN-world init scripts injected via `chrome.scripting` (isolated
content scripts and declarative MAIN-world scripts are both blocked by CSP on
real targets), a per-install randomly generated extension key so the extension
does not present a shared fingerprint, and a CSS noise filter that hides ~30
analytics and ad iframes so the accessibility tree the agent reads stays small.

**This is a maintenance liability and worth an explicit opinion from the
reviewer:** it is an arms race against parties who can change their detection
at any time, in service of submitting job applications that the candidate is
genuinely entitled to submit.

### Learned successful paths

When an application succeeds, the ordered tool-call sequence is persisted per
ATS family and prepended to the next prompt for that family as a "prior
successful path — a guide, not a script". A measured within-session effect
exists: a second Greenhouse job ran in 400s against the first's 857s.

---

## 8. What changed most recently (2026-08-19)

The review gate had been written, tested, and **left disconnected**. Nothing
called the batch builder, so no batch was ever assembled; and the job-acquisition
query read straight from the jobs table, so approving a batch changed nothing
about what went out. Approval was documentation, not control.

Both ends are now wired:

- `gate` became the seventh pipeline stage, downstream of `pdf`. It assembles
  the batch and never submits, so an unattended run stops at the human
  touchpoint instead of running past it.
- Job acquisition now requires gate approval by default. With nothing cleared,
  `apply` refuses before launching a browser. Two deliberate bypasses remain: an
  explicit `--no-gate` flag (labelled in the launch banner as unreviewed) and an
  explicit target URL (naming one job is itself a human decision, and it is the
  path the crash-recovery probe uses to finish a job that was already cleared).

Three real defects surfaced during the wiring:

1. **Batch membership was inferred from escalated items.** An application that
   escalated *nothing* — all questions auto-answered, no cover-letter concern —
   produced zero rows and therefore could never be cleared. The gate blocked
   exactly the applications it had no objection to. Membership is now recorded
   explicitly.
2. **Sensitive questions scaled catastrophically** (see §6).
3. **Cover letters were unreadable to the gate.** The PDF stage rewrites the
   path column to the converted file; the gate only read `.txt`. Every batch
   would have shown zero cover deltas and the batch-wide sameness check would
   never have fired — silently, because a batch with no cover items looks like a
   clean batch.

---

## 9. State of the world

### Live database

| Metric | Count |
|---|---|
| Jobs discovered | 268 |
| Distinct companies | 219 |
| Enriched with full descriptions | 124 |
| Scored | 124 |
| Scoring ≥ 8 (the apply threshold) | 7 |
| Tailored resume generated | 2 |
| Cover letter generated | 2 |
| **Applications actually submitted** | **1** |
| Question bank entries | 26 |
| Question sightings recorded | 0 |

The single submitted application (a web developer role via Paylocity) was
confirmed successful.

The funnel narrows hard at scoring: 124 scored, 7 above threshold. Whether that
reflects an accurate scorer or an overly strict one is unknown and worth an
expert opinion — it is the difference between "the market is thin" and "the
pipeline is discarding viable roles".

### Test suite

508 passing, 3 skipped. One test file is excluded from runs because it
segfaults inside X11 window-manipulation code; the segfault predates this work.

### Code distribution

| Area | Lines |
|---|---|
| apply (browser automation, HITL, orchestration) | 8,365 |
| discovery (13 scrapers) | 4,869 |
| scoring (score, tailor, cover, validate, render) | 4,759 |
| tracking (email classification, OTP, ghosting) | 2,226 |
| enrichment | 1,155 |
| review (the gate) | 1,053 |
| wizard (first-run setup) | 452 |

That the apply stage is nearly twice any other area is itself a finding: the
majority of the engineering effort has gone into fighting browsers and bot
detection, not into matching quality.

---

## 10. Known problems and open questions

### Blocked on external setup

- **Gmail OAuth is not configured.** The email-tracking and OTP-retrieval code
  is written and tested but cannot run. It needs a dedicated job-search Gmail
  account and a Google Cloud OAuth desktop credential. Until this exists, most
  of the queue (Workday/Oracle/SuccessFactors, all of which require email
  verification during account creation) cannot be completed end to end.

### Design questions worth an expert view

1. **Cost/quota metric.** See §2. What should the constraint actually be
   measured in under a subscription model?
2. **Agent-per-application vs. deterministic form-fillers.** 400–860 seconds of
   agentic browser loop per application, when 6–8 ATS families cover most of
   the queue and each has a stable form structure. Where is the right line
   between a deterministic filler and an LLM agent?
3. **Dual state truth.** `jobs.state` (validated, audited) coexists with three
   deprecated status columns that live code still reads and writes. What is the
   safe migration path, and is it worth doing before scaling to 50/week?
4. **Question matching.** Hand-rolled trigram cosine, no embedding model, 26
   entries, zero recorded sightings. Will the escalation rate stay low enough
   to hit the 15-minute batch target as the bank grows, or is a real embedding
   model warranted?
5. **Migration strategy.** No version table; every SQL migration re-executes on
   every startup and safety rests entirely on `IF NOT EXISTS`. Plus a parallel
   Python column-registry mechanism doing `ALTER TABLE`. Is this acceptable at
   this scale or a latent data-loss risk?
6. **Anti-detection maintenance burden.** See §7. Is this sustainable, and is
   there a materially better posture?
7. **Scoring calibration.** 7 of 124 above threshold. Too strict, correctly
   selective, or a prompt problem?
8. **Fabricated-metric risk.** The document validator pins tools but not
   numbers. What is a practical automated check for invented quantitative
   claims in generated application materials?
9. **Gate escalation rate at scale.** The design target is 17 applications
   cleared in 15 minutes. Currently a batch shows 3 batch-wide confirmations
   plus one item per cover letter. At 17 applications that is ~20 items. Is the
   escalation taxonomy right, or are there classes of error it structurally
   cannot catch?

### Operational risks already identified

- Session cookies for the primary discovery source expire and need periodic
  re-authentication through a scripted browser login.
- Local Claude Code session logs on this machine contain a sudo password and
  live third-party API tokens in plaintext. Rotation has been recommended and
  not yet performed. **This is unrelated to the pipeline itself but is the most
  serious security issue currently outstanding.**

---

## 11. Repository map

```
src/applypilot/
  cli.py               Typer CLI: run, apply, status, track, dashboard,
                       review, otp, parked, qa, creds, init
  pipeline.py          Stage orchestration, sequential and streaming
  database.py          Schema, migrations, state machine, stats
  config.py            Paths, defaults, tiers, per-company caps
  llm.py               Provider cascade; claude_cli provider
  questions.py         Similarity matching, confidence, decide()
  voice.py             Voice profile loading, sameness detection

  discovery/           13 source scrapers + URL normalization
  enrichment/          Detail scraping, embedded-ATS canonicalization
  scoring/             scorer, tailor, cover_letter, validator, pdf
  review/              batch.py (escalation), prepare.py (DB → batch),
                       tui.py (one-key review interface)
  apply/               orchestrator, launcher, chrome, prompt, hitl,
                       pacing, result_handlers, successful_paths, dashboard
  tracking/            gmail_client, classifier, otp, matcher, ghosting

migrations/            001 question bank + gate, 002 apply hardening,
                       003 review batch membership
extension/             Chrome extension: tab tracking, action log,
                       stealth injection, noise filter, popup, options
tests/                 508 tests
```

### Runtime data (outside the repository)

```
~/.applypilot/
  profile.json           Candidate PII, mode 600
  resume.txt / .pdf      Master resume
  candidate-dossier.md   Full evidence base for generation
  applypilot.db          The database
  searches.yaml          Search configuration
  company_limits.yaml    Per-company application caps
  tailored_resumes/      Generated, per job
  cover_letters/         Generated, per job
  logs/                  Per-run and per-worker logs
  chrome-for-testing/    The browser build used for applying
```

---

## 12. Note to the reviewer

The most useful feedback would be on §10 — particularly questions 1, 2, and 7,
which between them determine whether the 50-applications-per-week target is
reachable at all, and whether the effort is currently going into the right part
of the problem.

Context that may be relevant to your recommendations: this is a single
candidate's personal job search, not a product. The person running it graduates
in December 2026 and wants applications that position real work honestly rather
than padding it — a stated design goal that has already driven decisions like
cutting the cover-letter target from 400 words to 175, and trimming a project
description that overstated what had actually been built.
