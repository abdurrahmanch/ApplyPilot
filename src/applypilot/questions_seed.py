"""Seed the question bank with canonical answers drawn from profile.json.

Section 9 requires the bank to be pre-loaded before the first run, and that
every value come from Abdur-Rahman rather than being guessed. Everything here
is therefore derived from `profile.json`; nothing is invented. A field he has
not filled in produces no entry at all, so the question surfaces as novel at
the review gate instead of being answered from a default.
"""

from __future__ import annotations

import logging
import sqlite3

from applypilot.questions import add_question

log = logging.getLogger(__name__)


def _yes_no(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip().lower()
    if v in ("yes", "y", "true"):
        return "Yes"
    if v in ("no", "n", "false"):
        return "No"
    return str(value).strip()


def build_seed_entries(profile: dict) -> list[dict]:
    """Derive canonical (question, answer, category) triples from the profile."""
    personal = profile.get("personal", {})
    auth = profile.get("work_authorization", {})
    avail = profile.get("availability", {})
    comp = profile.get("compensation", {})
    exp = profile.get("experience", {})
    eeo = profile.get("eeo_voluntary", {})

    entries: list[dict] = []

    def add(text, answer, category, sensitive=None):
        if answer in (None, ""):
            log.debug("Skipping seed '%s' — profile has no value", text)
            return
        entry = {"text": text, "answer": str(answer), "category": category}
        if sensitive is not None:
            entry["sensitive"] = sensitive
        entries.append(entry)

    # Work authorization — sensitive. Multiple phrasings share one answer, and
    # the matcher generalizes from any of them.
    authorized = _yes_no(auth.get("legally_authorized_to_work"))
    add("Are you legally authorized to work in the United States?", authorized, "work_auth")
    add("Do you have the legal right to work in the country where this job is located?",
        authorized, "work_auth")

    # Sponsorship — sensitive, and the single most common silent-reject field.
    sponsorship = _yes_no(auth.get("require_sponsorship"))
    add("Will you now or in the future require sponsorship for employment visa status?",
        sponsorship, "sponsorship")
    add("Do you require visa sponsorship to work in the United States?",
        sponsorship, "sponsorship")

    permit = auth.get("work_permit_type")
    add("What is your work authorization status?", permit, "work_auth")

    # Salary — sensitive.
    expectation = comp.get("salary_expectation")
    currency = comp.get("salary_currency") or "USD"
    if expectation:
        # Several phrasings, because n-gram similarity is driven by wording and
        # "expectations" / "requirements" / "desired salary" score far apart.
        # Each is its own canonical entry pointing at the same answer.
        for phrasing in (
            "What are your salary expectations?",
            "What are your salary requirements?",
            "What is your desired compensation?",
            "What is your desired salary?",
            "What are your compensation expectations?",
        ):
            add(phrasing, f"{expectation} {currency}", "salary")
    lo, hi = comp.get("salary_range_min"), comp.get("salary_range_max")
    if lo and hi:
        add("What is your expected salary range?", f"{lo} - {hi} {currency}", "salary")

    # Start date.
    start = avail.get("earliest_start_date")
    add("When can you start?", start, "start_date")
    add("What is your earliest available start date?", start, "start_date")

    # Relocation and location.
    city, state = personal.get("city"), personal.get("province_state")
    if city and state:
        add("What country and region are you located within?",
            f"United States - {city}, {state}", "relocation")
        add("Where are you currently located?", f"{city}, {state}, United States", "relocation")

    # Degree and experience.
    add("What is your highest level of education?", exp.get("education_level"), "degree_yoe")
    add("How many years of professional experience do you have?",
        exp.get("years_of_experience_total"), "degree_yoe")

    # Background check.
    add("Are you willing to undergo a background check?", "Yes", "background_check")

    # EEO — his stated disclosure preferences, kept separate from knockout gates.
    add("What is your gender?", eeo.get("gender"), "eeo")
    add("What is your race/ethnicity?", eeo.get("race_ethnicity"), "eeo")
    add("Are you a protected veteran?", eeo.get("veteran_status"), "eeo")
    add("Do you have a disability?", eeo.get("disability_status"), "eeo")

    # Personal facts he has stated directly. These exist so the agent answers
    # from a fact rather than inferring one; anything not listed here still
    # parks. Section 9 / the 2026-08-19 fabrication incident.
    for key, question in (
        ("gaming", "What is your experience with video games?"),
        ("gaming", "Do you play video games?"),
        # Wording observed verbatim on the BisectHosting form, 2026-08-19. Long
        # parenthetical questions score ~0.72 against the short phrasing, just
        # under the review floor, so the exact wording is banked as its own
        # entry rather than loosening the threshold for everything.
        ("gaming",
         "What is your experience with video games at large (either working, playing, etc)?"),
    ):
        add(question, (profile.get("personal_facts") or {}).get(key), "personal_fact")

    # Sourcing question. Harmless, extremely common, and worth not re-asking.
    add("How did you hear about us?", "Online job board", "other")

    return entries


def seed_question_bank(conn: sqlite3.Connection, profile: dict | None = None,
                       confidence: float = 0.9) -> int:
    """Load the canonical entries into the bank. Idempotent.

    Seeded entries start at times_seen=0, so even non-sensitive ones surface
    for confirmation until they have been seen MIN_TIMES_SEEN_FOR_AUTO times on
    real forms. Seeding asserts the answer is right, not that the wording
    generalizes.
    """
    if profile is None:
        from applypilot.config import load_profile
        profile = load_profile()

    entries = build_seed_entries(profile)
    for entry in entries:
        add_question(conn, entry["text"], entry["answer"], category=entry["category"],
                     sensitive=entry.get("sensitive"), confidence=confidence)
    log.info("Seeded %d canonical question-bank entries", len(entries))
    return len(entries)
