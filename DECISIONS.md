# DECISIONS.md

Decision log for Arch's job-application pipeline (fork of `ibarrajo/ApplyPilot`).
Entries are append-only. Each records what was verified or decided, and why.

---

## 2026-08-19 — M0: fork + verification pass

### Fork state

- Forked `ibarrajo/ApplyPilot` → `abdurrahmanch/ApplyPilot`, cloned to
  `~/Documents/Repositories/ApplyPilot`. Remotes: `origin` (fork),
  `upstream` (ibarrajo), `pp` (Pickle-Pixel).
- **`git log pp/main..HEAD` = 151 commits ahead, `HEAD..pp/main` = 0 behind.**
  CLAUDE.md §2 asks to cherry-pick v0.3.0 hardening from Pickle-Pixel.
  **No-op:** the fork already contains everything upstream has.
  - SQL-injection concern: the only f-string SQL is `apply/launcher.py:1032-1042`
    (Q&A review server). The interpolated `where_sql` is assembled from static
    literals; every value is bound via `?`. Not injectable. No fix needed.
  - `--validation lenient/normal/strict`: **does not exist** in either repo.
    `scoring/validator.py` is pure Python (regex/set checks) with no LLM judge,
    so CLAUDE.md §6's "use `--validation lenient` to skip the LLM judge" is
    moot — there is no judge call to skip and no per-job saving available.
    Dropped from the M3 scope; noted here instead of building a flag that
    gates nothing.

### §3 [VERIFY] answers

1. **`searches.yaml` schema** — `src/applypilot/config/searches.example.yaml`.
   Keys: `queries[] {query, tier}`, `locations[] {location, remote}`,
   `location.{accept_patterns, reject_patterns}`, `country`, `boards[]`
   (jobspy sources), `defaults.{results_per_site, hours_old}`,
   `exclude_titles[]`. Filter support (accept/reject/exclude) is real.
   Irrelevant to us: this drives the jobspy path, which §5 disables.
2. **LLM configuration** — `src/applypilot/llm.py`.
   Env vars: `GEMINI_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`,
   `ANTHROPIC_API_KEY`, `LLM_URL`/`LLM_API_KEY`, `LLM_MODEL` (fast tier),
   `LLM_MODEL_QUALITY` (quality tier).
   Two-tier chains are built by `_build_fallback_chain(primary, quality=bool)`;
   order is Gemini → OpenAI → DeepSeek → Anthropic (Anthropic last).
   All stages funnel through one method: `LLMClient.chat(messages) -> str`,
   dispatching per-provider in `_try_entry`.
3. **Apply-stage selection SQL** — `database.py:get_jobs_by_stage()`.
   Gates on `fit_score >= ?` for `pending_tailor` / `pending_cover`
   (plus `eligibility IS NULL OR eligibility = 'eligible'`).
   `pending_apply` gates on `tailored_resume_path IS NOT NULL AND applied_at IS
   NULL AND application_url IS NOT NULL` — **not** on score (the score gate is
   already upstream of it).
   **Default `min_score` = 8**, not 7 (`config.py:192`, changed from 7 per a
   2026-04-23 funnel spec).
   `ORDER BY` **does exist**: `fit_score DESC NULLS LAST, _site_rank ASC,
   discovered_at DESC`, where `_site_rank` is a `ROW_NUMBER()` partitioned by
   `site` for round-robin fairness across sources.
   Fully parameterized.
4. **Q&A table** — `qa_knowledge(id, question_text, question_key, answer_text,
   answer_source, field_type, options_json, ats_slug, job_url, outcome,
   created_at, updated_at)`, `UNIQUE(question_key, answer_text)`.
   Matching is **exact-hash only**: `question_key = md5(lowercased,
   punctuation-stripped, whitespace-collapsed text)`. `lookup_qa()` ranks
   collisions by `outcome` (accepted > unknown > rejected) then recency.
   **No embeddings, no confidence, no similarity.** Confirms CLAUDE.md §8:
   the whole confidence/embedding layer is ours to build (M4).
