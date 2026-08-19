"""Read Gmail from a logged-in browser profile (ARCHITECTURE §6).

The OAuth path needs a Google Cloud project, a desktop credential, and a
console flow that stalled for weeks while the Workday-heavy queue sat blocked.
This reads the same mail out of a browser session the operator signed into once
— the same pattern discovery already uses for hiring.cafe.

Honest about what it is: scraping a UI is more fragile than an API. Gmail can
change its markup and this stops working. The failure mode is acceptable —
`available()` goes false or `fetch()` returns nothing, the OTP poller finds no
code, and the application parks instead of breaking. If the markup churn
becomes routine, the upgrade is `gws auth login` (browser OAuth, no cloud
project) rather than more selectors.

Read-only by construction. Nothing here sends, deletes, or modifies mail.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

PROFILE = Path.home() / ".applypilot" / "gmail-profile"

# Google sets these only on a genuinely authenticated session.
_AUTH_COOKIES = {"SID", "SSID", "HSID", "SAPISID", "__Secure-1PSID"}

# Row scraping. These are Gmail's long-standing list classes; if they change,
# `fetch` returns nothing rather than wrong data.
_ROW_SCRIPT = """(limit) => [...document.querySelectorAll('tr.zA')]
  .slice(0, limit).map(r => {
    const sender = r.querySelector('span[email]');
    const subject = r.querySelector('.bog');
    const snippet = r.querySelector('.y2');
    const date = r.querySelector('span[title]');
    return {
      id: r.getAttribute('data-legacy-thread-id') || r.id || '',
      sender: sender ? sender.getAttribute('email') : '',
      sender_name: sender ? sender.getAttribute('name') : '',
      subject: subject ? subject.innerText : '',
      snippet: snippet ? snippet.innerText : '',
      date: date ? date.getAttribute('title') : ''
    };
  })"""

_BODY_SCRIPT = """() => {
  const el = document.querySelector('div.a3s');
  return el ? el.innerText : '';
}"""


def available() -> bool:
    """Whether a signed-in profile exists to read from."""
    cookies = PROFILE / "Default" / "Cookies"
    if not cookies.exists():
        return False
    try:
        # Copy-free read: the profile may be locked by a running browser, so
        # open read-only and tolerate failure rather than holding a write lock.
        conn = sqlite3.connect(f"file:{cookies}?mode=ro&immutable=1", uri=True)
        try:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM cookies WHERE host_key LIKE '%google%'")}
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.debug("Could not read the Gmail profile cookies: %s", e)
        return False
    return _AUTH_COOKIES.issubset(names)


def _clean(text: str | None) -> str:
    """Gmail pads snippets with non-breaking spaces and a leading dash."""
    if not text:
        return ""
    text = text.replace(" ", " ").replace(" ", " ")
    text = re.sub(r"^\s*-\s*", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str | None) -> str:
    """Gmail's row title, e.g. 'Wed, Aug 19, 2026, 5:54 PM' → ISO, or ''."""
    if not raw:
        return ""
    cleaned = raw.replace(" ", " ").strip()
    for fmt in ("%a, %b %d, %Y, %I:%M %p", "%b %d, %Y, %I:%M %p"):
        try:
            return datetime.strptime(cleaned, fmt).replace(
                tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return ""


def _open(p):
    """Launch the persistent context. Caller owns closing it."""
    from applypilot import config
    return p.chromium.launch_persistent_context(
        str(PROFILE),
        executable_path=config.get_chrome_path() or None,
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )


def _scrape(page, query: str, limit: int) -> list[dict]:
    """Run one query in an already-open page and read the result rows."""
    fragment = ("#search/" + urllib.parse.quote(query)) if query else "#inbox"
    page.goto(f"https://mail.google.com/mail/u/0/{fragment}",
              wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector("tr.zA", timeout=25000)
    except Exception:
        # An empty result set is a legitimate answer, not a fault.
        log.debug("No message rows for query %r", query)
        return []

    return [{
        "email_id": row.get("id") or "",
        "subject": _clean(row.get("subject")),
        "sender": row.get("sender") or "",
        "sender_name": row.get("sender_name") or "",
        "snippet": _clean(row.get("snippet")),
        "body_text": "",
        "received_at": _parse_date(row.get("date")),
    } for row in (page.evaluate(_ROW_SCRIPT, limit) or [])]


def fetch(query: str = "newer_than:1d", limit: int = 25,
          with_bodies: bool = False) -> list[dict]:
    """Read recent messages as dicts shaped for `tracking.otp.process_email`.

    Args:
        query: a Gmail search query, exactly as typed into the search box.
        limit: how many rows to take.
        with_bodies: open each message to capture its body. Slower — one
            navigation per message — so callers that only need subject and
            snippet should leave it off.

    Returns [] on any failure. A tracking poll that finds nothing is a normal
    outcome; one that raises would take down the caller.
    """
    if not available():
        log.debug("No signed-in Gmail profile at %s", PROFILE)
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright is not installed; cannot read Gmail")
        return []

    try:
        with sync_playwright() as p:
            ctx = _open(p)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                emails = _scrape(page, query, limit)
                if with_bodies:
                    _load_bodies(page, emails)
                return emails
            finally:
                ctx.close()
    except Exception as e:
        log.warning("Reading Gmail from the browser session failed: %s", e)
        return []


def _load_bodies(page, emails: list[dict]) -> None:
    """Open each thread and capture its text. Best effort, never raises."""
    for email in emails:
        thread_id = email.get("email_id")
        if not thread_id:
            continue
        try:
            page.goto(f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector("div.a3s", timeout=15000)
            email["body_text"] = _clean(page.evaluate(_BODY_SCRIPT))
        except Exception as e:
            log.debug("Could not read the body of %s: %s", thread_id, e)


def fetch_otp_candidates(lookback_hours: int = 1, limit: int = 25) -> list[dict]:
    """Recent mail likely to carry a verification code.

    Bodies are loaded only for the rows that already look like an OTP, because
    opening a thread costs a navigation each and most of the inbox is not a
    code. Subject and snippet decide; the body only fills in a code that was
    not visible in the preview.
    """
    from applypilot.tracking.otp import looks_like_otp

    # Gmail search is day-granular, so a sub-day lookback still asks for a day.
    # otp.CODE_TTL_MINUTES is what actually keeps a stale code out.
    days = max(1, round(lookback_hours / 24))
    emails = fetch(f"newer_than:{days}d", limit=limit)

    candidates = [e for e in emails
                  if looks_like_otp(e.get("subject"), e.get("snippet"))]
    if candidates:
        try:
            with_bodies = fetch(f"newer_than:{days}d", limit=limit,
                                with_bodies=True)
            by_id = {e["email_id"]: e for e in with_bodies}
            for candidate in candidates:
                body = by_id.get(candidate["email_id"], {}).get("body_text")
                if body:
                    candidate["body_text"] = body
        except Exception as e:
            log.debug("Could not enrich OTP candidates with bodies: %s", e)
    return candidates


# ---------------------------------------------------------------------------
# Adapters for the tracking pipeline
# ---------------------------------------------------------------------------
#
# `run_tracking` was written against the MCP client and expects that module's
# `_normalize_email` shape — `id`/`date`/`body` rather than
# `email_id`/`received_at`/`body_text`. Adapting here keeps the ten-step
# pipeline untouched: it cannot tell which source it is reading from.

# The queries the MCP client uses, kept in step so switching source does not
# silently change what gets tracked.
_TRACKING_QUERIES = [
    "from:noreply OR from:no-reply OR from:notifications OR from:careers OR from:talent",
    "greenhouse OR lever OR workday OR ashby OR icims OR taleo OR paylocity",
    "application OR interview OR candidate OR position OR recruiting",
]


def _to_tracking_shape(email: dict) -> dict:
    return {
        "id": email.get("email_id", ""),
        "thread_id": email.get("email_id"),
        "subject": email.get("subject", ""),
        "sender": email.get("sender", ""),
        "sender_name": email.get("sender_name", ""),
        "date": email.get("received_at", ""),
        "snippet": email.get("snippet", ""),
        "body": email.get("body_text", ""),
        "to": "",
    }


def search_application_emails(days: int = 14, limit: int = 100) -> list[dict]:
    """Metadata-only search, matching the MCP client's signature and shape.

    Runs the same three queries and merges them, deduplicating by id — a
    message that matches two queries is one email, not two.
    """
    if not available():
        return []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    seen: dict[str, dict] = {}
    per_query = max(10, limit // len(_TRACKING_QUERIES))
    try:
        # One context for all three queries. A browser launch per query turned
        # a routine tracking run into minutes of startup cost.
        with sync_playwright() as p:
            ctx = _open(p)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                for clause in _TRACKING_QUERIES:
                    for email in _scrape(page, f"newer_than:{days}d ({clause})",
                                         per_query):
                        if email["email_id"] and email["email_id"] not in seen:
                            seen[email["email_id"]] = _to_tracking_shape(email)
                    if len(seen) >= limit:
                        break
            finally:
                ctx.close()
    except Exception as e:
        log.warning("Gmail search failed: %s", e)
    return list(seen.values())[:limit]


def read_email_bodies(email_ids: list[str]) -> dict[str, dict]:
    """Batch body read, matching the MCP client's signature and shape."""
    if not email_ids:
        return {}
    wanted = set(email_ids)
    out = {e["email_id"]: _to_tracking_shape(e)
           for e in fetch("newer_than:30d", limit=100)
           if e["email_id"] in wanted}
    # Only the threads actually asked for get opened, one navigation each,
    # rather than every row in the window.
    if out:
        _fill_bodies_by_id(out, list(out))
    return out


def _fill_bodies_by_id(out: dict[str, dict], ids: list[str]) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return
    try:
        with sync_playwright() as p:
            ctx = _open(p)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                for thread_id in ids:
                    try:
                        page.goto(
                            f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
                            wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_selector("div.a3s", timeout=15000)
                        out[thread_id]["body"] = _clean(page.evaluate(_BODY_SCRIPT))
                    except Exception as e:
                        log.debug("Could not read body %s: %s", thread_id, e)
            finally:
                ctx.close()
    except Exception as e:
        log.warning("Body read session failed: %s", e)
