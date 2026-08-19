"""Unit tests for the hiring.cafe discovery adapter.

Network and browser paths are not exercised here — these cover the pure
functions: query validation, hit normalization, and the (company, title)
dedupe window.
"""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from applypilot.discovery.hiringcafe import (
    QUERIES,
    QueryFileError,
    dedupe_recent,
    load_query,
    normalize_hit,
    validate_query,
)


def _hit(**over):
    """A search hit shaped like the real 2026-08-19 response."""
    base = {
        "id": "grnhse___acme___123",
        "source": "grnhse",
        "board_token": "acme",
        "apply_url": "https://job-boards.greenhouse.io/acme/jobs/123",
        "is_expired": False,
        "job_information": {"title": "Backend Engineer",
                            "job_title_raw": "Backend Engineer"},
        "v5_processed_job_data": {
            "core_job_title": "Software Engineer",
            "workplace_cities": ["Chicago, Illinois, US"],
            "workplace_type": "Hybrid",
            "estimated_publish_date": "2026-08-18T05:39:08.000Z",
        },
        "enriched_company_data": {"name": "Acme Corp"},
    }
    base.update(over)
    return base


class TestValidateQuery(unittest.TestCase):
    def _valid(self):
        return {
            "locations": [{
                "formatted_address": "Chicago, IL, US",
                "types": ["locality"],
                "workplace_types": ["Remote", "Hybrid", "Onsite"],
                "id": "abc123",
            }],
            "jobTitleQuery": '"software engineer"',
        }

    def test_accepts_the_real_shape(self):
        validate_query(self._valid())  # must not raise

    def test_rejects_missing_locations(self):
        s = self._valid(); del s["locations"]
        with self.assertRaises(QueryFileError):
            validate_query(s)

    def test_rejects_empty_locations(self):
        s = self._valid(); s["locations"] = []
        with self.assertRaises(QueryFileError):
            validate_query(s)

    def test_rejects_missing_job_title_query(self):
        s = self._valid(); del s["jobTitleQuery"]
        with self.assertRaises(QueryFileError):
            validate_query(s)

    def test_rejects_unknown_workplace_type(self):
        s = self._valid(); s["locations"][0]["workplace_types"] = ["Hybrid", "Lunar"]
        with self.assertRaises(QueryFileError):
            validate_query(s)

    def test_rejects_missing_location_key(self):
        s = self._valid(); del s["locations"][0]["id"]
        with self.assertRaises(QueryFileError):
            validate_query(s)

    def test_unknown_query_name_raises(self):
        with self.assertRaises(QueryFileError):
            load_query("no-such-query")


class TestCommittedQueries(unittest.TestCase):
    """The two committed artifacts must stay loadable — the run halts otherwise."""

    def test_both_queries_exist_and_validate(self):
        for name, path in QUERIES.items():
            self.assertTrue(Path(path).exists(), f"{name} artifact missing at {path}")
            state = load_query(name)
            self.assertIn("locations", state)
            self.assertIn("jobTitleQuery", state)

    def test_remote_query_carries_a_remote_us_location(self):
        state = load_query("remote-usa")
        remote_us = [
            loc for loc in state["locations"]
            if loc.get("formatted_address") == "United States"
            and loc.get("workplace_types") == ["Remote"]
        ]
        self.assertTrue(remote_us, "remote-usa lost its United States/Remote entry")

    def test_queries_are_byte_stable_json(self):
        """Guards against an accidental reformat that changes the sent payload."""
        for name, path in QUERIES.items():
            raw = json.loads(Path(path).read_text())
            self.assertEqual(raw, load_query(name))


