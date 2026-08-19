"""hiring.cafe discovery adapter.

This is the only discovery source at launch (CLAUDE.md section 5). Upstream
sources stay in the codebase but dormant.

## How the source actually works (verified 2026-08-19)

hiring.cafe has no public API. The architecture doc describes a
`POST /api/search-jobs` endpoint; that endpoint no longer exists — it 405s, and
`/api/search-jobs/get-total-count` 404s. The live site is a Next.js app behind a
Firebase login, and search results come back from the page-data route:

    GET https://hiringcafe.com/_next/data/{buildId}/index.json
        ?searchState={url-encoded json}&page={n}
    headers: x-nextjs-data: 1

`buildId` changes on every hiring.cafe deploy, so it is scraped from
`window.__NEXT_DATA__.buildId` on each run and never cached across runs.

Because the route requires an authenticated session, requests are issued from
inside a logged-in browser context (a persistent Chromium profile at
`~/.applypilot/hiringcafe-profile`) rather than from a bare HTTP client. That
profile is created by a one-time interactive login; if the session has expired,
discovery halts with instructions rather than silently returning nothing.

## Descriptions

Contrary to the architecture doc, hiring.cafe does NOT return job descriptions
inline — neither the search response nor the per-job route carries the posting
text. Rows are therefore written without `full_description`, land in the
`discovered` state, and the normal enrich stage scrapes `apply_url` for the
real text. Enrich is a required stage for this source, not a fallback.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Repo-versioned query artifacts. Never external saved searches (section 5).
QUERY_DIR = Path(__file__).resolve().parents[3] / "queries"
QUERIES = {
    "remote-usa": QUERY_DIR / "remote-usa.json",
    "hybrid-chicago": QUERY_DIR / "hybrid-chicago.json",
}

PROFILE_DIR = Path.home() / ".applypilot" / "hiringcafe-profile"

# Guard rails
MAX_PAGES = 25          # ~40 hits/page; far above any real day's volume
DEDUPE_WINDOW_DAYS = 14  # section 5: same company+title inside 14d is a skip
NAV_TIMEOUT_MS = 90_000


class QueryFileError(RuntimeError):
    """A query artifact is missing or malformed. The run must halt."""


class SessionError(RuntimeError):
    """The hiring.cafe browser session is missing or expired."""


# ---------------------------------------------------------------------------
# Query artifacts
# ---------------------------------------------------------------------------

def load_query(name: str) -> dict:
    """Load and validate one committed searchState artifact.

    Raises QueryFileError rather than guessing. The exact location tokens in
    these blobs were captured byte-exact from Abdur-Rahman's live saved
    searches and cannot be reconstructed.
    """
    path = QUERIES.get(name)
    if path is None:
        raise QueryFileError(
            f"Unknown query '{name}'. Known queries: {', '.join(sorted(QUERIES))}"
        )
    if not path.exists():
        raise QueryFileError(
            f"Query artifact missing: {path}\n\n"
            "Discovery cannot run without it, and the location tokens inside it "
            "cannot be guessed. To regenerate: open hiringcafe.com, run the "
            "saved search, copy the ?searchState=... URL, URL-decode it, and "
            f"save the JSON as {path.name}."
        )
    try:
        state = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise QueryFileError(f"Query artifact {path} is not valid JSON: {e}") from e

    validate_query(state, path)
    return state


def validate_query(state: dict, path: Path | str = "<memory>") -> None:
    """Schema-check a searchState blob against the real hiring.cafe shape.

    Validates what was observed live on 2026-08-19, NOT what the architecture
    doc describes (which is wrong on four points — see DECISIONS.md).
    """
    if not isinstance(state, dict):
        raise QueryFileError(f"{path}: searchState must be a JSON object")

    locations = state.get("locations")
    if not isinstance(locations, list) or not locations:
        raise QueryFileError(f"{path}: 'locations' must be a non-empty list")

    for i, loc in enumerate(locations):
        if not isinstance(loc, dict):
            raise QueryFileError(f"{path}: locations[{i}] must be an object")
        for key in ("formatted_address", "types", "workplace_types", "id"):
            if key not in loc:
                raise QueryFileError(f"{path}: locations[{i}] is missing '{key}'")
        wt = loc["workplace_types"]
        if not isinstance(wt, list) or not wt:
            raise QueryFileError(
                f"{path}: locations[{i}].workplace_types must be a non-empty list"
            )
        bad = set(wt) - {"Remote", "Hybrid", "Onsite"}
        if bad:
            raise QueryFileError(
                f"{path}: locations[{i}].workplace_types has unknown values: {sorted(bad)}"
            )

    if not state.get("jobTitleQuery"):
        raise QueryFileError(f"{path}: 'jobTitleQuery' is required and must be non-empty")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

_FETCH_JS = """
async ({buildId, state, page}) => {
    const qs = 'searchState=' + encodeURIComponent(JSON.stringify(state)) +
               (page ? '&page=' + page : '');
    const r = await fetch(`/_next/data/${buildId}/index.json?` + qs,
                          {headers: {'x-nextjs-data': '1'}});
    if (!r.ok) return {error: r.status};
    const j = await r.json();
    const pp = j.pageProps || {};
    return {
        hits: pp.ssrHits || [],
        page: pp.ssrPage,
        total: pp.ssrTotalCount,
        isLast: pp.ssrIsLastPage,
        err: pp.ssrError || null,
    };
}
"""


# Delay between result pages. The endpoint is not ours to hammer.
PAGE_DELAY_MS = 1500


def fetch_query(name: str, max_pages: int = MAX_PAGES) -> list[dict]:
    """Fetch every page of one query from inside the logged-in browser session."""
    from playwright.sync_api import sync_playwright

    state = load_query(name)

    if not PROFILE_DIR.exists():
        raise SessionError(
            f"No hiring.cafe browser session at {PROFILE_DIR}.\n"
            "Run `python scripts/hc_login.py` once and sign in when the window opens."
        )

    hits: list[dict] = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=True)
        try:
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto("https://hiringcafe.com/", wait_until="domcontentloaded",
                    timeout=NAV_TIMEOUT_MS)
            pg.wait_for_timeout(6000)

            title = (pg.title() or "")
            if "login" in title.lower():
                raise SessionError(
                    "hiring.cafe session has expired (landed on the login page).\n"
                    "Re-run `python scripts/hc_login.py` and sign in again."
                )

            build_id = pg.evaluate("() => window.__NEXT_DATA__ && window.__NEXT_DATA__.buildId")
            if not build_id:
                raise SessionError(
                    "Could not read window.__NEXT_DATA__.buildId — the hiring.cafe "
                    "frontend has changed shape. The adapter needs revisiting."
                )
            log.info("hiring.cafe buildId=%s query=%s", build_id, name)

            for page_no in range(max_pages):
                # Pace the pages. Everything else in this pipeline paces
                # deliberately; this loop hammered the endpoint as fast as it
                # could, and a run died on page 1 with "Failed to fetch".
                if page_no:
                    pg.wait_for_timeout(PAGE_DELAY_MS)

                res = None
                for attempt in range(2):
                    try:
                        res = pg.evaluate(_FETCH_JS, {"buildId": build_id,
                                                      "state": state,
                                                      "page": page_no})
                        break
                    except Exception as e:
                        # A network blip inside the page must not throw away
                        # the pages already collected. Retry once, then stop
                        # the query and keep what we have — park and continue,
                        # the same rule the rest of the pipeline follows.
                        log.warning("hiring.cafe %s page %d fetch failed "
                                    "(attempt %d/2): %s", name, page_no,
                                    attempt + 1, e)
                        if attempt == 0:
                            pg.wait_for_timeout(PAGE_DELAY_MS * 4)
                if res is None:
                    log.warning("hiring.cafe %s stopping at page %d; keeping "
                                "the %d hit(s) already collected",
                                name, page_no, len(hits))
                    break

                if res.get("error"):
                    log.warning("hiring.cafe page %d returned HTTP %s",
                                page_no, res["error"])
                    break
                if res.get("err"):
                    log.warning("hiring.cafe page %d returned ssrError: %s",
                                page_no, res["err"])
                    break

                page_hits = res.get("hits") or []
                hits.extend(page_hits)
                log.info("hiring.cafe %s page %d: %d hits (total %s)",
                         name, page_no, len(page_hits), res.get("total"))

                if res.get("isLast") or not page_hits:
                    break
            else:
                log.warning("hiring.cafe %s hit the %d-page cap; results truncated",
                            name, max_pages)
        finally:
            ctx.close()

    return hits


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_hit(hit: dict, query_name: str) -> dict | None:
    """Map one search hit onto the shared discovery row shape.

    Returns None for rows that can't be applied to (expired, or no apply URL).
    """
    if hit.get("is_expired"):
        return None

    apply_url = hit.get("apply_url")
    if not apply_url or not apply_url.startswith("http"):
        return None

    info = hit.get("job_information") or {}
    v5 = hit.get("v5_processed_job_data") or {}
    company_data = hit.get("enriched_company_data") or {}

    title = info.get("title") or info.get("job_title_raw") or v5.get("core_job_title")
    if not title:
        return None

    # Multi-country postings are common (a Chicago req also listed in Shanghai,
    # Hong Kong, Sydney). US entries sort first so the location text keeps the
    # city that made the posting match, and so downstream location filters see
    # a US location instead of a truncated foreign list.
    cities = v5.get("workplace_cities") or []
    us_first = sorted(cities, key=lambda c: not c.strip().endswith(", US"))
    workplace_type = v5.get("workplace_type")
    location = ", ".join(us_first[:3]) if us_first else workplace_type

    company = company_data.get("name") or hit.get("board_token") or "unknown"

    return {
        "url": apply_url,
        "application_url": apply_url,
        "title": title,
        "location": location,
        # No description is available from this source — enrich scrapes it.
        "description": None,
        "full_description": None,
        "posted_at": v5.get("estimated_publish_date"),
        "employer_name": company,
        # Not persisted by insert_normalized_jobs; kept for dedupe + logging.
        "_query": query_name,
        "_ats": hit.get("source"),
    }


_WORD_RE = re.compile(r"[a-z0-9]+")


def _title_key(title: str) -> frozenset:
    """Bag-of-words key for fuzzy title comparison."""
    stop = {"the", "a", "an", "of", "and", "for", "to", "in", "at", "senior",
            "sr", "jr", "junior", "i", "ii", "iii", "1", "2", "3"}
    return frozenset(w for w in _WORD_RE.findall(title.lower()) if w not in stop)


def _titles_match(a: str, b: str) -> bool:
    """True when two titles are the same role modulo wording."""
    ka, kb = _title_key(a), _title_key(b)
    if not ka or not kb:
        return False
    overlap = len(ka & kb)
    return overlap / max(len(ka), len(kb)) >= 0.8


def dedupe_recent(conn: sqlite3.Connection, rows: list[dict],
                  window_days: int = DEDUPE_WINDOW_DAYS) -> tuple[list[dict], int]:
    """Drop rows matching a (company, title) seen within the recent window.

    URL-level duplicates are already handled by the jobs table's primary key;
    this catches the same role reposted under a new requisition URL.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    recent = conn.execute(
        "SELECT site, title FROM jobs WHERE discovered_at > ? "
        "AND site IS NOT NULL AND title IS NOT NULL",
        (cutoff,),
    ).fetchall()
    seen = [(r["site"], r["title"]) for r in recent]

    kept: list[dict] = []
    skipped = 0
    for row in rows:
        company, title = row["employer_name"], row["title"]
        dupe = any(c == company and _titles_match(t, title) for c, t in seen)
        if dupe:
            skipped += 1
            log.debug("dedupe: skipping %s @ %s (seen in last %dd)",
                      title, company, window_days)
            continue
        kept.append(row)
        seen.append((company, title))
    return kept, skipped


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run_hiringcafe_discovery(queries: list[str] | None = None,
                             workers: int = 1) -> dict:
    """Run discovery for each committed query and store the results.

    `workers` is accepted for signature parity with the other discovery
    sources; hiring.cafe is fetched serially through one browser session.
    """
    from applypilot.database import get_connection
    from applypilot.discovery.ats_common import insert_normalized_jobs

    names = queries if queries else list(QUERIES)
    conn = get_connection()
    stats: dict = {}

    for name in names:
        hits = fetch_query(name)
        rows = [r for r in (normalize_hit(h, name) for h in hits) if r]
        rows, skipped_dupes = dedupe_recent(conn, rows)

        new, existing = insert_normalized_jobs(
            conn, rows, default_site="hiring.cafe", strategy=f"hiringcafe:{name}")

        ats_mix: dict[str, int] = {}
        for r in rows:
            ats_mix[r["_ats"]] = ats_mix.get(r["_ats"], 0) + 1

        stats[name] = {"hits": len(hits), "normalized": len(rows),
                       "new": new, "existing": existing,
                       "deduped": skipped_dupes, "ats_mix": ats_mix}
        log.info("hiring.cafe %s: %d hits -> %d new, %d existing, %d deduped",
                 name, len(hits), new, existing, skipped_dupes)

    return stats
