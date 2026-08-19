"""Tests for OTP detection and the apply-worker handoff."""
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from applypilot.tracking.otp import (
    CODE_TTL_MINUTES,
    consume_code,
    extract_code,
    latest_unconsumed_code,
    looks_like_otp,
    process_email,
    record_code,
    wait_for_code,
)
from applypilot.tracking.triage import triage_email

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for path in sorted(MIGRATIONS.glob("*.sql")):
        conn.executescript(path.read_text())
    return conn


def _iso(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


class TestDetection(unittest.TestCase):
    def test_recognises_common_phrasings(self):
        for subject in ("Your verification code",
                        "One-time password for your account",
                        "Confirm your email address",
                        "Your security code is inside",
                        "OTP for Workday registration"):
            self.assertTrue(looks_like_otp(subject), subject)

    def test_ordinary_mail_is_not_an_otp(self):
        self.assertFalse(looks_like_otp("Thanks for applying to Acme Corp"))
        self.assertFalse(looks_like_otp("Interview invitation for Backend Engineer"))

    def test_triage_classifies_otp_before_noise(self):
        """A no-reply ATS verification mail must not be dropped as noise."""
        result = triage_email({
            "subject": "Your verification code is 384719",
            "sender": "no-reply@myworkday.com",
            "snippet": "Enter this code to continue",
        })
        self.assertEqual(result.classification, "otp")


class TestExtraction(unittest.TestCase):
    def test_extracts_a_labelled_code(self):
        self.assertEqual(extract_code("Your code is 384719"), "384719")
        self.assertEqual(extract_code("Verification code: 4821"), "4821")

    def test_prefers_labelled_over_bare_numbers(self):
        text = "Requisition 998877 for the role. Your verification code is 123456."
        self.assertEqual(extract_code(text), "123456")

    def test_extracts_a_bare_six_digit_code(self):
        self.assertEqual(extract_code("Use 903214 to verify your account"), "903214")

    def test_rejects_years(self):
        self.assertIsNone(extract_code("Copyright 2026 Acme Corp"))

    def test_returns_none_when_there_is_no_code(self):
        self.assertIsNone(extract_code("Please verify your email by clicking below"))
        self.assertIsNone(extract_code(None))

    def test_searches_across_subject_snippet_and_body(self):
        self.assertEqual(extract_code("Verify", "no code here", "code 556677"), "556677")


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def test_recording_is_idempotent_per_message(self):
        self.assertIsNotNone(record_code(self.conn, "msg1", "123456"))
        self.assertIsNone(record_code(self.conn, "msg1", "123456"))
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM email_events").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_latest_unconsumed_code_is_returned(self):
        record_code(self.conn, "old", "111111", received_at=_iso(5))
        record_code(self.conn, "new", "222222", received_at=_iso(1))
        self.assertEqual(latest_unconsumed_code(self.conn)["otp_code"], "222222")

    def test_consumed_codes_are_not_returned_again(self):
        record_code(self.conn, "msg1", "123456", received_at=_iso(1))
        event = latest_unconsumed_code(self.conn)
        consume_code(self.conn, event["id"])
        self.assertIsNone(latest_unconsumed_code(self.conn))

    def test_expired_codes_are_ignored(self):
        record_code(self.conn, "stale", "123456",
                    received_at=_iso(CODE_TTL_MINUTES + 5))
        self.assertIsNone(latest_unconsumed_code(self.conn))

    def test_company_hint_is_a_preference_not_a_filter(self):
        """Generic no-reply senders carry no company; they must still be usable."""
        record_code(self.conn, "generic", "999999", received_at=_iso(1))
        found = latest_unconsumed_code(self.conn, company_hint="Acme")
        self.assertIsNotNone(found)
        self.assertEqual(found["otp_code"], "999999")

    def test_company_hint_wins_when_it_matches(self):
        record_code(self.conn, "generic", "111111", received_at=_iso(1))
        record_code(self.conn, "acme", "222222", company_guess="Acme Corp",
                    received_at=_iso(3))
        found = latest_unconsumed_code(self.conn, company_hint="acme")
        self.assertEqual(found["otp_code"], "222222")


class TestWaitForCode(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def test_returns_immediately_when_a_code_is_waiting(self):
        record_code(self.conn, "msg1", "123456", received_at=_iso(0))
        code = wait_for_code(self.conn, timeout_seconds=1,
                             sleep=lambda _s: None)
        self.assertEqual(code, "123456")

    def test_consumes_the_code_it_returns(self):
        record_code(self.conn, "msg1", "123456", received_at=_iso(0))
        wait_for_code(self.conn, timeout_seconds=1, sleep=lambda _s: None)
        self.assertIsNone(latest_unconsumed_code(self.conn))

    def test_gives_up_rather_than_waiting_forever(self):
        """Nothing in a run may block indefinitely; the caller parks instead."""
        code = wait_for_code(self.conn, timeout_seconds=0, sleep=lambda _s: None)
        self.assertIsNone(code)

    def test_picks_up_a_code_that_arrives_mid_wait(self):
        state = {"ticks": 0}

        def fake_sleep(_seconds):
            state["ticks"] += 1
            if state["ticks"] == 1:
                record_code(self.conn, "late", "777777", received_at=_iso(0))

        code = wait_for_code(self.conn, timeout_seconds=60, poll_seconds=1,
                             sleep=fake_sleep)
        self.assertEqual(code, "777777")


class TestProcessEmail(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def test_stores_a_code_from_a_verification_email(self):
        code = process_email(self.conn, {
            "email_id": "msg1",
            "subject": "Your verification code",
            "snippet": "Your code is 445566",
            "received_at": _iso(0),
        })
        self.assertEqual(code, "445566")
        self.assertIsNotNone(latest_unconsumed_code(self.conn))

    def test_ignores_non_otp_mail(self):
        self.assertIsNone(process_email(self.conn, {
            "email_id": "msg2",
            "subject": "Thanks for applying",
            "snippet": "We received your application",
        }))

    def test_otp_shaped_mail_without_a_code_is_not_stored(self):
        self.assertIsNone(process_email(self.conn, {
            "email_id": "msg3",
            "subject": "Please verify your email",
            "snippet": "Click the link below to confirm",
        }))
        self.assertIsNone(latest_unconsumed_code(self.conn))


if __name__ == "__main__":
    unittest.main()
