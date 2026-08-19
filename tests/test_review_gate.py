"""Tests for the review gate.

The escalation rules are the product here: what reaches Abdur-Rahman's eyes,
what stays silent, and what blocks submission. Each of those is tested as
behaviour, because getting them wrong either wastes his time or lets a bad
application through unseen.
"""
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from applypilot.questions import MIN_TIMES_SEEN_FOR_AUTO, add_question
from applypilot.review.batch import (
    apply_gate_correction,
    approve_batch,
    build_batch,
    get_batch,
    get_pending_batch,
    is_high_value,
    resolve_item,
    submittable_applications,
    unresolved_items,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.executescript(path.read_text())
    return conn


def _app(url="https://x/1", **over):
    base = {"url": url, "title": "Backend Engineer", "company": "Acme Corp",
            "cover_letter": None, "questions": [], "status": "ready"}
    base.update(over)
    return base


def _kinds(conn, batch_id):
    return [i["kind"] for i in get_batch(conn, batch_id)["items"]]


class TestEscalation(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def test_auto_answerable_question_is_silent(self):
        """The whole point of the confidence gate: proven answers never surface."""
        qid = add_question(self.conn, "How did you hear about us?", "Online job board",
                           category="other")
        self.conn.execute("UPDATE questions SET times_seen = ? WHERE id = ?",
                          (MIN_TIMES_SEEN_FOR_AUTO, qid))
        bid = build_batch(self.conn, [_app(questions=["How did you hear about us?"])])
        self.assertNotIn("novel_question", _kinds(self.conn, bid))
        self.assertNotIn("low_confidence", _kinds(self.conn, bid))

    def test_novel_question_surfaces(self):
        bid = build_batch(self.conn, [
            _app(questions=["Describe a time you disagreed with a coworker."])])
        self.assertIn("novel_question", _kinds(self.conn, bid))

    def test_sensitive_question_surfaces_even_at_perfect_match(self):
        q = "Will you now or in the future require sponsorship?"
        qid = add_question(self.conn, q, "No", category="sponsorship")
        self.conn.execute("UPDATE questions SET times_seen = 9999 WHERE id = ?", (qid,))
        bid = build_batch(self.conn, [_app(questions=[q])])
        self.assertIn("never_auto_guess", _kinds(self.conn, bid))

    def test_unproven_match_surfaces_as_low_confidence(self):
        add_question(self.conn, "How did you hear about us?", "Online job board",
                     category="other")
        bid = build_batch(self.conn, [_app(questions=["How did you hear about us?"])])
        self.assertIn("low_confidence", _kinds(self.conn, bid))

    def test_cover_letter_produces_a_delta_not_the_full_text(self):
        letter = ("Dear Hiring Manager,\n\nI wrote SELinux policy for the JVM at "
                  "Duha Media and shipped it to production.\n\nAbdur-Rahman")
        bid = build_batch(self.conn, [_app(cover_letter=letter)])
        item = next(i for i in get_batch(self.conn, bid)["items"]
                    if i["kind"] == "cover_delta")
        self.assertTrue(item["payload"]["opening"])
        self.assertIn("word_count", item["payload"])
        # Full text is carried for on-demand expansion, not shown by default.
        self.assertIn("full_text", item["payload"])

    def test_disqualified_and_parked_are_awareness_only(self):
        bid = build_batch(self.conn, [
            _app(url="https://x/1", status="disqualified", reason="senior role"),
            _app(url="https://x/2", status="parked", reason="captcha"),
        ])
        batch = get_batch(self.conn, bid)
        self.assertEqual({i["kind"] for i in batch["items"]}, {"disqualified", "parked"})
        self.assertEqual(unresolved_items(batch), [])

    def test_high_value_employer_is_flagged_and_sorted_first(self):
        bid = build_batch(self.conn, [
            _app(url="https://x/1", company="Acme Corp",
                 questions=["Describe a conflict."]),
            _app(url="https://x/2", company="DRW Trading"),
        ])
        kinds = _kinds(self.conn, bid)
        self.assertIn("high_value", kinds)
        self.assertEqual(kinds[0], "high_value")

    def test_sameness_warning_flags_both_letters(self):
        shared = ("I build backend services in Java and Spring Boot for payment "
                  "systems. At Duha Media I shipped Shopify storefronts for DTC "
                  "brands and ran the infrastructure behind them.")
        bid = build_batch(self.conn, [
            _app(url="https://x/1", cover_letter=f"Dear Hiring Manager,\n\n{shared}\n\nA"),
            _app(url="https://x/2", cover_letter=f"Dear Hiring Manager,\n\n{shared}\n\nA"),
        ])
        warnings = [i for i in get_batch(self.conn, bid)["items"]
                    if i["kind"] == "sameness_warning"]
        self.assertEqual(len(warnings), 2)
        self.assertEqual({w["application_id"] for w in warnings},
                         {"https://x/1", "https://x/2"})


class TestHighValue(unittest.TestCase):
    def test_recognises_chicago_trading_firms(self):
        for name in ("DRW Trading", "IMC Trading", "Peak6", "Cboe Global Markets"):
            self.assertTrue(is_high_value(name), name)

    def test_ordinary_employers_are_not_high_value(self):
        self.assertFalse(is_high_value("Acme Corp"))
        self.assertFalse(is_high_value(None))


class TestApproval(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def test_approval_is_the_only_path_to_submittable(self):
        bid = build_batch(self.conn, [_app(cover_letter="Dear Hiring Manager,\n\n"
                                           "Real specifics here about Spring Boot.\n\nA")])
        # Pending batch: nothing is submittable yet.
        self.assertEqual(submittable_applications(self.conn, bid), [])

        for item in get_batch(self.conn, bid)["items"]:
            resolve_item(self.conn, item["id"], "approved")
        result = approve_batch(self.conn, bid)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["submittable"], ["https://x/1"])

    def test_partial_approval_holds_back_only_the_unresolved_application(self):
        bid = build_batch(self.conn, [
            _app(url="https://x/1", questions=["Describe a conflict."]),
            _app(url="https://x/2", cover_letter="Dear Hiring Manager,\n\n"
                 "Concrete Kafka and Postgres work at Duha Media.\n\nA"),
        ])
        # Resolve only the second application's items.
        for item in get_batch(self.conn, bid)["items"]:
            if item["application_id"] == "https://x/2":
                resolve_item(self.conn, item["id"], "approved")

        result = approve_batch(self.conn, bid, allow_partial=True)
        self.assertEqual(result["status"], "partial")
        self.assertIn("https://x/2", result["submittable"])
        self.assertNotIn("https://x/1", result["submittable"])
        self.assertIn("https://x/1", result["held_back"])

    def test_strict_approval_refuses_while_items_are_open(self):
        bid = build_batch(self.conn, [_app(questions=["Describe a conflict."])])
        with self.assertRaises(ValueError):
            approve_batch(self.conn, bid, allow_partial=False)

    def test_disqualified_applications_are_never_submittable(self):
        bid = build_batch(self.conn, [
            _app(url="https://x/1", status="disqualified", reason="senior role")])
        approve_batch(self.conn, bid)
        self.assertEqual(submittable_applications(self.conn, bid), [])

    def test_pending_batch_lookup_returns_the_latest(self):
        build_batch(self.conn, [_app(url="https://x/1")])
        second = build_batch(self.conn, [_app(url="https://x/2")])
        self.assertEqual(get_pending_batch(self.conn)["id"], second)


class TestCorrectionPropagation(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def test_correcting_a_matched_question_updates_the_bank(self):
        q = "When can you start?"
        qid = add_question(self.conn, q, "Immediately", category="start_date")
        bid = build_batch(self.conn, [_app(questions=[q])])
        item = next(i for i in get_batch(self.conn, bid)["items"]
                    if i["kind"] in ("low_confidence", "novel_question"))

        apply_gate_correction(self.conn, item["id"], "December 2026")
        row = self.conn.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
        self.assertEqual(row["canonical_answer"], "December 2026")
        self.assertEqual(row["times_corrected"], 1)

    def test_answering_a_novel_question_creates_a_bank_entry(self):
        q = "Describe a time you disagreed with a coworker."
        bid = build_batch(self.conn, [_app(questions=[q])])
        item = next(i for i in get_batch(self.conn, bid)["items"]
                    if i["kind"] == "novel_question")

        apply_gate_correction(self.conn, item["id"], "I raised it directly in review.")
        row = self.conn.execute(
            "SELECT * FROM questions WHERE canonical_text = ?", (q,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["canonical_answer"], "I raised it directly in review.")

    def test_the_same_correction_is_never_needed_twice(self):
        q = "When can you start?"
        add_question(self.conn, q, "Immediately", category="start_date")
        bid = build_batch(self.conn, [_app(questions=[q])])
        item = next(i for i in get_batch(self.conn, bid)["items"]
                    if i["kind"] in ("low_confidence", "novel_question"))
        apply_gate_correction(self.conn, item["id"], "December 2026")

        # A later batch proposes the corrected answer, not the original.
        bid2 = build_batch(self.conn, [_app(url="https://x/2", questions=[q])])
        later = next(i for i in get_batch(self.conn, bid2)["items"]
                     if i["kind"] in ("low_confidence", "novel_question"))
        self.assertEqual(later["payload"]["proposed_answer"], "December 2026")

    def test_correction_resolves_the_item(self):
        q = "When can you start?"
        add_question(self.conn, q, "Immediately", category="start_date")
        bid = build_batch(self.conn, [_app(questions=[q])])
        item = next(i for i in get_batch(self.conn, bid)["items"]
                    if i["kind"] in ("low_confidence", "novel_question"))
        apply_gate_correction(self.conn, item["id"], "December 2026")
        refreshed = next(i for i in get_batch(self.conn, bid)["items"]
                         if i["id"] == item["id"])
        self.assertIsNotNone(refreshed["resolution"])


class TestCoverLearning(unittest.TestCase):
    def test_cover_note_appends_to_the_register(self):
        from applypilot.review.batch import record_cover_learning
        conn = _db()
        bid = build_batch(conn, [_app(cover_letter="Dear Hiring Manager,\n\n"
                                      "Spring Boot services and Postgres schemas.\n\nA")])
        item = next(i for i in get_batch(conn, bid)["items"] if i["kind"] == "cover_delta")
        with patch("applypilot.review.batch.append_learning", return_value=True) as appended:
            record_cover_learning(conn, item["id"], "letters open too generically")
        appended.assert_called_once()
        self.assertIn("letters open too generically", appended.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
