-- Submission proof and per-ATS health.
--
-- Section 11 requires every submission to record proof, and requires an ATS
-- whose success rate falls below ~50% over a rolling 30 submissions to be
-- flagged at the next review gate with a recommendation to move it to
-- prep-only. Both need durable per-attempt records, which the jobs table
-- cannot hold (it keeps only the latest attempt).

CREATE TABLE IF NOT EXISTS submission_proof (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id    TEXT NOT NULL,        -- jobs.url
    ats               TEXT,
    worker_id         INTEGER,
    outcome           TEXT NOT NULL,        -- applied|failed|parked|skipped
    screenshot_path   TEXT,
    confirmation_text TEXT,
    confirmation_id   TEXT,
    duration_ms       INTEGER,
    submitted_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proof_application ON submission_proof(application_id);
CREATE INDEX IF NOT EXISTS idx_proof_ats ON submission_proof(ats, submitted_at);

-- Per-ATS cooldowns. A 403, 429, or Turnstile means back off that ATS for the
-- rest of the run rather than hammering it with the remaining applications.
CREATE TABLE IF NOT EXISTS ats_cooldowns (
    ats          TEXT PRIMARY KEY,
    reason       TEXT NOT NULL,
    cooled_at    TEXT NOT NULL,
    expires_at   TEXT
);
