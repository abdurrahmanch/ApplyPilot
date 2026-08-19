"""Tests for the question bank: matching, thresholds, and the learning loop.

The escalation rules here are the difference between a considered answer and a
silent permanent reject, so they are tested behaviourally: what does the system
DO with this question, not what score did it compute.
"""
import sqlite3
import unittest
from pathlib import Path

from applypilot.questions import (
    MIN_TIMES_SEEN_FOR_AUTO,
    REUSE_THRESHOLD,
    REVIEW_FLOOR,
    add_question,
    apply_correction,
    category_hint,
    decide,
    find_match,
    normalize,
    record_sighting,
    similarity,
    threshold_report,
)
from applypilot.questions_seed import build_seed_entries, seed_question_bank

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "001_question_bank.sql"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(MIGRATION.read_text())
    return conn


PROFILE = {
    "personal": {"city": "Lombard", "province_state": "Illinois"},
    "work_authorization": {"legally_authorized_to_work": "Yes",
                           "require_sponsorship": "No",
                           "work_permit_type": "US Citizen"},
    "availability": {"earliest_start_date": "December 2026"},
    "compensation": {"salary_expectation": "85000", "salary_currency": "USD",
                     "salary_range_min": "80000", "salary_range_max": "110000"},
    "experience": {"education_level": "Bachelor's Degree",
                   "years_of_experience_total": "2"},
    "eeo_voluntary": {"gender": "Male", "race_ethnicity": "Asian",
                      "veteran_status": "I am not a protected veteran",
                      "disability_status": "No, I do not have a disability"},
}


class TestNormalize(unittest.TestCase):
    def test_folds_united_states_variants(self):
        a = normalize("Are you authorized to work in the United States?")
        b = normalize("Are you authorized to work in the US?")
        self.assertEqual(a, b)

    def test_folds_now_or_in_the_future(self):
        a = normalize("Will you now or in the future require sponsorship?")
        b = normalize("Do you require sponsorship?")
        self.assertEqual(a, b)

    def test_strips_punctuation_and_case(self):
        self.assertEqual(normalize("Salary?!  EXPECTATIONS "), "salary expectations")


class TestSimilarity(unittest.TestCase):
    def test_identical_questions_score_one(self):
        q = "Are you legally authorized to work in the United States?"
        self.assertAlmostEqual(similarity(q, q), 1.0, places=6)

    def test_reworded_authorization_question_matches(self):
        self.assertGreaterEqual(
            similarity("Are you legally authorized to work in the United States?",
                       "Are you legally authorized to work in the US?"),
            REUSE_THRESHOLD)

    def test_unrelated_questions_score_low(self):
        self.assertLess(
            similarity("Are you authorized to work in the US?",
                       "Describe a time you resolved a conflict."),
            REVIEW_FLOOR)

    def test_empty_text_is_safe(self):
        self.assertEqual(similarity("", "anything"), 0.0)


