"""Recover missing application URLs (ARCHITECTURE §3).

Enrichment resolves a description and an apply URL. When it gets the first and
not the second, the row still passes every gate the tailor and cover stages
check, so a resume and a letter get written for a job that can never be
submitted. Twelve of twenty-seven prepared applications were in that state.

Three methods, cheapest first — the same discipline the enrichment cascade
already uses:

    1. URL alone          no network at all. On Greenhouse, Lever, Ashby,
                          Rippling and Taleo the listing page IS the
                          application page; there is no separate apply link
                          to find, which is exactly why the selector hunt
                          came back empty. Also rewrites a `?gh_jid=N`
                          embed to Greenhouse's own form, which needs only the
                          job id and not the board slug.
    2. structured data    JSON-LD JobPosting carries an `applicationUrl` or a
                          `url` on many custom career sites. Cheap to read.
    3. rendered page      a wider selector sweep than enrichment's, plus
                          same-origin apply paths.
    4. click Apply        for boards that render Apply as a JavaScript button
                          with no href, where the destination does not exist
                          until the click. Navigation only; nothing is filled
                          or submitted.

Never guesses. A URL that cannot be established stays NULL, because a wrong
apply URL sends an application into the void more quietly than no URL at all.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

# ATS families whose listing page hosts the application form itself.
SELF_HOSTING_ATS = frozenset({
    "greenhouse", "lever", "ashby", "rippling", "taleo", "workday",
    "smartrecruiters", "jazzhr", "breezy", "workable", "recruitee",
    "bamboohr", "paylocity", "icims", "jobvite",
})

# Broader than enrichment's set: these are the patterns that survive on custom
# career sites, where the button is rarely a plain <a> with "apply" in it.
WIDE_APPLY_SELECTORS = (
    "a[href*='apply' i]",
    "a[data-testid*='apply' i]",
    "a[aria-label*='apply' i]",
    "button[data-testid*='apply' i]",
    "[class*='apply' i] a[href]",
    "a[href*='greenhouse.io']",
    "a[href*='lever.co']",
    "a[href*='ashbyhq.com']",
    "a[href*='myworkdayjobs.com']",
    "a[href*='icims.com']",
    "a[href*='smartrecruiters.com']",
)


def from_self_hosting_ats(url: str | None) -> str | None:
    """The listing URL, when its ATS hosts the form on that same page."""
    if not url:
        return None
    try:
        from applypilot.apply.chrome import detect_ats
    except Exception:  # pragma: no cover - import guard only
        return None
    return url if detect_ats(url) in SELF_HOSTING_ATS else None


def from_greenhouse_token(url: str | None) -> str | None:
    """A `?gh_jid=N` embed rewritten to Greenhouse's own application form.

    Many employers embed Greenhouse in their careers page and the iframe loads
    lazily, so a rendered-page sweep finds `about:blank` and gives up. The job
    id in the query string is enough on its own: Greenhouse serves the form at
    `boards.greenhouse.io/embed/job_app?token=<id>` without needing the
    company's board slug, which is the part we do not know.

    Verified against a live posting: HTTP 200, 23 form fields, a resume upload.
    Note the host — `job-boards.greenhouse.io` 404s on this route, only the
    older `boards.greenhouse.io` serves it.
    """
    if not url or "gh_jid=" not in url:
        return None
    import re
    m = re.search(r"gh_jid=(\d+)", url)
    if not m:
        return None
    return f"https://boards.greenhouse.io/embed/job_app?token={m.group(1)}"


def from_json_ld(page) -> str | None:
    """A JobPosting's own applicationUrl, if the page publishes one."""
    try:
        blobs = page.evaluate(
            "() => [...document.querySelectorAll('script[type=\"application/ld+json\"]')]"
            ".map(s => s.textContent)")
    except Exception:
        return None

    for blob in blobs or []:
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict) or node.get("@type") != "JobPosting":
                continue
            for key in ("applicationUrl", "applicationContact", "url"):
                value = node.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return value
    return None


def from_rendered_page(page) -> str | None:
    """A wider selector sweep than enrichment runs."""
    for selector in WIDE_APPLY_SELECTORS:
        try:
            el = page.query_selector(selector)
            if not el:
                continue
            href = el.evaluate("e => e.href || null")
            if href and href != "#" and "javascript:" not in href:
                return href
        except Exception:
            continue

    # A button that posts rather than links: if the form action is a real URL,
    # that is where the application goes.
    try:
        action = page.evaluate(
            "() => { const f = document.querySelector('form[action*=\"apply\" i]');"
            " return f ? f.action : null; }")
        if action and action.startswith("http"):
            return action
    except Exception:
        pass
    return None


