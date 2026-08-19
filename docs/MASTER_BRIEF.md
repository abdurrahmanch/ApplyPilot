# ApplyPilot — Master Project Brief

**A complete, self-contained description of the system: what it does, how it is
built, every significant design decision and the reasoning behind it, what is
actually running, and what is unresolved.**

Written for an expert reviewer or agent with no access to the repository. No
prior context is assumed.

- **Snapshot date:** 2026-08-19
- **Status:** milestones M0–M8 complete, M9 in progress
- **Size:** ~29,900 lines of Python, 508 tests passing, 3 skipped
- **Lifetime output:** 268 jobs discovered, 1 application submitted

---

# PART 0 — How to read this document

| Part | Contents |
|---|---|
| I | Mission, constraints, and the human factor that drives every design choice |
| II | Cost and resource model — including a correction to a figure that had been carried in project notes |
| III | Architecture: runtime topology, data model, pipeline stages |
| IV | The review gate — the architectural centrepiece |
| V | The apply stage and the ATS reality that reshaped the project |
| VI | Response tracking and OTP |
| VII | **The full design decision record** — current build (D1–D13, milestones, surprises) and inherited (#0–#51) |
| VIII | Current measured state |
| IX | Known problems and open questions for review |
| X | Repository map, runtime data layout, operational runbook |

Parts II, VII, and IX carry the most reviewable content. Part VII is the
reasoning record and is the reason this document is long.

---

# PART I — Mission and constraints

## What the system does

Discovers software engineering job postings → scores them for fit → generates a
tailored resume and cover letter for each high-scoring one → presents a single
batch to the candidate for review → drives a real browser to fill out and
submit the approved applications → tracks the email responses that come back.

## The target

**~50 applications per week, behind exactly one human review gate.**

## The constraint that shapes everything

The candidate's attention is the scarce resource, not compute. The design
target is **a 17-application batch cleared in under 15 minutes**.

Every decision about what to escalate, what to answer automatically, and what
to hide is downstream of that number. A gate that shows everything becomes a
dump the human stops reading. A gate that hides a judgement call is worse. The
escalation taxonomy in Part IV exists to thread that needle.

## Secondary constraints

- **Honest positioning.** A stated goal is applications that describe real work
  accurately rather than padding it. This has driven concrete decisions: cutting
  the cover-letter target from 400 words to 175 because the longer target
  produced filler, and trimming a project description in the resume that
  overstated what had actually been built (a database schema described with
  implementation verbs).
- **Local only.** No server, no container, no cloud component. One Arch Linux
  machine.
- **No API keys.** All LLM work runs through a Claude Max subscription via the
  `claude` CLI. See Part II.
- **Privacy.** The candidate's surname is deliberately truncated in all
  generated output to limit doxxing; forms demanding a full legal surname are
  parked for manual handling rather than auto-filled.

## Origin

A fork of the open-source `ibarrajo/ApplyPilot`. Three remotes exist: the fork,
the original upstream, and a third downstream variant. A verification pass at
project start established that the fork was **151 commits ahead and 0 behind**
every upstream — so a planned cherry-pick of upstream hardening was a no-op.

Substantial parts have since been rewritten. The inherited code assumed a
different candidate (Seattle, senior, Go/Kotlin), a different LLM provider
setup (Gemini + OpenAI API keys), and a friendlier ATS landscape than reality.

---

# PART II — Cost and resource model

This section corrects a figure that had been carried in the project notes and
repeated as a blocker: **"~$1.69 per application, ~$29/run, ~$87/week."**

That number is wrong in three independent ways.

## The arithmetic as written

```
$1.69 per application  ×  17 applications per run   =  $28.73 per run
$28.73 per run         ×  3 runs per week (M/W/F)   =  $86.19 per week
```

## Why each term fails

**1. `$1.69 per application` was never measured on this system.**

The apply stage records cost by parsing `total_cost_usd` out of the Claude Code
CLI's stream-JSON output. A search of every log file this system has ever
written returns **zero** cost records. The figure appears to derive from an
earlier note about a different job on a different ATS (a Greenhouse run
recorded at ~$1.58), not from this candidate's runs.

**2. `17 per run` is a target, not an observation.**

It is 50/week ÷ 3 runs. The system has submitted **one** application in its
lifetime.

**3. The dollars are not dollars.**

There is no `ANTHROPIC_API_KEY`, no `GEMINI_API_KEY`, no `OPENAI_API_KEY`, and
no `~/.applypilot/.env` on this machine. Every LLM call — scoring, resume
tailoring, cover-letter generation, and the browser agent that fills forms — is
executed by spawning the `claude` CLI, authenticated against a **Claude Max
subscription**. The `total_cost_usd` the CLI reports is a notional
API-equivalent price. It is consumed against subscription rate limits, not
billed.

## What the real constraint is

**Rate-limit headroom and wall-clock time**, neither of which has been measured
at scale. Prior notes record **400–860 seconds of agentic browser loop per
application**. At 17 applications that is 2–4 hours of wall clock even with
parallel workers.

## What is genuinely known

One measured, load-bearing fact survives: **LLM scoring is batched at 10 jobs
per call.** A single trivial CLI call costs ~$0.018 in notional terms because
every `claude -p` invocation re-sends ~26k tokens of system prompt. One call per
job at ~1,400 jobs/month projected to ~$25/month against a $10 ceiling. Batched
at 10, a real batch measured **$0.0632 for 10 jobs = $0.0063/job ≈ $9/month**.

The 10× reduction in process spawns is real regardless of the currency. **Do not
un-batch scoring.**

A malformed batch degrades only itself: any job whose block is missing from the
response returns `score=None` and re-enters the normal retry/backoff path.

---

# PART III — Architecture

## Runtime topology

```
  applypilot CLI  (Python 3, Typer, Rich)
        │
        ├── SQLite  (WAL mode, thread-local connections, ~/.applypilot/applypilot.db)
        │
        ├── LLM calls ──> `claude` CLI subprocess
        │                 └─ --output-format json, parsed for text + notional cost
        │                 └─ authenticated against Claude Max subscription
        │
        └── Apply stage ──> Chrome for Testing
                            ├─ per-worker user-data-dir + custom extension
                            └─ driven by a `claude` CLI subprocess holding
                               Playwright MCP tools, connected over CDP
```

Everything is local. No network services are run; the only outbound traffic is
scraping, LLM calls, and the browser.

## The tier system

An inherited concept that gates features on installed dependencies:

| Tier | Name | Requires | Stages |
|---|---|---|---|
| 1 | Discovery | nothing | discover, enrich, status, dashboard |
| 2 | AI processing | an LLM provider | score, tailor, cover, pdf |
| 3 | Auto-apply | LLM + Chrome | apply |

Originally tier 2 used Gemini/OpenAI API keys while tier 3 used the Claude CLI —
two separate billing systems. **That split has been collapsed.**
`config.get_tier()` now treats the presence of the `claude` binary as
satisfying the LLM requirement, and `llm.py` falls back to a `claude_cli`
provider chain whenever no API keys are present. Which is always, here.

**Consequence worth flagging:** the entire Gemini → OpenAI → DeepSeek →
Anthropic fallback cascade in `llm.py` remains, fully tested, and completely
dead on this install.

## Two critical subprocess details

**`ANTHROPIC_API_KEY` is stripped from the apply subprocess environment.** If
present it would override Max-plan auth and silently switch to per-token API
billing.

**`--strict-mcp-config` is mandatory.** Docker Desktop's MCP Toolkit, if
installed, exposes a competing set of Playwright tools that run inside a
container and therefore cannot read host files — which silently breaks resume
uploads. Strict mode ensures only the local Playwright MCP is visible.

The full spawn line per worker:

```
claude --model <m> -p
       --mcp-config .mcp-apply-<id>.json --strict-mcp-config
       --permission-mode bypassPermissions
       --no-session-persistence
       --disallowedTools <...>
       --output-format stream-json --verbose -
```

## Data model

One SQLite file, WAL mode, **16 tables**.

### `jobs` — the spine (50 columns)

One row per discovered posting, keyed by URL. Columns accrete by stage:

| Stage | Columns written |
|---|---|
| Discovery | `url`, `title`, `salary`, `description`, `location`, `site`, `strategy`, `discovered_at`, `posted_at`, `company` |
| Enrichment | `full_description`, `application_url`, `detail_scraped_at`, `detail_error`, `enrich_attempts`, `enrich_next_retry_at` |
| Scoring | `fit_score` (1–10), `score_reasoning`, `scored_at`, `score_attempts`, `eligibility` |
| Tailoring | `tailored_resume_path`, `tailored_at`, `tailor_attempts` |
| Cover | `cover_letter_path`, `cover_letter_at`, `cover_attempts` |
| Apply | `applied_at`, `apply_status`, `apply_error`, `apply_attempts`, `agent_id`, `last_attempted_at`, `apply_duration_ms`, `apply_task_id`, `verification_confidence` |

### Schema migration — two parallel mechanisms

This is a known wart:

1. **A Python column registry** (`_ALL_COLUMNS`) diffed against
   `PRAGMA table_info` and applied via `ALTER TABLE` on every startup. Also
   handles one-shot column renames.
2. **A `migrations/*.sql` directory**, every statement `IF NOT EXISTS`,
   re-executed in filename order on every startup. **There is no version
   table**; idempotency is the only safety mechanism. A failing migration is
   logged and skipped, on the principle that a broken optional table must never
   stop the existing stages from running.

### Explicit state machine

`jobs.state` holds one of **23 canonical values**, transitions validated against
a `VALID_TRANSITIONS` map, every transition audited into
`job_state_transitions` (from, to, timestamp, reason, metadata).

```
discovered → enriched → scored → tailoring → tailored
          → cover_writing → ready_to_apply → applying → applied
          → responded → interview → offer
```

Failure and side branches: `enrich_failed`, `score_failed`, `low_score`,
`tailor_failed`, `cover_failed`, `apply_failed`, `needs_human`, `manual_only`.
Terminals: `rejected`, `ghosted`, `archived`.

Three older columns — `apply_status`, `apply_category`, `tracking_status` — are
retained for backward compatibility and formally deprecated, but a great deal
of live code still reads and writes them. **This dual-truth situation is the
most significant unaddressed design smell in the codebase.**

### The question bank

The system's answer to ATS screening questions.

| Table | Purpose |
|---|---|
| `questions` | Canonical questions: text, stored answer, category, `sensitive` flag, `confidence` (0–1), `times_seen`, `times_corrected` |
| `question_sightings` | One row per time a question was actually seen on a real form — the audit trail that makes threshold tuning possible |
| `qa_knowledge` | The inherited exact-hash store (MD5 of normalized text). Still live; `questions.legacy_qa_id` links back for incremental migration |

**Matching** is a hand-rolled character-trigram cosine similarity with a small
synonym table. No embedding model, no external service, no network call.

**Decision thresholds:**

| Condition | Action |
|---|---|
| `similarity ≥ 0.90` **and** `times_seen ≥ 3` **and** not sensitive | Answer silently |
| `0.75 ≤ similarity < 0.90` | Escalate: "low confidence, confirm the match" |
| `similarity < 0.75` | Escalate: novel question |
| `category ∈ {work_auth, sponsorship, salary}` | Escalate **always**, regardless of confidence or history |

**The learning loop:** a human correction to a matched question rewrites the
canonical answer and **halves** its confidence (`CORRECTION_PENALTY = 0.5`). A
correction to a novel question creates a new canonical entry. Either way the
same correction is never needed twice.

The inherited `qa_knowledge` store had none of this — exact hash only, no
similarity, no confidence, and no notion of a question being too sensitive to
auto-answer. The whole confidence layer was built for this project.

### Review gate tables

| Table | Purpose |
|---|---|
| `review_batches` | One row per prepared run. `status ∈ {pending, approved, partial, submitted}` |
| `review_items` | What needs human eyes, typed by `kind`, JSON payload, resolution |
| `review_batch_applications` | Explicit batch membership (added 2026-08-19) |

### Apply-stage tables

| Table | Purpose |
|---|---|
| `parked` | Work that stopped for a human reason — captcha, MFA, login, OTP wait, form error, unanswerable question. The run continues; nothing waits |
| `submission_proof` | Durable per-attempt evidence: outcome, screenshot path, confirmation text and ID, duration. Dry runs excluded, so per-ATS health is never poisoned by attempts no employer saw |
| `ats_cooldowns` | Per-ATS backoff. A 403, 429, or Turnstile cools that ATS for the rest of the run rather than hammering it |
| `accounts` | Credentials created on ATS sites during account-creation flows |

### Tracking tables

`tracking_emails`, `tracking_people`, `email_events` — classified inbound mail,
including the `otp` class that closes the account-creation loop on Workday- and
Oracle-style ATSes.

## The pipeline

Seven stages:

```
discover → enrich → score → tailor → cover → pdf → gate
```

Run sequentially by default, or concurrently with `--stream`: each stage in its
own thread, the database acting as the conveyor belt, each stage polling for
pending work until its upstream reports done **and** its own queue is empty.

### discover

**Thirteen source scrapers exist** — JobSpy aggregator (LinkedIn/Indeed/
ZipRecruiter), Workday, Greenhouse, Lever, Ashby, Amazon, Costco, BuiltIn,
HackerNews "Who is Hiring", a generic AI-powered smart-extract, and
hiring.cafe. **Only hiring.cafe runs by default.** The rest stay importable and
individually runnable but dormant — not deleted.

**The significant reverse-engineering finding:** hiring.cafe's documented API is
gone. `POST /api/search-jobs` returns 405; the count endpoint returns 404. The
site is a Firebase-gated Next.js app and results come from the page-data route:

```
GET /_next/data/{buildId}/index.json?searchState={json}&page={n}
headers: x-nextjs-data: 1
```

- `buildId` changes on **every** hiring.cafe deploy, so it is scraped fresh per
  run and never cached.
- The route requires an authenticated session, so requests are issued from
  inside a persistent logged-in Chromium profile established once by a login
  script. This collapses the planned fallback ladder — the plain-HTTPS rung is
  not reachable at all, so the browser path is primary, not fallback.
- Pagination is `&page=N`, terminated by an `ssrIsLastPage` flag.
- **Descriptions are not inline.** Neither the search response nor the per-job
  route carries posting text, contradicting the original plan. The enrich stage
  is therefore **mandatory** for this source, not a fallback.

The `searchState` blobs are committed verbatim from the candidate's live saved
searches. The originally documented schema was wrong on four points: latitude
and longitude are floats not strings; there is no `flexible_regions` key;
`workplace_types` is per-location not top-level; and the query field is
`jobTitleQuery`, not `searchQuery`.

### enrich

Fetches the full description and resolves the true `application_url` by
scraping the listing page.

**Embedded-ATS canonicalization** happens here and at insert time. Many
companies embed a Greenhouse application form in an iframe on their own careers
page, with the real job ID in a `?gh_jid=N` parameter. Iframe forms are 2–3×
slower for the browser agent to fill (parent-page noise, iframe-relative
references). Those URLs are rewritten to the direct
`job-boards.greenhouse.io/{slug}/jobs/{id}` form using a curated host→slug map
plus a **runtime cache that learns new slugs** by scraping iframe `src`
attributes on any page it renders. Aggregator-discovered URLs on unknown
employer hosts therefore teach the canonicalizer organically. A one-time
backfill rewrote ~450 rows.

### score

An LLM assigns a fit score 1–10, a reasoning string, and an `eligibility` tag
(`eligible` | `non_us_only`). Batched at 10 jobs per call. A keyword pre-filter
shortcuts obviously irrelevant postings (retail roles from the Costco scraper,
for example) to a low score with no LLM call at all.

Descriptions are truncated to **1,800 characters** (~300 words) for scoring. The
tailor stage reads the full text; that is where a disqualifier missed by
truncation surfaces.

Ordering is `COALESCE(posted_at, discovered_at) DESC` with `fit_score` as
tiebreaker, plus a per-company `ROW_NUMBER()` window for round-robin fairness.

### tailor

Generates a tailored resume per job, then validates it against a `resume_facts`
allowlist that pins claimable facts. Failures retry up to 5 times.

### cover

Generates a cover letter. **This stage was substantially overhauled** after an
audit of 3,823 previously generated letters found systemic failures:

- Median length **138 words** against a 300–400 target
- **87 letters pitched LinkedIn as the prospective employer** — the job board
  mistaken for the company
- **62%** contained the prompt's own example sentence verbatim
- **28%** contained the phrase "aligns with"
- The intended 4-paragraph structure routinely collapsed to 3

Fixes shipped:

1. A `display_company()` resolver — company column → ATS tenant slug → empty.
   A job board is **never** presented as the employer; an unknown company is
   labelled as unknown with instructions to infer from the description.
2. A hard refusal to ship a letter that fails validation. Rejected drafts go to
   a `_rejected.txt` sidecar and the job parks as `cover_failed` for retry.
   Previously, failed letters shipped as ready-to-apply.
3. Stem-based banned-pattern regexes at **error** tier (forcing regeneration),
   distinct from the existing warning-tier banned words.
4. A structural check: fewer than 3 substantial paragraphs is an error.
5. Too-short retries receive explicit per-paragraph expansion targets rather
   than a generic error string.

The word target was subsequently cut to **150–200 words**, because 250–400
produced padding.

**Known unfixed gap:** the validator catches fabricated *tools* (against the
facts allowlist) but not fabricated *metrics*. A letter claiming an invented
"12B+ workflows/month" would pass. Human review of generated documents remains
necessary.

### pdf

Converts the `.txt` resume and cover letter to `.docx` (default) or `.pdf`, and
rewrites the path column to point at the converted file. **The source `.txt`
remains on disk beside it** — a detail that later mattered (Part VII, 2026-08-19
defect 3).

### gate

Assembles the review batch and stops. **Never submits.** See Part IV.

---

# PART IV — The review gate

The architectural centrepiece, and the part most worth expert scrutiny.

## Principle

Every run prepares its applications end to end and then stops. **Approving a
batch is the only thing that permits submission.**

## What escalates, and what it blocks

An item appears if and only if one of these holds:

| Kind | Trigger | Blocks submission? |
|---|---|---|
| `high_value` | Employer with a one-application-per-season rule (Chicago proprietary trading firms — humans read those applications and a wasted one cannot be retried) | No — awareness, but **sorted first** so it gets attention while it is freshest |
| `never_auto_guess` | Work authorization / sponsorship / salary | **Yes — blocks the entire batch** |
| `novel_question` | No bank match above 0.75 | Yes — blocks its application |
| `low_confidence` | Match between 0.75 and 0.90 | Yes — blocks its application |
| `cover_delta` | Opening line + extracted specifics, **not** the full text | Yes — blocks its application |
| `sameness_warning` | Two letters in the batch share a skeleton | Yes — blocks both |
| `disqualified` | Prepared but unsubmittable | No — one awareness line |
| `parked` | Stopped for a human reason | No — one awareness line |

**Everything else is silent by design.** That is the entire point of the
confidence gate.

## Cover deltas, not cover letters

The gate shows the opening line and a crude extraction of concrete
hooks — capitalised multi-word names, specific nouns — plus word count and
repeated phrases. Full text is expandable on demand. Showing full text by
default turns a 15-minute review into an hour of reading.

## Sensitive questions are batch-scoped, one per category

Read literally — *"surface work auth, sponsorship, and salary every batch,
regardless of confidence"* — the first live run produced **11 items for a single
application**, because the bank holds five phrasings of the work-authorization
question and six of the salary question. At 17 applications that is 187
confirmations of the same three answers.

They are now filed **once per batch** with a null application ID, and any
unresolved one blocks the whole batch — which is what "the answer applies to
all of it" actually means. The live batch went from 12 items to 4.

## Interface

A one-key-per-action terminal UI, deliberately designed to be driven **over SSH
from a phone**: no mouse, no wide tables, nothing requiring a scrollback
buffer. One item per screen, one question per item. Keys are uniform across
item types:

```
a  approve as shown
e  edit the answer (or record a note on a letter)
s  skip this application entirely
f  show the full text (cover letters)
p  park this application
q  quit, leave the rest pending
```

Batch-wide questions do not offer `s` or `p`, because skipping one would
silently apply to every application.

Nothing is submitted from the TUI. Approving sets `review_batches.status`,
which is the only thing the submit phase looks at.

## The learning loop

- An edit to a **matched** question corrects the bank entry and halves its
  confidence.
- An edit to a **novel** question creates a new canonical entry.
- A note on a **cover letter** is appended to a persistent voice register as a
  *pattern*, never an instance — "letters padded to hit a word target", not
  "this letter said responsive design five times".

## Voice profile

A separate read-only repository holds a core voice profile (supplied directly by
the candidate, stored mode 444, never reconstructed from memory) plus
per-context registers. It is loaded into the generation prompts, and the gate's
cover-letter learnings are appended to the job-applications register.

---

# PART V — The apply stage and the ATS reality

The messiest part of the system. The messiness is not accidental.

## Architecture

`applypilot apply` spawns N parallel workers. Each worker:

1. Atomically acquires a job (predicate below)
2. Launches Chrome for Testing with a per-worker profile and a custom extension
3. Builds a prompt: candidate profile, job description, file paths for resume
   and cover letter, and — if one exists — a **prior successful path** for this
   ATS family
4. Spawns a `claude` CLI subprocess with Playwright MCP tools pointed at that
   Chrome instance over CDP
5. Parses agent output for a result, records proof, moves on

## The acquisition predicate

A job is acquirable only if **all** hold:

- **An approved review batch has cleared it** (added 2026-08-19)
- It has a tailored resume and a non-empty `application_url`
- `fit_score ≥ 8` and `discovered_at` within 14 days
- `apply_status` is null or `failed`, with fewer than 3 attempts
- `eligibility` is `eligible` or null
- Its ATS is not on the manual-only skip list
- Its ATS is not cooling down from a 403/429/Turnstile
- **No sibling row shares its `application_url` and is already in flight.**
  Aggregators repost the same posting under many listing URLs — one job was
  measured at **17 repostings**. Without this guard every variant becomes
  eligible after the first applies.
- Its company is under the per-company in-flight cap (default 3 per 30 days,
  YAML-overridable; `-1` means unlimited, `0` means blocked)
- No other worker is currently on the same company or the same ATS family

Candidates are then **re-sorted so companies with the fewest in-flight
applications fire first**, with fit score as tiebreaker. Net effect: the system
cycles through every eligible employer once before taking a second role at any
of them.

Stale locks from crashed runs (older than 30 minutes) are released on
acquisition, with each affected job transitioned back to `ready_to_apply`.

## The ATS landscape — a planning assumption that proved wrong

The build plan assumed **Greenhouse, Lever, and Ashby**: clean JSON APIs, simple
forms, no accounts required.

In the candidate's actual queue those are a **minority**. The dominant families
are **Workday, Oracle Cloud, SAP SuccessFactors, iCIMS, Taleo, and BrassRing** —
all of which typically require creating an account, verifying an email address,
and completing a multi-page form.

This is why the Gmail/OTP milestone matters far more than the original build
order implied. Without automated email verification, most of the queue cannot
be completed at all.

## Human-in-the-loop

When the agent hits a captcha, an MFA prompt, or a login wall, the worker
injects a banner into the page and pauses.

The browser extension then **records what the human does** — clicks on buttons
and links, form submissions with field values (passwords masked), history
navigations — into a per-worker ring buffer (200 events / 64KB cap). On resume,
that timeline is formatted into a `USER ACTIONS DURING PAUSE:` block and
threaded into the agent's resume prompt, so it does not redo or contradict
completed steps.

Three near-duplicate HITL code paths were later collapsed into one parameterized
helper, cutting ~150 lines.

A `--no-hitl` mode parks such jobs instead of waiting, for unattended runs, and
the mode can be **toggled live from the extension popup** without restarting —
the pause path reads the live worker state at the start of each pause rather
than the CLI-time default.

A stdin fallback exists for when the injected banner's Done button is broken:
a lock-gated one-shot reader lets the user type `done` in the terminal.

## Anti-detection

Because a large fraction of the queue sits behind bot detection, the system
carries a substantial stealth layer:

- **~30 Chromium launch flags** copied from Patchright's switch list
- **MAIN-world init scripts injected via `chrome.scripting.executeScript`.**
  This mechanism is load-bearing: declarative MAIN-world content scripts and
  `<script>`-tag injection from isolated content scripts are **both blocked by
  CSP on real targets**. Only extension-privileged injection works. Overrides
  applied: `navigator.webdriver` undefined, populated `chrome.runtime`, two
  plausible fake plugins, non-empty `navigator.languages`, disguised WebGL
  vendor/renderer, and a notification-permission query that mirrors real state
  rather than the headless-deterministic answer.
- **A per-install randomly generated RSA extension key**, so the extension does
  not present a shared fingerprint across all users of the tool
- **A CSS noise filter** hiding ~30 analytics and ad iframes at `document_start`
  so the accessibility tree the agent reads stays small. Captcha iframes are
  explicitly **not** hidden — the agent's captcha detection needs them.

Verification note: these overrides can only be confirmed by reading from the
MAIN world, because Playwright's `page.evaluate` reads from the isolated world
by design.

**This is a maintenance liability and worth an explicit opinion.** It is an
arms race against parties who can change detection at any time, in service of
submitting job applications the candidate is genuinely entitled to submit.

## Learned successful paths

When an application succeeds, the ordered tool-call sequence is persisted per
ATS family (capped at 60 steps — most of the value is in the form-fill tail) and
prepended to the next prompt for that family as a **"prior successful path — a
guide, not a script"**, framed explicitly so the agent does not blindly replay
it on a different employer's form.

A measured effect exists: a second Greenhouse job ran in 400s against the
first's 857s. The persistence makes that survive pipeline restarts and carry
across employers on the same ATS.

---

# PART VI — Response tracking and OTP

Inbound mail is fetched, then triaged in **pure Python** (confirmation /
rejection / noise / OTP), with an LLM classifying only the ambiguous, interview,
and offer cases. Results land in `tracking_emails.classification` and update the
job's tracking status — and, since the state-machine work, the canonical
`state` column as well.

The **OTP class** is a pure-Python pattern match placed *before* any LLM call.
It closes the account-creation loop on Workday- and Oracle-style ATSes: a worker
that needs an emailed verification code parks, the poller retrieves and files
the code, and the worker resumes.

`applypilot otp --interval` supports polling every 60 seconds during an apply
phase and less often otherwise.

**This entire subsystem is written and tested but cannot run.** See Part IX.

---

# PART VII — The design decision record

## VII.A — Decisions made during this build

### D1 — No API key; all LLM stages run through the Claude Code CLI

The candidate has no API key and wants the existing subscription used. This
overrode the original plan's "use the Anthropic API for score/tailor/cover".

Implementation: a `claude_cli` provider in `llm.py` that shells out to
`claude -p --model <m> --output-format json`, flattening the message list into
a single prompt with the system block prepended. It becomes the sole fallback
chain when no provider API keys are present, so every existing caller of
`LLMClient.chat()` keeps working unchanged. Tiering maps to `--model`: fast tier
for scoring, quality tier for tailor and cover.

**Trade-off accepted:** per-call process spawn (~1–2s overhead) and no
token-level cost accounting. Scoring is therefore batched — see D5.

### D2 — A planned `--validation lenient` flag was dropped

Investigation showed the flag does not exist in any repo, and the validator is
pure Python (regex and set checks) with **no LLM judge**. There was no judge
call to skip and no per-job saving available. Building the flag would have
gated nothing.

### D3 — Recency ordering needs a posting date *(superseded by D8)*

The `jobs` table had `discovered_at` but no posting date. hiring.cafe supplies a
real one, so a new column was planned.

### D4 — `min_score` floor handling

Upstream default is 8. Rather than editing the shipped default, runs pass
`--min-score` explicitly, so upstream behaviour stays intact for dry runs.

### D5 — Scoring is batched, and batching is load-bearing

See Part II. `SCORE_BATCH_SIZE = 10`. Measured 4× cost reduction. The batched
path runs when `workers ≤ 1`; the threaded and sequential per-job paths are
untouched and still available.

### D6 — The scoring prompt was written for a different person

The inherited prompt hardcoded "The candidate is US-based (Seattle, WA)", a
Go/Kotlin/Kubernetes senior stack, and a Seattle-area commute rule, and reserved
its top scores for Senior/Staff/Principal roles — **the exact inverse** of what
this candidate needs.

Rewritten:

- Candidate location comes from `profile.json`, not a literal
- Score bands target entry-level/new-grad/junior IC roles on the actual stack
  (Java/Spring Boot/PostgreSQL, TypeScript/React/Next.js)
- Senior/Staff/Principal/Lead/Manager titles are a hard 1–2, not a top score
- Roles requiring an active security clearance are a hard 1–2
- Years-of-experience bars are read literally: 0–3 ideal, 4–5 capped at 6,
  6+ capped at 3
- Commute rule is Chicago metro; fully remote US roles unrestricted

Verified on 20 real jobs: senior titles scored 1–2, "Web Developer" scored 9,
"Backend Engineer, Multiplayer" scored 7. No parse errors.

### D7 — Description truncation at 1,800 characters

Down from 6,000. The tailor stage still reads the full description; that is
where a disqualifier missed by truncation surfaces.

### D8 — Recency ordering shipped without a migration

`get_jobs_by_stage` orders by `COALESCE(posted_at, discovered_at) DESC` with
`fit_score DESC` as tiebreaker. The column already existed as `posted_at` and
hiring.cafe populates it from its estimated publish date, so no migration was
needed. **Supersedes D3.**

### D9 — A `gate` stage closes the prepare run

`STAGE_ORDER` gained a seventh stage, `gate`, downstream of `pdf`. It assembles
the review batch and **never submits**. `applypilot run` therefore ends at the
human touchpoint by default instead of quietly running past it — the property
that the unattended timers planned for the rest of M9 depend on.

In streaming mode the gate is special-cased: it waits for its upstream to finish
and then runs **exactly once**. Treating it as a conveyor stage would build one
batch per polling pass and split a single run's work across several of them.

### D10 — Batch membership is recorded, not inferred

`submittable_applications` derived a batch's membership from `review_items`,
which **inverted the intent in the most common case**. An application whose
questions all auto-answered and whose cover letter raised no delta produces zero
items — so it was never "in" the batch and could never be cleared. **The gate
blocked exactly the applications it had no objection to.**

Migration 003 adds `review_batch_applications`, one row per application per
batch. `review_items` keeps its narrower job: what needs human eyes. Batches
predating the table fall back to the old derivation.

### D11 — Sensitive questions are batch-scoped, one per category

See Part IV. 12 items → 4 on the live batch.

### D12 — The gate mirrors the acquisition predicate, including the manual-ATS skip

A batch that clears work the submit phase would refuse is a batch that lies. The
first live run cleared a Workday role that the acquisition path then marked
`manual_only` on sight, because its host is on the manual-ATS skip list.

The readiness query now applies the same predicate — plus open `parked` rows and
terminal failure states — and diverts those to one awareness line each.

### D13 — Approval is binding, with two named escape hatches

`acquire_job(require_gate=True)` is the default. With nothing cleared it returns
nothing, and the apply entry point refuses **before any browser launches**,
naming which of the two reasons applies (no batch built, or one still awaiting
review).

Two bypasses stay open and are deliberate:

- **`--no-gate`** — explicit, and labelled in the launch banner as unreviewed
- **An explicit target URL** — naming one job is itself a human decision, and it
  is the path the crash-reconnect probe uses to finish a job that was already
  cleared before the run died

`cleared_urls` unions across **every** opened batch rather than reading only the
newest, so a partial batch whose held-back items are resolved later is not
invalidated by the next run, and an interrupted apply run resumes. A batch is
marked `submitted` only once every application it cleared has actually gone out.

### Milestone record

| Milestone | State | Note |
|---|---|---|
| M0 fork + verify | done | All six verification items answered; upstream cherry-pick proved a no-op |
| M1 Claude consolidation | done | `claude_cli` provider; no API key anywhere |
| M2 hiring.cafe discovery | done | 268 real jobs, 219 companies |
| M3 scoring patch | done | Prompt retargeted, batched, recency ordering |
| M4 schema + question bank | done | 6 tables, similarity matching, 25 seeded answers |
| M5 register + letters | done | Voice register written and wired, batch sameness check |
| M6 review gate | done | Full escalation logic |
| M7 apply hardening | done | Pacing, parking, proof, per-ATS cooldowns |
| M8 Gmail + OTP | code done, **blocked on auth** | See Part IX |
| M9 timers + reports | in progress | Gate wired end to end; timers and reports remain |

### The 2026-08-19 wiring session — three real defects

The review gate had been written, tested, and **left disconnected**. Nothing
called the batch builder, so no batch was ever assembled; and the job-acquisition
query read straight from the jobs table, so approving a batch changed nothing
about what went out. **Approval was documentation, not control.**

Wiring both ends surfaced three genuine defects:

1. **Batch membership inversion** (D10) — the gate blocked exactly what it had
   no objection to.
2. **Sensitive-question explosion** (D11) — 187 confirmations of three answers
   at target scale.
3. **Cover letters were unreadable to the gate.** The PDF stage rewrites the
   path column to the converted file; the gate only read `.txt`. Every batch
   would have shown zero cover deltas and the batch-wide sameness check would
   never have fired — **silently**, because a batch with no cover items looks
   exactly like a clean batch.

### Things that surprised us — do not re-derive these

- hiring.cafe's documented API is gone; the real route is the Next.js page-data
  endpoint from inside a logged-in browser, with a `buildId` that changes on
  every deploy
- hiring.cafe returns **no** job descriptions; enrich is required, not optional
- The documented `searchState` schema was wrong on four points
- The inherited scoring prompt described a different person entirely
- The inherited cover-letter target (250–400 words) produced padding
- **A dry run polluted the Q&A bank, and the next real submission replayed a
  fabricated answer from it.** Dry runs no longer write to the bank.
- The ATS mix is far harder than planned — Greenhouse/Lever/Ashby are a
  minority; Workday, Oracle, SuccessFactors, iCIMS, Taleo and BrassRing dominate

## VII.B — Inherited decision record

Decisions carried from the fork's own history, preserved because they encode
non-obvious constraints. Numbering is the original.

**Security and data handling**

| # | Decision | Rationale |
|---|---|---|
| 0 | Never paste API keys into chat | Keys go directly into the env file |
| 1 | Display name comes from `profile.json` | Legal name reserved for background checks only |
| 3 | No real password in `profile.json` | It would be embedded in plaintext in LLM prompts |
| 4 | Stay at tier 2 until the pipeline is stable | Auto-apply runs with bypassed permissions — prompt-injection surface |
| 5 | Review tailored resumes before use | `resume_facts` pins facts but does not guarantee them |
| 17 | Skip CapSolver and (originally) Gmail MCP | Too much attack surface alongside bypassed permissions |
| 27 | Basic prompt-injection defence only | Prompts instruct the model to treat scraped input as untrusted. Minimal — not a sandbox |

**LLM and generation**

| # | Decision | Rationale |
|---|---|---|
| 6 | Free primary + cheap fallback | Original provider strategy, now superseded by D1 |
| 12 | Two-tier model strategy | Fast tier for scoring, quality tier for writing |
| 13 | High `max_tokens` for thinking models | Thinking tokens consume the budget: scoring 8192, tailoring 16384, cover 8192 |
| 19 | Banned words are warnings, not errors | Later refined: a stem-based **error** tier was added for cover letters |
| 25 | Apply uses the Claude Code CLI, not the tier-2 provider | Separate billing system; spawns a subprocess |
| 47 | Default the apply driver to the mid-tier model | The cheaper model saved per-call cost but iterated so much on iframe forms (one job: $1.58 over 14 minutes) that total spend exceeded the better model's likely budget. Fewer turns wins |
| 51 | Cover-letter quality overhaul | See Part III. Driven by an audit of 3,823 letters |

**Data and selection**

| # | Decision | Rationale |
|---|---|---|
| 7 | Location comes from `searches.yaml` | Radius and accept patterns are config-driven |
| 18 | URL normalization at discovery time | Resolves relative URLs via configured base URLs |
| 20 | Jobs without an `application_url` are manual | LinkedIn Easy Apply cannot be automated |
| 23 | Company-aware apply prioritization | Spreads applications across employers rather than exhausting one |
| 29 | Funnel config: `min_score=8`, age 14d, cap 3/30d | Replaced a score-7 threshold and soft-sort deprioritization with a hard cap enforced at acquisition |
| 30 | Explicit state machine replaces implicit status derivation | 23 canonical states, validated transitions, audit table. Three legacy status columns deprecated |
| 31 | State-machine rollout shipped | All tier-2 stages and all 6 scrapers emit transitions. Four known leak paths documented as follow-up |
| 37 | Those four leaks closed | Email-driven status changes, the manual-ATS skip, stale-lock release, and HITL re-queue all now emit transitions, using forced transitions since the from-state is not always legal |
| 45 | Embedded-ATS URL canonicalization | See Part III. ~470 affected rows; iframe forms are 2–3× slower to fill |

**Browser and apply mechanics**

| # | Decision | Rationale |
|---|---|---|
| 28 | `--strict-mcp-config` for the apply subprocess | Docker MCP Toolkit's containerized Playwright cannot access host files, silently breaking uploads |
| 32 | Per-worker resume directory | Concurrent workers were cross-polluting a shared directory of clean-filename uploads |
| 33 | Apply uses Chrome for Testing | It is the only branded Chromium build that still accepts `--load-extension` after Chrome 137 — Stable, Beta, Dev and Canary all silently reject it |
| 34 | Three HITL paths collapsed into one | ~150-line reduction; each call site now parameterizes only its differences |
| 35 | Standalone human-review server deleted | 567 lines duplicating the in-pipeline HITL flow. Parked jobs are now picked up by the next apply run |
| 36 | Patchright launch flags + stdin Done fallback | Stealth flags only, no Patchright Python API integration |
| 38 | Per-install random extension key | Defeats bulk fingerprinting of the tool's extension. Falls back to the manifest key if generation fails |
| 39 | Extension per-job tab tracking + action log | Tab-set tracking via opener chains, replacing an unreliable focus-based approach. Filtered event capture with passwords masked |
| 40 | Action log wired into the pause cycle | Closes the loop: the agent's resume prompt now states what the user actually did |
| 41 / 49 | HITL toggle surfaced, then made active, in the extension popup | Live toggle read at each pause, so it takes effect without a restart |
| 42 | Stealth init scripts via `chrome.scripting` | Declarative MAIN-world scripts and script-tag injection are both CSP-blocked on real targets |
| 43 | `launcher.py` decomposed | 3,410 → 1,843 lines, split into orchestrator / hitl / result_handlers, all re-exported for backwards compatibility. Cycles resolved by lazy imports and a bottom-of-file re-export |
| 44 | Extension settings page | Ten endpoints on the per-worker HTTP server covering integrations, Q&A editing, preferences, credentials, and ATS sessions |
| 46 | Per-ATS successful-path memoization | See Part V |
| 48 | Extension ID computed at runtime | A hardcoded ID predated the per-install random key, so the extension was never actually pinned and kept disappearing from the toolbar |
| 50 | Worker logs are line-buffered | Default 8KB block buffering made a live log look empty for 10+ minutes |

**Known technical gotchas**

- Thinking-model token budgets: a simple response needs ~30 tokens, a bullet
  rewrite needs 1,200+
- Agent log filenames use local time; database timestamps are UTC
- `llm.py` reads env vars at module import — `config.load_env()` must be called
  **before** importing it
- The package is an editable install, so source edits take effect immediately

---

# PART VIII — Current measured state

## Live database

| Metric | Count |
|---|---|
| Jobs discovered | 268 |
| Distinct companies | 219 |
| Enriched with full descriptions | 124 |
| Scored | 124 |
| Scoring ≥ 8 (the apply threshold) | **7** |
| Tailored resume generated | 2 |
| Cover letter generated | 2 |
| **Applications submitted** | **1** |
| Question bank entries | 26 |
| Question sightings recorded | **0** |

The single submitted application (a web developer role via Paylocity) was
confirmed successful.

**The funnel narrows hard at scoring: 124 scored, 7 above threshold.** Whether
that reflects an accurate scorer or an overly strict one is unknown, and it is
the difference between "the market is thin" and "the pipeline is discarding
viable roles".

**`question_sightings` is empty**, because only one application has ever run.
This means per-application question anticipation at the gate is currently inert;
novel questions surface at apply time via the human-in-the-loop path instead.
This corrects itself as real applications run and should **not** be "fixed" by
widening what the gate guesses.

## Test suite

508 passing, 3 skipped. One test file is excluded from runs because it segfaults
inside X11 window-manipulation code; the segfault predates this work and is
unrelated to it.

## Code distribution

| Area | Lines |
|---|---|
| apply (browser automation, HITL, orchestration) | 8,365 |
| discovery (13 scrapers) | 4,869 |
| scoring (score, tailor, cover, validate, render) | 4,759 |
| tracking (email classification, OTP, ghosting) | 2,226 |
| enrichment | 1,155 |
| review (the gate) | 1,053 |
| wizard (first-run setup) | 452 |

**That the apply stage is nearly twice any other area is itself a finding.** The
majority of engineering effort has gone into fighting browsers and bot
detection, not into matching quality.

---

# PART IX — Known problems and open questions

## Blocked on external setup

**Gmail OAuth is not configured.** The email-tracking and OTP-retrieval code is
written and tested but cannot run — the poller fails with a session error. It
needs a dedicated job-search Gmail account and a Google Cloud OAuth desktop
credential.

Until this exists, most of the queue — Workday, Oracle, SuccessFactors, all of
which require email verification during account creation — **cannot be completed
end to end.**

## Operational risks already identified

- Discovery session cookies expire and need periodic re-authentication through a
  scripted browser login.
- **Local Claude Code session logs on this machine contain a sudo password and
  live third-party API tokens in plaintext.** Rotation has been recommended and
  not yet performed. This is unrelated to the pipeline itself but is the most
  serious security issue currently outstanding.

## Open questions for review

1. **Cost/quota metric.** Under a subscription model, is there any reason to
   track notional API cost? Should the constraint instead be rate-limit headroom
   and wall-clock time per application?
2. **Agent-per-application vs. deterministic form-fillers.** 400–860 seconds of
   agentic browser loop per application, when 6–8 ATS families cover most of the
   queue and each has a stable form structure. Where is the right line between a
   deterministic filler and an LLM agent? The 8,365-vs-1,053 line split between
   apply and review suggests effort is concentrated in the wrong place.
3. **Dual state truth.** `jobs.state` (validated, audited) coexists with three
   deprecated status columns that live code still reads and writes. What is the
   safe migration path, and is it worth doing before scaling to 50/week?
4. **Question matching.** Hand-rolled trigram cosine, no embedding model, 26
   entries, zero recorded sightings. Will the escalation rate stay low enough to
   hit the 15-minute target as the bank grows, or is a real embedding model
   warranted?
5. **Migration strategy.** No version table; every SQL migration re-executes on
   every startup and safety rests entirely on `IF NOT EXISTS`, alongside a
   parallel Python column-registry mechanism doing `ALTER TABLE`. Acceptable at
   this scale, or a latent data-loss risk?
6. **Anti-detection maintenance burden.** Is the current posture sustainable,
   and is there a materially better one?
7. **Scoring calibration.** 7 of 124 above threshold. Too strict, correctly
   selective, or a prompt problem?
8. **Fabricated-metric risk.** The validator pins tools but not numbers. What is
   a practical automated check for invented quantitative claims in generated
   application materials?
9. **Gate escalation rate at scale.** The target is 17 applications in 15
   minutes. A batch currently shows 3 batch-wide confirmations plus one item per
   cover letter — ~20 items at target scale. Is the escalation taxonomy right,
   or are there classes of error it structurally cannot catch?

## What is planned next (rest of M9)

1. **systemd user timers.** A prepare timer running Mon/Wed/Fri early morning,
   executing the full run (which now ends at `gate`) and exiting — it must never
   submit, a property now enforced in code rather than by convention. Plus an
   OTP poller timer. Submission stays manual.
2. **Run reports.** Plain text per run: discovered N, deduped N, gated N,
   prepared N, review items by kind; after approval, submitted N, parked N with
   reasons, per-ATS outcomes. The per-ATS health computation already exists, as
   do the prepared/gated counts.
3. **Per-stage JSONL logs.** The run report is the summary; the JSONL is the
   truth.

---

# PART X — Repository map and runbook

## Source layout

```
src/applypilot/
  cli.py               Typer CLI: run, apply, status, track, dashboard,
                       review, otp, parked, qa, creds, init
  pipeline.py          Stage orchestration, sequential and streaming
  database.py          Schema, migrations, state machine, stats
  config.py            Paths, defaults, tiers, per-company caps
  llm.py               Provider cascade; the claude_cli provider
  questions.py         Trigram similarity, confidence, decide()
  voice.py             Voice profile loading, batch sameness detection
  view.py              Stage derivation for status/dashboard

  discovery/           13 source scrapers + URL normalization
  enrichment/          Detail scraping, embedded-ATS canonicalization
  scoring/             scorer, tailor, cover_letter, validator, pdf, json_resume
  review/              batch.py    — escalation rules and approval
                       prepare.py  — database → batch assembly
                       tui.py      — one-key review interface
  apply/               orchestrator, launcher, chrome, prompt, hitl, pacing,
                       result_handlers, successful_paths, dashboard, human_review
  tracking/            gmail_client, classifier, triage, otp, matcher,
                       ghosting, markdown_gen
  wizard/              First-run setup

migrations/            001 question bank + review gate + parked + email events
                       002 apply hardening (proof, cooldowns)
                       003 review batch membership
extension/             Chrome extension: tab tracking, action log, stealth
                       injection, noise filter, popup, options page
scripts/               hc_login, gmail_oauth, install_cft, smoke tests
tests/                 508 tests
```

## Runtime data (outside the repository)

```
~/.applypilot/
  profile.json           Candidate PII, mode 600
  resume.txt / .pdf      Master resume
  candidate-dossier.md   Full evidence base for generation
  applypilot.db          The database
  searches.yaml          Search configuration
  company_limits.yaml    Per-company application caps
  tailored_resumes/      Generated, per job
  cover_letters/         Generated, per job (.txt source + converted file)
  logs/                  Per-run and per-worker logs
  chrome-for-testing/    The browser build used for applying
  hiringcafe-profile/    Persistent logged-in Chromium profile for discovery
  successful_paths/      Learned tool-call sequences, per ATS

~/Documents/Repositories/voice-profile/    Read-only voice core + registers
~/.gmail-mcp/                              OAuth keys and token (not yet created)
```

## Operating loop

```bash
applypilot status                  # where is the funnel
applypilot run                     # discover → … → gate  (never submits)
applypilot review                  # the human gate; approval is the only trigger
applypilot apply                   # submit what was approved
applypilot track                   # classify inbound responses
applypilot parked                  # inspect and resolve the parked queue
```

Individual stages: `applypilot run discover enrich`, `applypilot run gate`, etc.

Tests:

```bash
.venv/bin/python -m pytest -q tests --ignore=tests/test_extension_server.py
```

## Error-handling posture

- A stage that fails is stopped and diagnosed rather than retried blindly
- Above ~30% failure rate in any stage, stop and fix before continuing
- Provider rate limits fall through the cascade automatically
- Subscription exhaustion cannot be auto-fixed and alerts the user

---

# Note to the reviewer

The most useful feedback would be on Part IX questions **1, 2, and 7**, which
between them determine whether the 50-applications-per-week target is reachable
at all and whether effort is currently going into the right part of the problem.

Context that may inform recommendations: this is a single candidate's personal
job search, not a product. The person running it graduates in December 2026 and
has consistently chosen honest positioning over volume — a stated goal that has
already driven concrete reductions in scope and output length rather than
increases.
