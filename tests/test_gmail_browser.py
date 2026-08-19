"""Tests for the browser-session Gmail reader (ARCHITECTURE §6).

Everything that needs a browser is excluded — what is tested here is the part
that decides whether a session exists, the parsing of what Gmail hands back,
and the shape adapters that let `run_tracking` stay ignorant of which source it
is reading from.

The reader's contract in one line: **it never raises.** A tracking poll that
finds nothing is a normal Tuesday; one that throws takes down the caller.
"""

from __future__ import annotations

import sqlite3

import pytest

from applypilot.tracking import gmail_browser as gb


# ---------------------------------------------------------------------------
# available() — is there a signed-in session to read from
# ---------------------------------------------------------------------------

def _cookie_db(path, names: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT)")
    conn.executemany("INSERT INTO cookies VALUES (?, ?)",
                     [(".google.com", n) for n in names])
    conn.commit()
    conn.close()


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(gb, "PROFILE", tmp_path / "gmail-profile")
    return tmp_path / "gmail-profile"


def test_no_profile_means_unavailable(profile):
    assert gb.available() is False


def test_a_profile_without_auth_cookies_is_unavailable(profile):
    """Visiting Gmail while signed out still writes cookies. Presence of the
    file proves nothing; the auth cookie set is what matters."""
    _cookie_db(profile / "Default" / "Cookies", ["NID", "CONSENT"])
    assert gb.available() is False


def test_a_signed_in_profile_is_available(profile):
    _cookie_db(profile / "Default" / "Cookies",
               ["SID", "SSID", "HSID", "SAPISID", "__Secure-1PSID", "NID"])
    assert gb.available() is True


def test_a_corrupt_cookie_store_is_unavailable_not_an_error(profile):
    path = profile / "Default" / "Cookies"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not a database")
    assert gb.available() is False


def test_fetch_returns_nothing_when_no_session_exists(profile):
    """The guard that keeps a missing login from launching a browser."""
    assert gb.fetch("newer_than:1d") == []
    assert gb.fetch_otp_candidates() == []


# ---------------------------------------------------------------------------
# Parsing what Gmail actually hands back
# ---------------------------------------------------------------------------

def test_snippet_padding_is_stripped():
    """Gmail prefixes snippets with a non-breaking space, a dash, and a
    newline. Left in, every stored snippet starts with junk."""
    assert gb._clean(" - \nDear Abdur-Rahman, thank you") == \
        "Dear Abdur-Rahman, thank you"


def test_clean_collapses_whitespace_and_handles_none():
    assert gb._clean("  a   b\n\nc  ") == "a b c"
    assert gb._clean(None) == ""
    assert gb._clean("") == ""


def test_row_date_parses_to_iso():
    """Gmail uses a narrow no-break space before AM/PM, which is why a plain
    strptime on the raw title fails."""
    assert gb._parse_date("Wed, Aug 19, 2026, 5:54 PM").startswith(
        "2026-08-19T17:54")


def test_short_date_form_parses():
    assert gb._parse_date("Aug 19, 2026, 1:55 PM").startswith(
        "2026-08-19T13:55")


def test_an_unparseable_date_is_empty_not_an_exception():
    assert gb._parse_date("yesterday") == ""
    assert gb._parse_date(None) == ""


# ---------------------------------------------------------------------------
# Shape adapters — run_tracking must not be able to tell the source apart
# ---------------------------------------------------------------------------

def test_tracking_shape_matches_the_mcp_client_keys():
    """`run_tracking` indexes emails by `id`, `date`, and `body`. Reading a
    browser dict with `email_id`/`received_at`/`body_text` would KeyError deep
    inside the pipeline."""
    adapted = gb._to_tracking_shape({
        "email_id": "abc123",
        "subject": "Application Update",
        "sender": "do-not-reply@mail.paylocity.com",
        "sender_name": "BisectHosting",
        "snippet": "Thank you for applying",
        "body_text": "full text",
        "received_at": "2026-08-19T13:55:00+00:00",
    })
    assert set(adapted) == {"id", "thread_id", "subject", "sender",
                            "sender_name", "date", "snippet", "body", "to"}
    assert adapted["id"] == "abc123"
    assert adapted["date"] == "2026-08-19T13:55:00+00:00"
    assert adapted["body"] == "full text"


def test_otp_candidate_shape_actually_works_with_process_email(tmp_db):
    """The reader's OTP shape and `process_email`'s expectations are the same
    contract, so exercise it end to end rather than comparing key names."""
    from applypilot.tracking.otp import process_email

    conn = tmp_db()
    code = process_email(conn, {
        "email_id": "thread-1",
        "subject": "Your verification code",
        "sender": "no-reply@myworkday.com",
        "sender_name": "Workday",
        "snippet": "Your verification code is 481920",
        "body_text": "",
        "received_at": "2026-08-19T13:55:00+00:00",
    })
    assert code == "481920"
    stored = conn.execute(
        "SELECT otp_code FROM email_events WHERE gmail_msg_id = 'thread-1'"
    ).fetchone()
    assert stored["otp_code"] == "481920"


def test_a_non_otp_email_is_ignored(tmp_db):
    from applypilot.tracking.otp import process_email

    assert process_email(tmp_db(), {
        "email_id": "thread-2",
        "subject": "BisectHosting Application Update",
        "snippet": "Thank you for your interest in the Web Developer role",
        "body_text": "",
        "received_at": "2026-08-19T13:55:00+00:00",
    }) is None


def test_search_queries_cover_the_ats_families_in_the_queue():
    """Losing a family here silently stops tracking its responses."""
    joined = " ".join(gb._TRACKING_QUERIES).lower()
    for family in ("greenhouse", "lever", "workday", "ashby", "paylocity"):
        assert family in joined, family


def test_read_email_bodies_with_no_ids_does_not_open_a_browser(profile):
    assert gb.read_email_bodies([]) == {}
