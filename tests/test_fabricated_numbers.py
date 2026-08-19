"""Tests for the fabricated-quantity check (ARCHITECTURE §4, §13.6).

The `resume_facts` allowlist has always pinned tools and employers, so a letter
cannot invent a framework. It never pinned numbers, so a letter claiming "12B+
workflows/month" passed every check. That is the fabrication that actually
costs the candidate something: a reviewer who checks it finds a lie with his
name on it.

The hard part is not catching inventions — it is *not* catching the numerals
that legitimately appear in every letter. A check that flags "2026" or "three
years" forces regeneration forever and gets switched off. Both halves are
tested here.
"""

from __future__ import annotations

from applypilot.scoring.validator import find_unsupported_numbers, validate_cover_letter


SOURCES = [
    "Writers Studio: took the site to #2 in local Google ranking.",
    "Job description: maintain 99.9% uptime across 12 services, 3+ years.",
]


# ---------------------------------------------------------------------------
# It catches inventions
# ---------------------------------------------------------------------------

def test_invented_scale_claim_is_caught():
    assert find_unsupported_numbers(
        "I processed 12B+ workflows per month.", SOURCES) == ["12B"]


def test_invented_percentage_is_caught():
    assert find_unsupported_numbers("Cut latency by 45%.", SOURCES) == ["45%"]


def test_invented_large_bare_number_is_caught():
    assert find_unsupported_numbers(
        "Supported 85000 daily active users.", SOURCES) == ["85000"]


def test_several_inventions_are_all_reported_without_duplicates():
    found = find_unsupported_numbers(
        "Handled 45% growth, 45% churn, and 7M requests.", SOURCES)
    assert found == ["45%", "7M"]


# ---------------------------------------------------------------------------
# It does not fire on the numerals every letter legitimately contains
# ---------------------------------------------------------------------------

def test_years_are_not_claims():
    assert find_unsupported_numbers(
        "I graduate from DePaul in December 2026.", SOURCES) == []


def test_small_bare_integers_are_not_claims():
    assert find_unsupported_numbers(
        "I have worked on two products across three teams, 5 in total.",
        SOURCES) == []


def test_a_number_taken_from_the_job_description_is_supported():
    assert find_unsupported_numbers("I can hold 99.9% uptime.", SOURCES) == []


def test_a_number_taken_from_resume_facts_is_supported():
    assert find_unsupported_numbers(
        "I took that site to #2 in local search.", SOURCES) == []


def test_thousands_separators_match_their_plain_form():
    """'12,500' in the letter and '12500' in the resume are the same claim."""
    assert find_unsupported_numbers(
        "Served 12,500 requests.", ["peak was 12500 requests"]) == []


def test_no_sources_means_everything_is_unsupported():
    """A caller with nothing to check against should pass no sources at all
    rather than an empty list it expects to be lenient."""
    assert find_unsupported_numbers("Grew revenue 300%.", []) == ["300%"]


# ---------------------------------------------------------------------------
# Wiring into the validator
# ---------------------------------------------------------------------------

_LETTER = (
    "Dear Hiring Manager,\n\n"
    "I build backend services in Java and Spring Boot, and I have spent the "
    "last two years shipping them for small teams that could not afford to get "
    "it wrong. At Duha Media I owned the pieces nobody else wanted to touch.\n\n"
    "{claim}\n\n"
    "The role reads like the work I already do, and I would rather do it "
    "somewhere the outcome matters. I would welcome the chance to talk it "
    "through with your team and show what I have shipped so far this year.\n\n"
    "Sincerely,\nAbdur-Rahman"
)


def test_validator_rejects_a_letter_with_an_invented_metric():
    letter = _LETTER.format(
        claim="At Writers Studio I drove 12B+ monthly workflows through the "
              "platform and cut infrastructure spend by 60% in one quarter.")
    result = validate_cover_letter(letter, fact_sources=SOURCES)
    assert result["passed"] is False
    assert any("Unsupported quantities" in e for e in result["errors"])


def test_validator_accepts_the_same_letter_with_a_supported_metric():
    letter = _LETTER.format(
        claim="At Writers Studio I took the site to #2 in local Google "
              "ranking, which is the kind of result I care about most.")
    result = validate_cover_letter(letter, fact_sources=SOURCES)
    assert not any("Unsupported quantities" in e for e in result["errors"])


def test_the_check_is_off_when_no_sources_are_supplied():
    """Backwards compatibility: existing callers that pass only the text must
    behave exactly as they did before."""
    letter = _LETTER.format(claim="I drove 12B+ monthly workflows.")
    result = validate_cover_letter(letter)
    assert not any("Unsupported quantities" in e for e in result["errors"])