5. **Gmail tracking** — `tracking/gmail_client.py`; OAuth lives in
   `~/.gmail-mcp/gcp-oauth.keys.json` (app keys) + `~/.gmail-mcp/credentials.json`
   (token), obtained via `npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth`.
   Pipeline (`tracking/__init__.py`): fetch → pure-Python triage
   (confirmation / rejection / noise) → LLM classify only the ambiguous,
   interview, and offer cases → `tracking_emails.classification` →
   `update_tracking_status`. The `otp` class (§12) slots into the triage step
   as a pure-Python pattern match, before any LLM call.
6. **Apply spawn** — `apply/launcher.py:2065`. Per worker:
   `claude --model <m> -p --mcp-config .mcp-apply-<id>.json --strict-mcp-config
   --permission-mode bypassPermissions --no-session-persistence
   --disallowedTools <...> --output-format stream-json --verbose -`.
   MCP config (`_make_mcp_config`) wires `@playwright/mcp@0.0.75` over a CDP
   port plus the Gmail MCP server (all Gmail write tools blocked).
   Browser session/profile persistence is per-worker via the CDP port and
   Chrome user-data-dir managed in `apply/chrome.py`.

### Decisions

- **D1 — No Anthropic API key; all LLM stages run through the Claude Code CLI.**
  Arch has no API key and wants the existing Claude Code subscription used.
  This overrides CLAUDE.md §1's "Anthropic API for score/tailor/cover".
  Implementation (M1): add a `claude_cli` provider to `llm.py` that shells out
  to `claude -p --model <m> --output-format json`, flattening the `messages`
  list into a single prompt (system → prepended block). It becomes the sole
  fallback chain when no provider API keys are present, so every existing
  caller of `LLMClient.chat()` keeps working unchanged.
  Tiering maps to `--model`: fast tier for scoring, quality tier for
  tailor/cover.
  Trade-off accepted: per-call process spawn (~1-2s overhead), no token-level
  cost accounting. Scoring is therefore batched (N jobs per invocation) to keep
  the spawn count near the job count / batch size.
- **D2 — `--validation lenient` dropped** (see above; the flag does not exist
  and gates no LLM call).
- **D3 — Recency ordering (§6) requires a new column.** The `jobs` table has
  `discovered_at` but **no `date_posted`**. hiring.cafe supplies a real posting
  date, so M2 adds `date_posted` via the migration runner and M3 reorders
  selection to `date_posted DESC` with `fit_score` as tiebreaker.
- **D4 — `min_score` floor.** Upstream default is 8. Per §6 (low sanity floor,
  curated queries do the real filtering) runs will pass `--min-score 4`
  explicitly rather than editing the shipped default, so upstream behaviour
  stays intact for any dry runs.

### hiring.cafe searchState — real schema vs CLAUDE.md §5

Both blobs captured byte-exact from Arch's live saved searches and committed as
`queries/hybrid-chicago.json` ("Default") and `queries/remote-usa.json`
("Remote"). §5's field descriptions were wrong on four points; the live blobs
are authoritative:

| CLAUDE.md §5 claims | Actual |
|---|---|
| `lat`/`lon` are strings | floats (`41.85003`, `-87.65005`) |
| `options.flexible_regions` expansion tokens | no such key; `options` = `{radius: 100, radius_unit: "miles", ignore_radius: false}` |
| top-level `workplaceTypes` | per-location `workplace_types` |
| `searchQuery` | `jobTitleQuery` (134 quoted titles, newline-separated) |

Only four top-level keys are present: `locations`, `dateFetchedPastNDays` (4),
`roleYoeRange` (`[0, 5]`), `jobTitleQuery`. No `seniorityLevel`,
`commitmentTypes`, `securityClearances`, or salary fields in Arch's searches.

`remote-usa.json` was built from the Default blob plus the third location entry
(`United States` / `workplace_types: ["Remote"]`) taken verbatim from Arch's
Remote URL; every other field is identical between the two searches.
**Open verification:** confirm byte-equivalence against the live UI at M2 by
loading the committed blob back into hiring.cafe and comparing result counts.

**Known overlap:** the Remote query is a strict superset of Default — it retains
the Chicago and Illinois entries at all workplace types. Dedupe absorbs the
duplicates, but per-query yield stats will be muddied until Arch decides whether
to strip those entries. Question raised 2026-08-19; awaiting answer.