class TestDecide(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def test_empty_bank_returns_novel(self):
        d = decide(self.conn, "Are you authorized to work in the US?")
        self.assertEqual(d["action"], "novel")
        self.assertIsNone(d["answer"])

    def test_sensitive_category_never_auto_answers(self):
        """A perfect match on a sensitive question still requires confirmation."""
        q = "Are you legally authorized to work in the United States?"
        qid = add_question(self.conn, q, "Yes", category="work_auth",
                           times_seen=500)
        self.conn.execute("UPDATE questions SET times_seen = 500 WHERE id = ?", (qid,))
        d = decide(self.conn, q)
        self.assertEqual(d["action"], "confirm")
        self.assertEqual(d["answer"], "Yes")

    def test_unproven_entry_is_confirmed_not_auto(self):
        q = "How did you hear about us?"
        add_question(self.conn, q, "Online job board", category="other", times_seen=0)
        self.assertEqual(decide(self.conn, q)["action"], "confirm")

    def test_proven_non_sensitive_entry_auto_answers(self):
        q = "How did you hear about us?"
        qid = add_question(self.conn, q, "Online job board", category="other")
        self.conn.execute("UPDATE questions SET times_seen = ? WHERE id = ?",
                          (MIN_TIMES_SEEN_FOR_AUTO, qid))
        d = decide(self.conn, q)
        self.assertEqual(d["action"], "auto")
        self.assertEqual(d["answer"], "Online job board")

    def test_a_corrected_entry_never_auto_answers_again(self):
        q = "How did you hear about us?"
        qid = add_question(self.conn, q, "Online job board", category="other")
        self.conn.execute("UPDATE questions SET times_seen = 99 WHERE id = ?", (qid,))
        self.assertEqual(decide(self.conn, q)["action"], "auto")

        apply_correction(self.conn, qid, "Company website")
        d = decide(self.conn, q)
        self.assertEqual(d["action"], "confirm")
        self.assertEqual(d["answer"], "Company website")

    def test_weak_match_below_floor_is_novel(self):
        add_question(self.conn, "How did you hear about us?", "Online job board",
                     category="other")
        d = decide(self.conn, "Describe a time you disagreed with a coworker.")
        self.assertEqual(d["action"], "novel")

    def test_terse_sensitive_question_prefills_rather_than_parking(self):
        """"Desired salary?" is too short to match on n-grams but must not be novel."""
        add_question(self.conn, "What are your salary expectations?", "85000 USD",
                     category="salary")
        d = decide(self.conn, "Desired salary?")
        self.assertEqual(d["action"], "confirm")
        self.assertEqual(d["answer"], "85000 USD")

    def test_keyword_hint_never_produces_auto(self):
        qid = add_question(self.conn, "What are your salary expectations?", "85000 USD",
                           category="salary")
        self.conn.execute("UPDATE questions SET times_seen = 999, sensitive = 0 WHERE id = ?",
                          (qid,))
        self.assertNotEqual(decide(self.conn, "Desired salary?")["action"], "auto")


class TestCategoryHint(unittest.TestCase):
    def test_recognises_sponsorship(self):
        self.assertEqual(category_hint("Do you need visa sponsorship?"), "sponsorship")

    def test_recognises_salary(self):
        self.assertEqual(category_hint("Desired salary?"), "salary")

    def test_ignores_unrelated_questions(self):
        self.assertIsNone(category_hint("Describe your favourite project."))


class TestCorrectionLoop(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def test_correction_updates_answer_and_cuts_confidence(self):
        qid = add_question(self.conn, "When can you start?", "Immediately",
                           category="start_date", confidence=0.9)
        apply_correction(self.conn, qid, "December 2026")
        row = self.conn.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
        self.assertEqual(row["canonical_answer"], "December 2026")
        self.assertEqual(row["times_corrected"], 1)
        self.assertLess(row["confidence"], 0.9)

    def test_correction_keeps_the_embedding_keyed_to_the_question(self):
        """Regression: the answer must not overwrite the question's vector."""
        qid = add_question(self.conn, "When can you start?", "Immediately",
                           category="start_date")
        apply_correction(self.conn, qid, "December 2026")
        match = find_match(self.conn, "When can you start?")
        self.assertAlmostEqual(match["similarity"], 1.0, places=6)

    def test_sighting_increments_times_seen(self):
        qid = add_question(self.conn, "How did you hear about us?", "Online job board",
                           category="other")
        record_sighting(self.conn, qid, "How did you hear about us?",
                        answer_given="Online job board", auto_answered=True)
        row = self.conn.execute("SELECT times_seen FROM questions WHERE id = ?",
                                (qid,)).fetchone()
        self.assertEqual(row["times_seen"], 1)

    def test_threshold_report_counts_auto_answers_later_corrected(self):
        qid = add_question(self.conn, "How did you hear about us?", "Online job board",
                           category="other")
        sid = record_sighting(self.conn, qid, "How did you hear about us?",
                              answer_given="Online job board", auto_answered=True)
        apply_correction(self.conn, qid, "Referral", sighting_id=sid)
        report = threshold_report(self.conn)
        self.assertEqual(report["auto_answered"], 1)
        self.assertEqual(report["auto_answered_then_corrected"], 1)
        self.assertEqual(report["auto_error_rate"], 1.0)


class TestSeed(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def test_seeding_is_idempotent(self):
        first = seed_question_bank(self.conn, PROFILE)
        count_after_first = self.conn.execute(
            "SELECT COUNT(*) AS n FROM questions").fetchone()["n"]
        seed_question_bank(self.conn, PROFILE)
        count_after_second = self.conn.execute(
            "SELECT COUNT(*) AS n FROM questions").fetchone()["n"]
        self.assertEqual(count_after_first, count_after_second)
        self.assertEqual(first, count_after_first)

    def test_sensitive_categories_are_flagged(self):
        seed_question_bank(self.conn, PROFILE)
        rows = self.conn.execute(
            "SELECT category, sensitive FROM questions "
            "WHERE category IN ('work_auth', 'sponsorship', 'salary')").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(r["sensitive"] == 1 for r in rows))

    def test_eeo_is_not_sensitive(self):
        """EEO is voluntary disclosure, not a knockout gate — different handling."""
        seed_question_bank(self.conn, PROFILE)
        rows = self.conn.execute(
            "SELECT sensitive FROM questions WHERE category = 'eeo'").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(r["sensitive"] == 0 for r in rows))

    def test_missing_profile_values_produce_no_entry(self):
        """A blank field must surface as a novel question, never a guessed default."""
        thin = {"personal": {}, "work_authorization": {}, "availability": {},
                "compensation": {}, "experience": {}, "eeo_voluntary": {}}
        texts = [e["text"] for e in build_seed_entries(thin)]
        self.assertNotIn("What are your salary expectations?", texts)
        self.assertNotIn("When can you start?", texts)

    def test_seeded_entries_are_not_immediately_auto_answerable(self):
        seed_question_bank(self.conn, PROFILE)
        d = decide(self.conn, "How did you hear about us?")
        self.assertEqual(d["action"], "confirm")

    def test_authorization_and_sponsorship_answers_come_from_the_profile(self):
        seed_question_bank(self.conn, PROFILE)
        auth = decide(self.conn, "Are you legally authorized to work in the United States?")
        spon = decide(self.conn, "Will you now or in the future require sponsorship?")
        self.assertEqual(auth["answer"], "Yes")
        self.assertEqual(spon["answer"], "No")


if __name__ == "__main__":
    unittest.main()
