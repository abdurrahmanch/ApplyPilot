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
