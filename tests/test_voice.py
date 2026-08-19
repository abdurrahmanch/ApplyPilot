"""Tests for voice-profile loading and the batch sameness check."""
import unittest
from unittest.mock import patch

from applypilot.voice import (
    SAMENESS_THRESHOLD,
    append_learning,
    check_batch_sameness,
    letter_similarity,
    load_voice_profile,
    repeated_phrases,
)


def _letter(body: str) -> str:
    return f"Dear Hiring Manager,\n\n{body}\n\nAbdur-Rahman"


CLONE_A = _letter(
    "I build backend services in Java and Spring Boot for payment systems. "
    "At Duha Media I shipped Shopify storefronts for DTC brands and ran the "
    "infrastructure behind them on a self-hosted RHEL box.")
CLONE_B = _letter(
    "I build backend services in Java and Spring Boot for logistics systems. "
    "At Duha Media I shipped Shopify storefronts for DTC brands and ran the "
    "infrastructure behind them on a self-hosted RHEL box.")
DISTINCT = _letter(
    "SELinux policy authoring is where I spend my time. I wrote type "
    "enforcement modules so the JVM would run under full MAC enforcement, "
    "then documented the workflow for the next person.")


class TestLetterSimilarity(unittest.TestCase):
    def test_identical_letters_score_one(self):
        self.assertAlmostEqual(letter_similarity(CLONE_A, CLONE_A), 1.0, places=6)

    def test_company_name_swap_still_reads_as_a_clone(self):
        self.assertGreaterEqual(letter_similarity(CLONE_A, CLONE_B), SAMENESS_THRESHOLD)

    def test_genuinely_different_letters_score_low(self):
        self.assertLess(letter_similarity(CLONE_A, DISTINCT), SAMENESS_THRESHOLD)

    def test_boilerplate_alone_is_not_similarity(self):
        """Two letters sharing only salutation and sign-off must not be flagged."""
        a = _letter("Kafka consumers and Postgres schema design are my day job.")
        b = _letter("I write Liquid templates and tune storefront performance.")
        self.assertLess(letter_similarity(a, b), SAMENESS_THRESHOLD)

    def test_empty_text_is_safe(self):
        self.assertEqual(letter_similarity("", CLONE_A), 0.0)


class TestBatchSameness(unittest.TestCase):
    def test_flags_both_members_of_a_cloned_pair(self):
        flagged = check_batch_sameness({"job1": CLONE_A, "job2": CLONE_B,
                                        "job3": DISTINCT})
        self.assertEqual(len(flagged), 1)
        self.assertEqual({flagged[0]["a"], flagged[0]["b"]}, {"job1", "job2"})

    def test_clean_batch_flags_nothing(self):
        self.assertEqual(check_batch_sameness({"a": CLONE_A, "b": DISTINCT}), [])

    def test_single_letter_batch_is_trivially_clean(self):
        self.assertEqual(check_batch_sameness({"only": CLONE_A}), [])

    def test_results_are_sorted_worst_first(self):
        flagged = check_batch_sameness(
            {"a": CLONE_A, "b": CLONE_B, "c": CLONE_A}, threshold=0.1)
        scores = [f["similarity"] for f in flagged]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestRepeatedPhrases(unittest.TestCase):
    def test_catches_the_real_padding_example(self):
        text = ("responsive design here and responsive design there and more "
                "responsive design everywhere")
        self.assertIn("responsive design", repeated_phrases(text))

    def test_ignores_pure_filler(self):
        self.assertEqual(repeated_phrases("of the and of the and of the"), [])

    def test_clean_text_reports_nothing(self):
        self.assertEqual(repeated_phrases(DISTINCT), [])


class TestVoiceLoading(unittest.TestCase):
    def test_missing_profile_returns_empty_not_error(self):
        """A missing profile degrades to generic copy, it does not crash a run."""
        with patch("applypilot.voice.voice_dir") as vd:
            vd.return_value = __import__("pathlib").Path("/nonexistent/voice")
            self.assertEqual(load_voice_profile(), "")

    def test_append_learning_refuses_when_register_missing(self):
        with patch("applypilot.voice.voice_dir") as vd:
            vd.return_value = __import__("pathlib").Path("/nonexistent/voice")
            self.assertFalse(append_learning("- something learned"))

    def test_loads_core_and_register_together(self):
        import pathlib
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "registers").mkdir()
            (base / "voice-profile.md").write_text("CORE CONTENT")
            (base / "registers" / "job-applications.md").write_text("REGISTER CONTENT")
            with patch("applypilot.voice.voice_dir", return_value=base):
                loaded = load_voice_profile()
        self.assertIn("CORE CONTENT", loaded)
        self.assertIn("REGISTER CONTENT", loaded)

    def test_append_learning_never_touches_the_core(self):
        import pathlib
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "registers").mkdir()
            core = base / "voice-profile.md"
            core.write_text("CORE CONTENT")
            (base / "registers" / "job-applications.md").write_text("REGISTER")
            with patch("applypilot.voice.voice_dir", return_value=base):
                self.assertTrue(append_learning("- learned something"))
            self.assertEqual(core.read_text(), "CORE CONTENT")
            self.assertIn("learned something",
                          (base / "registers" / "job-applications.md").read_text())


if __name__ == "__main__":
    unittest.main()
