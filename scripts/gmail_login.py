"""One-time interactive Gmail login (ARCHITECTURE §6).

Opens a real browser window against a persistent profile, waits for a human to
sign in, then exits. Same pattern as `hc_login.py` — the session lives in the
profile directory and later runs read Gmail from it without re-authenticating.

Nothing here reads mail or touches credentials. It opens a window, watches the
URL to notice when sign-in finished, and closes.

Google is aggressive about refusing sign-in from browsers that look automated
("this browser or app may not be secure"). Two mitigations: drive the real
Chrome/Chromium binary rather than Playwright's bundled build, and turn off the
AutomationControlled blink feature. If Google still refuses, sign in to this
profile through a normal Chromium window instead:

    chromium --user-data-dir=~/.applypilot/gmail-profile
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE = Path.home() / ".applypilot" / "gmail-profile"
TIMEOUT = 900  # 15 minutes; 2FA and device prompts are slow


# The cookies Google sets only once a session is genuinely authenticated.
_AUTH_COOKIES = {"SID", "SSID", "HSID", "SAPISID", "__Secure-1PSID"}


def _signed_in(ctx) -> bool:
    """True once the context holds a real authenticated Google session.

    Checks cookies rather than the URL. The URL is unreliable: the user may
    finish sign-in in a second tab, Gmail rewrites the fragment constantly, and
    `page.url` can lag a redirect — all of which made an earlier URL-based
    check sit there reporting failure on a session that had plainly worked.
    """
    try:
        names = {c["name"] for c in ctx.cookies()
                 if "google" in (c.get("domain") or "")}
    except Exception:
        return False
    return _AUTH_COOKIES.issubset(names)


def main() -> int:
    PROFILE.mkdir(parents=True, exist_ok=True)

    from applypilot import config
    chrome = config.get_chrome_path()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE),
            executable_path=chrome or None,
            headless=False,
            viewport={"width": 1400, "height": 950},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://mail.google.com/", wait_until="domcontentloaded",
                      timeout=90000)
        except Exception as e:
            print(f"Could not open Gmail: {e}", file=sys.stderr)
            ctx.close()
            return 1

        print("Sign in to Gmail in the window that just opened.")
        print("Use the job-search account, not a personal one if you can help it.")
        print(f"The session is saved to {PROFILE} and this closes by itself.")
        sys.stdout.flush()

        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            try:
                if _signed_in(ctx):
                    # Let Gmail finish settling so the session cookies are
                    # written before the context closes.
                    time.sleep(5)
                    print(f"\nSigned in. Session saved to {PROFILE}")
                    sys.stdout.flush()
                    ctx.close()
                    return 0
            except Exception:
                # The page can be mid-navigation; that is not a failure.
                pass
            time.sleep(3)

        print("Timed out waiting for sign-in.", file=sys.stderr)
        ctx.close()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