### Voice profile

Arch supplied `voice-profile.md` v1.2 directly (it was not on this machine).
Stored read-only (mode 444) at `~/Documents/Repositories/voice-profile/voice-profile.md`
with an empty `registers/` alongside it. `registers/job-applications.md` gets
seeded at M5 per §10. No reconstruction from memory was performed.

---

## 2026-08-19 — M2 / M3

### hiring.cafe: the documented API no longer exists

`POST /api/search-jobs` returns 405 and `/api/search-jobs/get-total-count`
returns 404. The host is `hiringcafe.com`, the site is a Firebase-gated Next.js
app, and search results come from the page-data route:

    GET /_next/data/{buildId}/index.json?searchState={json}&page={n}
    headers: x-nextjs-data: 1

- `buildId` (`window.__NEXT_DATA__.buildId`) changes on every hiring.cafe
  deploy and is scraped per run, never cached.
- The route requires an authenticated session, so requests are issued from
  inside a persistent logged-in Chromium profile at
  `~/.applypilot/hiringcafe-profile`, established once by `scripts/hc_login.py`.
  This collapses the section 5 fallback ladder: rung 1 (plain HTTPS) is not
  reachable at all now, so the browser path is the primary, not the fallback.
- Pagination is `&page=N`, terminated by `ssrIsLastPage`.
- **Descriptions are not inline.** Neither the search response nor the per-job
  route (`/_next/data/{buildId}/jobs/{slug}.json`) carries posting text. This
  contradicts section 5. Rows land in `discovered` and the enrich stage scrapes
  `apply_url`; enrich is REQUIRED for this source, not a fallback.

### D5 — Scoring is batched, and batching is load-bearing

Measured with the CLI provider: a single trivial call costs ~$0.018 because
each `claude -p` invocation re-sends ~26k tokens of system prompt. One call per
job at ~1,400 jobs/month is ~$25/month, over the section 6 ceiling of $10.

Batched at `SCORE_BATCH_SIZE = 10`, a real batch measured **$0.0632 for 10 jobs
= $0.0063/job ≈ $9/month**. Batching is therefore a budget requirement, not an
optimization. The batched path runs when `workers <= 1`; the threaded and
sequential per-job paths are untouched and still available.

A malformed batch degrades only its own jobs: any job whose block is missing
from the response comes back `score=None` and re-enters the existing
retry/backoff path on the next run.

### D6 — The scoring prompt was written for a different person

The inherited prompt hardcoded "The candidate is US-based (Seattle, WA)", a
Go/Kotlin/K8s senior stack, and a Seattle-area commute rule, and reserved its
top scores for Senior/Staff/Principal roles — the exact inverse of what
Abdur-Rahman needs. Rewritten:

- Candidate location now comes from `profile.json` rather than a literal.
- Score bands target entry-level/new-grad/junior IC roles on his stack
  (Java/Spring Boot/PostgreSQL, TypeScript/React/Next.js).
- Senior/Staff/Principal/Lead/Manager titles are a hard 1-2, not a top score.
- Roles requiring an active security clearance are a hard 1-2.
- Years-of-experience bars are read literally: 0-3 ideal, 4-5 capped at 6,
  6+ capped at 3.
- Commute rule is Chicago metro; fully remote US roles are unrestricted.

Verified on 20 real jobs: senior titles scored 1-2, "Web Developer" 9,
"Backend Engineer, Multiplayer" 7. No parse errors.

### D7 — Description truncation

`SCORE_DESC_CHARS = 1800` (~300 words) per section 6, down from 6000. The
tailor stage still reads the full description; that is where a disqualifier
missed by truncation surfaces.

### D8 — Recency ordering shipped

`get_jobs_by_stage` now orders by `COALESCE(posted_at, discovered_at) DESC`
first, with `fit_score DESC` as tiebreaker, and the per-company `ROW_NUMBER()`
window reordered to match. Section 6 wanted `date_posted`; the column already
exists as `posted_at` and hiring.cafe populates it from
`estimated_publish_date`, so no migration was needed (this supersedes D3).

### Query overlap resolved

