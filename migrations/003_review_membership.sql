-- Batch membership for the review gate.
--
-- Before this table, a batch's membership was inferred from `review_items`,
-- which silently dropped the best case: an application whose questions all
-- auto-answered and whose cover letter raised no delta produces zero items,
-- so it was never "in" the batch and could never become submittable. The gate
-- would have blocked exactly the applications it had no objection to.
--
-- Membership is now recorded explicitly, once per application per batch.
-- `review_items` keeps its job: what needs Abdur-Rahman's eyes, nothing more.

CREATE TABLE IF NOT EXISTS review_batch_applications (
    batch_id       INTEGER NOT NULL REFERENCES review_batches(id) ON DELETE CASCADE,
    application_id TEXT NOT NULL,          -- jobs.url
    status         TEXT NOT NULL DEFAULT 'ready',   -- ready|disqualified|parked
    title          TEXT,
    company        TEXT,
    reason         TEXT,                   -- why, for disqualified/parked
    added_at       TEXT,
    PRIMARY KEY (batch_id, application_id)
);

CREATE INDEX IF NOT EXISTS idx_rba_application
    ON review_batch_applications(application_id);
