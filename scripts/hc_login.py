"""One-time interactive hiring.cafe login.

Opens a real browser window against the persistent profile the discovery
adapter uses, waits for a human to sign in, then exits. Re-run whenever
discovery reports an expired session.
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE = Path.home() / ".applypilot" / "hiringcafe-profile"
TIMEOUT = 900


def main() -> int:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), headless=False, viewport={"width": 1400, "height": 900})
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto("https://hiringcafe.com/", wait_until="domcontentloaded", timeout=90000)

        print("Sign in to hiring.cafe in the window that just opened. "
              "This will close by itself once you're through.")
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            if "login" not in (pg.title() or "").lower():
                print("Signed in. Session saved to", PROFILE)
                ctx.close()
                return 0
            time.sleep(5)

        print("Timed out waiting for login.", file=sys.stderr)
        ctx.close()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