class TestNormalizeHit(unittest.TestCase):
    def test_maps_the_fields_the_pipeline_needs(self):
        row = normalize_hit(_hit(), "hybrid-chicago")
        self.assertEqual(row["url"], "https://job-boards.greenhouse.io/acme/jobs/123")
        self.assertEqual(row["application_url"], row["url"])
        self.assertEqual(row["title"], "Backend Engineer")
        self.assertEqual(row["employer_name"], "Acme Corp")
        self.assertEqual(row["location"], "Chicago, Illinois, US")
        self.assertEqual(row["posted_at"], "2026-08-18T05:39:08.000Z")
        self.assertEqual(row["_ats"], "grnhse")
        self.assertEqual(row["_query"], "hybrid-chicago")

    def test_description_is_left_for_the_enrich_stage(self):
        # hiring.cafe returns no posting text; enrich must scrape apply_url.
        row = normalize_hit(_hit(), "q")
        self.assertIsNone(row["description"])
        self.assertIsNone(row["full_description"])

    def test_expired_postings_are_dropped(self):
        self.assertIsNone(normalize_hit(_hit(is_expired=True), "q"))

    def test_missing_apply_url_is_dropped(self):
        self.assertIsNone(normalize_hit(_hit(apply_url=None), "q"))

    def test_non_http_apply_url_is_dropped(self):
        self.assertIsNone(normalize_hit(_hit(apply_url="javascript:void(0)"), "q"))

    def test_missing_title_is_dropped(self):
        h = _hit(job_information={})
        h["v5_processed_job_data"] = dict(h["v5_processed_job_data"], core_job_title=None)
        self.assertIsNone(normalize_hit(h, "q"))

    def test_falls_back_to_board_token_when_company_is_unknown(self):
        row = normalize_hit(_hit(enriched_company_data={}), "q")
        self.assertEqual(row["employer_name"], "acme")

    def test_multi_country_postings_surface_the_us_location_first(self):
        # A Chicago req cross-listed abroad must not lose Chicago to truncation.
        h = _hit()
        h["v5_processed_job_data"]["workplace_cities"] = [
            "Shanghai, Shanghai, CN", "Hong Kong, Hong Kong, HK",
            "Sydney, New South Wales, AU", "Chicago, Illinois, US"]
        row = normalize_hit(h, "q")
        self.assertTrue(row["location"].startswith("Chicago, Illinois, US"))

    def test_location_is_capped_at_three_cities(self):
        h = _hit()
        h["v5_processed_job_data"]["workplace_cities"] = [
            f"City{i}, State{i}, US" for i in range(6)]
        row = normalize_hit(h, "q")
        self.assertEqual(row["location"].count(" US"), 3)

    def test_falls_back_to_workplace_type_without_cities(self):
        h = _hit()
        h["v5_processed_job_data"]["workplace_cities"] = []
        self.assertEqual(normalize_hit(h, "q")["location"], "Hybrid")


class TestDedupeRecent(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE jobs (url TEXT PRIMARY KEY, site TEXT, title TEXT, "
            "discovered_at TEXT)")

    def _seed(self, company, title, days_ago=1):
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        self.conn.execute(
            "INSERT INTO jobs (url, site, title, discovered_at) VALUES (?,?,?,?)",
            (f"https://x/{company}/{title}/{days_ago}", company, title, ts))

    def _rows(self, *pairs):
        return [{"employer_name": c, "title": t} for c, t in pairs]

    def test_same_company_and_title_inside_the_window_is_skipped(self):
        self._seed("Acme Corp", "Backend Engineer", days_ago=3)
        kept, skipped = dedupe_recent(self.conn, self._rows(("Acme Corp", "Backend Engineer")))
        self.assertEqual(kept, [])
        self.assertEqual(skipped, 1)

    def test_wording_differences_still_count_as_duplicates(self):
        self._seed("Acme Corp", "Senior Backend Engineer", days_ago=2)
        kept, skipped = dedupe_recent(self.conn, self._rows(("Acme Corp", "Backend Engineer")))
        self.assertEqual(skipped, 1, "seniority prefix should not defeat the match")
        self.assertEqual(kept, [])

    def test_outside_the_window_is_kept(self):
        self._seed("Acme Corp", "Backend Engineer", days_ago=30)
        kept, skipped = dedupe_recent(self.conn, self._rows(("Acme Corp", "Backend Engineer")))
        self.assertEqual(len(kept), 1)
        self.assertEqual(skipped, 0)

    def test_same_title_at_a_different_company_is_kept(self):
        self._seed("Acme Corp", "Backend Engineer", days_ago=1)
        kept, _ = dedupe_recent(self.conn, self._rows(("Globex", "Backend Engineer")))
        self.assertEqual(len(kept), 1)

    def test_different_role_at_the_same_company_is_kept(self):
        self._seed("Acme Corp", "Backend Engineer", days_ago=1)
        kept, _ = dedupe_recent(self.conn, self._rows(("Acme Corp", "Data Scientist")))
        self.assertEqual(len(kept), 1)

    def test_duplicates_within_one_batch_collapse(self):
        kept, skipped = dedupe_recent(self.conn, self._rows(
            ("Acme Corp", "Backend Engineer"),
            ("Acme Corp", "Backend Engineer")))
        self.assertEqual(len(kept), 1)
        self.assertEqual(skipped, 1)


if __name__ == "__main__":
    unittest.main()
