-- Question bank, review gate, parked queue, and email events.
--
-- Extends the upstream conveyor tables rather than replacing them. The
-- upstream `qa_knowledge` table stays in place and keeps working: it is an
-- exact-hash store (md5 of the normalized question) with no similarity, no
-- confidence, and no notion of a question being too sensitive to auto-answer.
-- The tables here add those, and `questions.legacy_qa_id` links back so the
-- existing apply-stage lookup path can be migrated incrementally.
--
-- Every statement is IF NOT EXISTS. This file is applied on every startup.

CREATE TABLE IF NOT EXISTS questions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_text   TEXT NOT NULL,
    embedding        BLOB,             -- serialized sparse vector, see questions.py
    canonical_answer TEXT NOT NULL,
    category         TEXT NOT NULL,    -- work_auth | sponsorship | salary | start_date |
                                       -- relocation | degree_yoe | background_check |
                                       -- eeo | personal_fact | free_text | other
    sensitive        INTEGER NOT NULL DEFAULT 0,   -- 1 = never auto-answer, any confidence
    confidence       REAL NOT NULL DEFAULT 0.5,    -- 0..1, cut on every correction
    times_seen       INTEGER NOT NULL DEFAULT 0,
    times_corrected  INTEGER NOT NULL DEFAULT 0,
    legacy_qa_id     INTEGER,          -- qa_knowledge.id this was migrated from
    created_at       TEXT NOT NULL,
    updated_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_questions_category ON questions(category);
CREATE INDEX IF NOT EXISTS idx_questions_sensitive ON questions(sensitive);

-- One row per time a question was actually seen on a form. This is the audit
-- trail that makes threshold tuning possible: it records what was answered,
-- whether the system chose it without asking, and whether a human overrode it.
CREATE TABLE IF NOT EXISTS question_sightings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id     INTEGER REFERENCES questions(id) ON DELETE CASCADE,
    application_id  TEXT,              -- jobs.url
    raw_text        TEXT NOT NULL,     -- the question exactly as the form worded it
    ats             TEXT,
    answer_given    TEXT,
    auto_answered   INTEGER NOT NULL DEFAULT 0,
    corrected       INTEGER NOT NULL DEFAULT 0,
    correction_text TEXT,
    similarity      REAL,              -- match score against the canonical, if any
    seen_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sightings_question ON question_sightings(question_id);
CREATE INDEX IF NOT EXISTS idx_sightings_application ON question_sightings(application_id);

-- A batch is one prepared run awaiting review. Submission is gated on
-- status = 'approved' and nothing else.
CREATE TABLE IF NOT EXISTS review_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|submitted|partial
    approved_at TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS review_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       INTEGER NOT NULL REFERENCES review_batches(id) ON DELETE CASCADE,
    application_id TEXT,               -- jobs.url
    kind           TEXT NOT NULL,      -- novel_question | low_confidence | never_auto_guess |
                                       -- cover_delta | sameness_warning | disqualified |
                                       -- parked | high_value
    payload_json   TEXT,
    resolution     TEXT,
    resolved_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_items_batch ON review_items(batch_id);

-- Work that stopped for a human reason. The run continues; nothing waits.
CREATE TABLE IF NOT EXISTS parked (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL,      -- jobs.url
    reason         TEXT NOT NULL,      -- captcha|mfa|login|otp_wait|form_error|
                                       -- unanswerable_question|other
    details_json   TEXT,
    parked_at      TEXT NOT NULL,
    resolved_at    TEXT,
    outcome        TEXT
);

CREATE INDEX IF NOT EXISTS idx_parked_unresolved ON parked(resolved_at);

-- Classified inbound mail, including the OTP class that closes the
-- account-creation loop on Workday/Oracle-style ATSes.
CREATE TABLE IF NOT EXISTS email_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_msg_id    TEXT UNIQUE,
    classified_as   TEXT NOT NULL,     -- confirmation|rejection|interview|assessment|
                                       -- offer|otp|other
    company_guess   TEXT,
    application_id  TEXT,              -- jobs.url
    otp_code        TEXT,
    otp_consumed_at TEXT,
    received_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_email_events_class ON email_events(classified_as);
CREATE INDEX IF NOT EXISTS idx_email_events_otp ON email_events(classified_as, otp_consumed_at);
