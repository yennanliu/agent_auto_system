"""
One-time interactive login for the LinkedIn Easy Apply automation.

LinkedIn guards login with 2FA / CAPTCHA / device checks, so we log in ONCE in a
real (headed) browser and persist the session (cookies). The linkedin_apply
automation then reuses that session on every run — the same pattern as the 104 /
Shopee / tasker.com.tw scripts.

Usage:
    uv run playwright install chromium      # first time only
    uv run python scripts/linkedin_login.py

A Chromium window opens at linkedin.com/login. Log in manually (complete any
CAPTCHA / 2FA), and once you can see your feed, return to the terminal and press
Enter. The session is written to LINKEDIN_STORAGE_STATE
(default: data/linkedin_state.json).
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright

from src.automation.browser_session import open_login_context, profile_dir

load_dotenv()

STATE_PATH = os.getenv("LINKEDIN_STORAGE_STATE", "data/linkedin_state.json")
USERNAME = os.getenv("LINKEDIN_USERNAME", "")
PASSWORD = os.getenv("LINKEDIN_PASSWORD", "")
_LOGIN_URL = "https://www.linkedin.com/login"


def _prefill(page: Page) -> None:
    """Best-effort: type the .env credentials into the login form so the user
    only has to solve the CAPTCHA/2FA. Silently skipped if fields aren't found."""
    if not (USERNAME and PASSWORD):
        return
    filled = False
    try:
        user = page.locator("#username, input[name='session_key']").first
        if user.count() and user.is_visible():
            user.fill(USERNAME, timeout=6000)
            filled = True
    except Exception:
        pass
    try:
        pw = page.locator("#password, input[name='session_password']").first
        if pw.count() and pw.is_visible():
            pw.fill(PASSWORD, timeout=6000)
            filled = True
    except Exception:
        pass
    print("✓ Pre-filled credentials from .env" if filled
          else "· Could not auto-fill — please enter credentials manually")


def main() -> int:
    out = Path(STATE_PATH)
    out.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = open_login_context(
            pw, user_data_dir=profile_dir("linkedin", STATE_PATH),
            locale="en-US", on_progress=print,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(_LOGIN_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        _prefill(page)

        print("\n" + "=" * 70)
        print("A browser window is open. Log in to LinkedIn manually")
        print("(handle any CAPTCHA / 2FA). When you can see your feed,")
        print("come back here and press Enter to save the session.")
        print("=" * 70)
        input("\nPress Enter once you are logged in... ")

        # Verify before saving. Snapshotting a logged-OUT page produces a
        # "session" that fails on every run with a confusing "not logged in"
        # error. Give the operator a couple of chances to finish any checkpoint.
        from src.automation.tools.linkedin_apply_tool import _AUTHED_SELECTORS

        def _looks_logged_in(page) -> bool:
            """Return True only when an authenticated signal is positively found.
            Unlike _looks_logged_out's lenient mid-run default, the login script
            must only save on explicit confirmation."""
            try:
                for sel in _AUTHED_SELECTORS:
                    if page.locator(sel).count() > 0:
                        return True
            except Exception:  # noqa: BLE001
                pass
            return False

        for attempt in range(3):
            if _looks_logged_in(page):
                break
            # On the final (3rd) attempt, skip the prompt — just fall through to abort
            if attempt < 2:
                print("\n⚠ LinkedIn still shows you as logged OUT (guest / sign-in "
                      "page). Saving now would produce a broken session. Finish "
                      "logging in — complete any CAPTCHA / 2FA / security checkpoint "
                      "until you can see your feed — then press Enter to re-check.")
                input("Press Enter to re-check (or Ctrl-C to abort)... ")
        else:
            print("\n✗ Still logged out after 3 checks — not saving a broken "
                  "session. Re-run once you can reach your LinkedIn feed.")
            return 1

        try:
            ctx.storage_state(path=str(out))
            print(f"\n✓ Session saved to {out.resolve()}")
            print(f"  Make sure .env has: LINKEDIN_STORAGE_STATE={STATE_PATH}")
        except Exception as exc:  # noqa: BLE001 — browser may have been closed manually
            print(f"\n✗ Could not save session (was the browser closed?): {exc}")
            return 1
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