A live run proved the overlap: `remote-usa` returned 266 rows and
`hybrid-chicago` added 2, because the remote query kept the Chicago and
Illinois entries at all workplace types. Abdur-Rahman approved stripping them;
`remote-usa.json` now carries only the United States / Remote location.

### Candidate data

`~/.applypilot/profile.json` (mode 600) and `~/.applypilot/resume.txt` are
populated from his master resume plus values he supplied directly. A unique
24-char password was generated for ATS accounts and written only to
profile.json. `~/.applypilot/candidate-dossier.md` holds the full evidence
base, including the binding constraint that Anasheed is schema-and-design only
and must never be described with implementation verbs.

### Naming

He goes by **Abdur-Rahman**, not "Arch" as this document's section 1 uses, and
his surname is written **"Ch"** only — deliberately truncated to limit
doxxing. Never expand it in generated output; park any form demanding a full
legal surname.

---

## 2026-08-19 — M9 part 1: the gate becomes binding

The review gate was written, tested, and disconnected. Nothing called
`build_batch`, so no batch was ever assembled; and `acquire_job` selected
straight from the jobs table, so approving a batch changed nothing about what
went out. Approval was documentation, not control. Both ends are now wired.

### D9 — A `gate` stage closes the prepare run

`STAGE_ORDER` gains a seventh stage, `gate`, downstream of `pdf`. It calls
`review.prepare.build_run_batch` and never submits. `applypilot run` therefore
ends at the human touchpoint by default instead of quietly running past it,
which is the property the unattended timers in the rest of M9 depend on.

In streaming mode the gate is special-cased: it waits for its upstream to
finish and then runs exactly once. Treating it like a conveyor stage would
build one batch per polling pass and split a single run's work across several
of them.

### D10 — Batch membership is recorded, not inferred

`submittable_applications` derived a batch's membership from `review_items`,
which inverted the intent in the most common case. An application whose
questions all auto-answered and whose cover letter raised no delta produces
zero items — so it was never "in" the batch and could never be cleared. The
gate blocked exactly the applications it had no objection to.

`migrations/003_review_membership.sql` adds `review_batch_applications`, one
row per application per batch. `review_items` keeps its narrower job: what
needs his eyes. Batches predating the table fall back to the old derivation.

### D11 — Sensitive questions are batch-scoped, one per category

Section 9 requires work auth, sponsorship, and salary in every batch forever.
Read literally — every stored phrasing, against every application — the first
live run produced 11 items for a single application. At 17 applications that is
187 confirmations of the same three answers, and the gate's whole design target
is 15 minutes.

So `sensitive_questions` returns one question per sensitive *category*, and
`build_batch` files them with `application_id = NULL`. `submittable_applications`
treats any unresolved application-less item as blocking the entire batch, which
is what "the answer applies to all of it" actually means. The live batch went
from 12 items to 4.

### D12 — The gate mirrors `acquire_job`, including the manual-ATS skip

A batch that clears work the submit phase would refuse is a batch that lies.
The first live run cleared an IMEG Workday role that `acquire_job` then marked
`manual_only` on sight, because its host is on the manual-ATS skip list. The
readiness query now applies the same predicate — plus open `parked` rows and
terminal failure states — and diverts those to one awareness line each.

### D13 — Approval is binding, with two named escape hatches

`acquire_job(require_gate=True)` is the default. With nothing cleared it
returns None, and `apply.main` refuses before any browser launches, naming
which of the two reasons applies (no batch built, or one still awaiting him).
Two bypasses stay open and are deliberate:

- `--no-gate` — explicit, labelled in the launch banner as unreviewed.
- an explicit `target_url` — naming one job is itself a human decision, and it
  is the path the crash-reconnect probe uses to finish a job that was already
  cleared before the run died.

`cleared_urls` unions across every opened batch rather than reading only the
newest, so a partial batch whose held-back items are resolved later is not
invalidated by the next run, and an interrupted apply run resumes. A batch is
marked `submitted` only once every application it cleared has actually gone
out.

### Cover letters are read from the `.txt` beside the converted file

`cover_letter_path` points at the `.pdf` after the pdf stage rewrites it. The
gate reads the sibling `.txt`. Without that fallback every batch would have
shown zero cover deltas and the batch-wide sameness check would never fire —
silently, since a batch with no cover items looks like a clean batch.
