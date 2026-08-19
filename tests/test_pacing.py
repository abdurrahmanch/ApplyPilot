"""Tests for apply-stage hardening: pacing, parking, proof, ATS health."""
import random
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from applypilot.apply.pacing import (
    APPLICATION_DELAY_RANGE,
    HEALTH_WINDOW,
    action_delay,
    active_cooldowns,
    application_delay,
    ats_health,
    cool_down_ats,
    is_cooled_down,
    looks_like_a_block,
    open_parked,
    park_application,
    proof_for,
    record_proof,
    resolve_parked,
    spread_schedule,
    unhealthy_ats,
)

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for path in sorted(MIGRATIONS.glob("*.sql")):
        conn.executescript(path.read_text())
    return conn


class TestPacing(unittest.TestCase):
    def test_delays_actually_sleep(self):
        with patch("applypilot.apply.pacing.time.sleep") as slept:
            action_delay(random.Random(1))
            application_delay(random.Random(1))
        self.assertEqual(slept.call_count, 2)

    def test_application_delay_is_within_range(self):
        with patch("applypilot.apply.pacing.time.sleep"):
            for seed in range(20):
                seconds = application_delay(random.Random(seed))
                self.assertGreaterEqual(seconds, APPLICATION_DELAY_RANGE[0])
                self.assertLessEqual(seconds, APPLICATION_DELAY_RANGE[1])

    def test_spread_schedule_covers_the_window_in_order(self):
        offsets = spread_schedule(17, 3600, random.Random(7))
        self.assertEqual(len(offsets), 17)
        self.assertEqual(offsets, sorted(offsets))
        self.assertGreaterEqual(offsets[0], 0.0)
        self.assertLess(offsets[-1], 3600)

    def test_spread_schedule_is_not_a_burst(self):
        """The last application must land well after the first."""
        offsets = spread_schedule(17, 3600, random.Random(3))
        self.assertGreater(offsets[-1] - offsets[0], 1800)

    def test_spread_schedule_edge_cases(self):
        self.assertEqual(spread_schedule(0, 3600), [])
        self.assertEqual(spread_schedule(1, 3600), [0.0])


class TestParking(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def test_parking_records_and_lists(self):
        park_application(self.conn, "https://x/1", "captcha", {"page": "review"})
        rows = open_parked(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "captcha")

    def test_unknown_reason_is_recorded_as_other_not_dropped(self):
        park_application(self.conn, "https://x/1", "aliens")
        row = open_parked(self.conn)[0]
        self.assertEqual(row["reason"], "other")
        self.assertIn("aliens", row["details_json"])

    def test_resolved_entries_leave_the_queue(self):
        pid = park_application(self.conn, "https://x/1", "mfa")
        resolve_parked(self.conn, pid, outcome="submitted manually")
        self.assertEqual(open_parked(self.conn), [])


class TestProof(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def test_proof_is_recorded_for_a_submission(self):
        record_proof(self.conn, "https://x/1", "applied", ats="greenhouse",
                     worker_id=0, screenshot_path="/tmp/a.png",
                     confirmation_text="Application Successful",
                     duration_ms=187000)
        rows = proof_for(self.conn, "https://x/1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["confirmation_text"], "Application Successful")

    def test_failures_are_recorded_too(self):
        """Evidence of failure is what makes an ATS's numbers investigable."""
        record_proof(self.conn, "https://x/1", "failed", ats="workday")
        self.assertEqual(proof_for(self.conn, "https://x/1")[0]["outcome"], "failed")

    def test_multiple_attempts_are_kept(self):
        record_proof(self.conn, "https://x/1", "failed", ats="workday")
        record_proof(self.conn, "https://x/1", "applied", ats="workday")
        self.assertEqual(len(proof_for(self.conn, "https://x/1")), 2)


class TestCooldowns(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def test_block_signals_are_recognised(self):
        for text in ("HTTP 403 Forbidden", "429 Too Many Requests",
                     "Cloudflare Turnstile challenge", "Access denied"):
            self.assertTrue(looks_like_a_block(text), text)

    def test_ordinary_errors_are_not_blocks(self):
        self.assertFalse(looks_like_a_block("field validation failed"))
        self.assertFalse(looks_like_a_block(None))

    def test_cooled_ats_is_skipped(self):
        cool_down_ats(self.conn, "workday", "429 on submit")
        self.assertTrue(is_cooled_down(self.conn, "workday"))
        self.assertFalse(is_cooled_down(self.conn, "greenhouse"))

    def test_expired_cooldown_lapses(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.conn.execute(
            "INSERT INTO ats_cooldowns (ats, reason, cooled_at, expires_at) "
            "VALUES ('workday', 'old', ?, ?)", (past, past))
        self.conn.commit()
        self.assertFalse(is_cooled_down(self.conn, "workday"))
        self.assertEqual(active_cooldowns(self.conn), [])

    def test_cooldown_is_idempotent(self):
        cool_down_ats(self.conn, "workday", "403")
        cool_down_ats(self.conn, "workday", "429")
        rows = active_cooldowns(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "429")

    def test_no_ats_is_never_cooled(self):
        self.assertFalse(is_cooled_down(self.conn, None))


class TestAtsHealth(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def _attempts(self, ats, applied, failed):
        for _ in range(applied):
            record_proof(self.conn, "https://x/a", "applied", ats=ats)
        for _ in range(failed):
            record_proof(self.conn, "https://x/b", "failed", ats=ats)

    def test_healthy_ats_is_not_flagged(self):
        self._attempts("greenhouse", applied=28, failed=2)
        self.assertEqual(unhealthy_ats(self.conn), [])

    def test_ats_below_the_floor_is_flagged(self):
        self._attempts("workday", applied=10, failed=20)
        flagged = unhealthy_ats(self.conn)
        self.assertEqual([f["ats"] for f in flagged], ["workday"])

    def test_small_samples_are_never_judged(self):
        """Three attempts is variance, not a verdict."""
        self._attempts("icims", applied=0, failed=3)
        self.assertEqual(unhealthy_ats(self.conn), [])
        report = next(r for r in ats_health(self.conn) if r["ats"] == "icims")
        self.assertFalse(report["judged"])

    def test_health_uses_only_the_recent_window(self):
        self._attempts("workday", applied=0, failed=HEALTH_WINDOW)
        self._attempts("workday", applied=HEALTH_WINDOW, failed=0)
        report = next(r for r in ats_health(self.conn) if r["ats"] == "workday")
        self.assertEqual(report["success_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
