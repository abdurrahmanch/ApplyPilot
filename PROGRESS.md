# PROGRESS.md

**Rewritten at the end of each working session** (ARCHITECTURE §8). Read this
first, then `ARCHITECTURE.md` for the design and `DECISIONS.md` for why.

**Last updated:** 2026-08-20, end of the first live submission session.

---

## Current state

**6 applications submitted and confirmed by email.** First time the pipeline
has put real applications in front of real employers at any volume.

| Fit | Employer | Role | Confirmation |
|---|---|---|---|
| 9 | BisectHosting | Web Developer | yes (Aug 19) — **rejected 2h later** |
| 9 | IMEG | Full Stack Software Engineer | yes |
| 8 | 10a Labs | Software Engineer, Infrastructure | none (Greenhouse often sends none) |
| 8 | Kindred | Full-Stack Engineer | yes |
| 7 | BisectHosting | Backend Engineer, Multiplayer | yes |
| 6 | Bixal | DevOps Engineer – Data Platform | yes |

**Tests: 574 passed.**
```bash
.venv/bin/python -m pytest -q tests --ignore=tests/test_extension_server.py
```
`test_extension_e2e.py` fails **only while an apply run holds port 9222** — it
launches its own Chrome. Passes in isolation. `test_extension_server.py`
segfaults in X11 code, pre-existing.

**Mode changed: the pipeline no longer auto-submits.** Operator decided
2026-08-20 that every application is *prepared* and left for him to review and
send by hand. More defensible under ATS terms (most are written against
automated submission, not against preparing a form), and it dissolves the
verification problem — he sees what goes out because he sends it.

```bash
applypilot apply --fill-only --limit 20 --min-score 4 --workers 1 --no-hitl
```

One window, one tab per application, every field filled, nothing submitted.

---

## Next up

1. **Run fill-only across the remaining 19.** Everything is fixed and tested
   but this had not been re-run when the session ended. Verify on the first run
   that the window is genuinely left open at the end.
2. **A live OTP is waiting.** `myworkday@cmegroup.com — "Verify your candidate
   account"`. CME Group's Workday account was created but never verified.
3. **Submission proof captures nothing.** Every `submission_proof` row has null
   `confirmation_text`, `screenshot_path` and `verification_confidence`. The
   table fills, the evidence does not. Matters less now the operator submits by
   hand, but it is why "did these actually send?" had to be answered from Gmail
   rather than from the database.
4. **Gate tailor on `application_url IS NOT NULL`.** `apply_url.py` recovered
   25 of 27 after the fact, but tailor and cover still spend a full generation
   on rows with no apply link — 44% of one run's quota went that way.
5. **Two jobs still have no apply link** — B. Riley Wealth (saashr) and CGI
   (njoyn). ASP-era boards where Apply leads to a login wall. Genuinely manual.
6. **Scoring calibration** (§4) — still unresolved, still needs a human to read
   20 sub-threshold rejects.
7. **Batched tailor persistence.** `run_tailoring` flushes every result after
   the whole loop; a crash at job 24 orphans 24 resumes with no DB rows.

---

## Blocked

- **Credential rotation.** The sudo password and live third-party tokens in
  `~/.claude/projects` are still unrotated. **`worker-0.log` now also contains
  an ATS account password in plaintext**, and every Workday account created
  writes another. Fix at source — mask it in `_parse_account_created`.
- **The repo is public and now says so.** `DECISIONS.md` and `PROGRESS.md`
  state that this machine has plaintext secrets in its logs. Accurate, but
  worth not advertising until rotated.
- **Workday is unblocked in `sites.yaml`** for the OTP experiment, with
  `RESTORE THIS LINE` markers on both entries. The experiment **succeeded** —
  account creation, resume upload, form fill and a real submission (IMEG) all
  worked — so leaving it unblocked is defensible. Backup at
  `/tmp/sites.yaml.bak`. No final call from the operator.

---

## Recently finished

- **Fill-only mode** (`--fill-only`): fills everything, stops before Submit,
  leaves each tab open, leaves CAPTCHAs for the human, marks
  `ready_for_review`. `RESULT:READY_FOR_REVIEW` parses before `APPLIED` so
  narration cannot promote a prepared form into a sent one.
- **One persistent window, a tab per application.** Chrome had been launched
  and killed per job — a window per application and a cold profile every time,
  which is both a bot signal and why a solved captcha never carried forward.
- **Chrome for Testing installed.** Decision #33 specified it in April; it was
  never present, and `get_chrome_path()` silently used Arch's system Chromium.
- **CAPTCHA now cools its ATS.** `cool_down_ats` was wired only to the `failed`
  branch, so a captcha park never triggered it — one Greenhouse challenge
  became six, because each park sent the next attempt straight back.
- **Dry runs no longer mark jobs applied.** `mark_result(..., "applied")` ran
  regardless of `dry_run`; only proof recording checked it. CAI sat in the
  submitted tally for hours after being nothing but a test.
- **The agent no longer invents degrees.** It selected "Associates of Science"
  for College of DuPage, which awarded nothing, reasoning it was the closest
  match. Rule 2 is now a procedure, and `resume_facts.education_truth` states
  the fact outright. Confirmed: transfer credits to DePaul, no degree.
- **Blocked employers were reaching batches.** `sites.yaml` lists `google`, the
  scraper stores `Google`, SQLite `!=` is case-sensitive. Google was one
  approval away from an application.
- **25 of 27 apply URLs recovered** (`enrichment/apply_url.py`). The cause was
  a wrong assumption: on Greenhouse/Lever/Ashby the listing page *is* the form,
  so the extractor hunted for a link that never existed.
- **Gmail via browser session** — `scripts/gmail_login.py` +
  `tracking/gmail_browser.py`, read-only. This is what confirmed the six
  submissions actually landed.
- **Cover letters retuned** to the operator's voice: warm plain opening, no
  lecturing the company about its own business, human close ("I would love to
  sit down with you"), LLM work framed as directing multi-agent workflows
  rather than naming a tool.
- **Score floor is 4, not 8** (D25) — the curated hiring.cafe queries are the
  real keyword filter.

---

## Hard-won lessons worth not relearning

- **An empty inbox is not evidence.** Gmail showed zero mail and the obvious
  read was "nothing submitted". The real cause was a broad `pkill` that had
  killed the Gmail session. Check the session before believing the silence.
- **Never `pkill -9 -f chrome` or `-f applypilot` broadly.** It takes out the
  Gmail profile mid-write and destroys its auth cookies.
- **Launch long runs with `setsid`.** A `nohup ... &` inside a tool call dies
  when the call times out — it killed the worker threads of a tailoring run and
  left the parent alive doing nothing.
- **`applypilot apply` defaults to `min_score=8`.** The prepare stages moved to
  4; the submit command did not, and a run reported "queue empty" after three
  jobs.
- **The operator's name is `Abdur-Rahman Ch`**, not all-caps. `profile.json`
  had `ABDUR-RAHMAN CH` and it reached one submitted application. The pdf stage
  skips already-converted files, so fixing the `.txt` is not enough — delete
  the `.docx` to force regeneration, then verify inside the document.
- **Verify a fix by its effect, not its log line.** The first name-fix pass
  reported success and changed nothing, because conversion was skipped.