def _looks_like_an_application_form(page) -> bool:
    """A resume upload plus a real field count is what a form looks like."""
    try:
        return bool(page.evaluate(
            "() => !!document.querySelector('input[type=file]') "
            "&& document.querySelectorAll('input,select,textarea').length >= 8"))
    except Exception:
        return False


def from_clicking_apply(page, context) -> str | None:
    """Click the Apply control and record where it lands.

    Some boards render Apply as a `<button>` with no href and resolve the
    destination in JavaScript. Nothing static can read that — the URL does not
    exist until the click happens. Three of the stubborn rows were this shape.

    Navigation only. This opens the application form; it never fills or
    submits anything.
    """
    origin = page.url
    try:
        button = page.query_selector(
            "a:has-text('Apply'), button:has-text('Apply'), "
            "input[type=submit][value*='Apply' i]")
        if button is None:
            return None

        # The click either navigates this page or opens a new tab; watch both.
        try:
            with context.expect_page(timeout=8000) as popup:
                button.click(timeout=8000)
            landed = popup.value
            landed.wait_for_load_state("domcontentloaded", timeout=20000)
            url = landed.url
            landed.close()
            if url and url.startswith("http"):
                return url
        except Exception:
            page.wait_for_timeout(4000)

        # No new tab. Either the click navigated this page, or — more often —
        # it revealed the form in place. Bixal goes from 3 inputs to 66 with a
        # file upload and never changes its URL, so requiring the URL to move
        # rejected a page that had just become the application form.
        if page.url != origin and page.url.startswith("http"):
            return page.url
        if _looks_like_an_application_form(page):
            return page.url
    except Exception as e:
        log.debug("Apply click failed: %s", e)
    return None


def recover_offline(conn, limit: int | None = None) -> dict:
    """Method 1 only. No browser, no network — safe to run any time."""
    rows = conn.execute("""
        SELECT url, title FROM jobs
        WHERE (application_url IS NULL OR application_url = '')
          AND applied_at IS NULL
        ORDER BY fit_score DESC
    """).fetchall()
    if limit:
        rows = rows[:limit]

    fixed = 0
    for row in rows:
        found = (from_self_hosting_ats(row["url"])
                 or from_greenhouse_token(row["url"]))
        if not found:
            continue
        conn.execute("UPDATE jobs SET application_url = ? WHERE url = ?",
                     (found, row["url"]))
        fixed += 1
        log.info("apply URL recovered (self-hosting ATS): %s", row["title"])
    conn.commit()
    return {"checked": len(rows), "recovered": fixed,
            "remaining": len(rows) - fixed}


def recover_with_browser(conn, limit: int | None = None) -> dict:
    """Methods 2 and 3, for rows method 1 could not settle.

    One browser for the whole sweep. Never raises: a row that cannot be
    resolved is left alone for the next pass.
    """
    rows = conn.execute("""
        SELECT url, title FROM jobs
        WHERE (application_url IS NULL OR application_url = '')
          AND applied_at IS NULL
        ORDER BY fit_score DESC
    """).fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        return {"checked": 0, "recovered": 0, "remaining": 0}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright is not installed; cannot sweep pages")
        return {"checked": 0, "recovered": 0, "remaining": len(rows)}

    from applypilot import config

    fixed = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=config.get_chrome_path() or None,
                args=["--disable-blink-features=AutomationControlled"])
            try:
                browser_ctx = browser.new_context()
                page = browser_ctx.new_page()
                for row in rows:
                    found = None
                    try:
                        page.goto(row["url"], wait_until="domcontentloaded",
                                  timeout=45000)
                        page.wait_for_timeout(2500)
                        found = (from_json_ld(page)
                                 or from_rendered_page(page)
                                 or from_clicking_apply(page, browser_ctx))
                    except Exception as e:
                        log.debug("Sweep failed for %s: %s", row["title"], e)
                    if found:
                        conn.execute(
                            "UPDATE jobs SET application_url = ? WHERE url = ?",
                            (found, row["url"]))
                        fixed += 1
                        log.info("apply URL recovered (page sweep): %s -> %s",
                                 row["title"], found[:80])
                conn.commit()
            finally:
                browser.close()
    except Exception as e:
        log.warning("Browser sweep failed: %s", e)

    return {"checked": len(rows), "recovered": fixed,
            "remaining": len(rows) - fixed}


def recover(conn, use_browser: bool = True, limit: int | None = None) -> dict:
    """Run every method, cheapest first, and report what each one settled."""
    offline = recover_offline(conn, limit)
    result = {"offline": offline["recovered"], "browser": 0,
              "remaining": offline["remaining"]}
    if use_browser and offline["remaining"]:
        swept = recover_with_browser(conn, limit)
        result["browser"] = swept["recovered"]
        result["remaining"] = swept["remaining"]
    return result
