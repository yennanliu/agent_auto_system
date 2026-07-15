"""
One-time interactive login for the Shopee seller-scraper automation.

Shopee blocks headless password logins with captcha / SMS-OTP / device checks,
so we log in ONCE in a real (headed) browser and persist the session. The
shopee_seller_scraper tool then reuses that session headlessly on every run.

Usage:
    uv run playwright install chromium      # first time only
    uv run python scripts/shopee_login.py

A Chromium window opens at shopee.tw. Log in manually (complete any captcha /
OTP), and once you can see your account, return to the terminal and press Enter.
The session is written to SHOPEE_STORAGE_STATE (default: data/shopee_state.json).
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from src.automation.browser_session import open_login_context, profile_dir

load_dotenv()

STATE_PATH = os.getenv("SHOPEE_STORAGE_STATE", "data/shopee_state.json")
USERNAME = os.getenv("SHOPEE_USERNAME", "")
PASSWORD = os.getenv("SHOPEE_PASSWORD", "")


def _prefill(page) -> None:
    """Best-effort: type the .env credentials into the login form so the user
    only has to solve the captcha/OTP. Silently skipped if fields aren't found."""
    if not (USERNAME and PASSWORD):
        return
    try:
        page.locator('input[name="loginKey"]').first.fill(USERNAME, timeout=8000)
        page.locator('input[name="password"]').first.fill(PASSWORD, timeout=8000)
        print("✓ Pre-filled username/password from .env")
    except Exception:
        print("· Could not auto-fill the form — please enter credentials manually")


def main() -> int:
    out = Path(STATE_PATH)
    out.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = open_login_context(
            pw, user_data_dir=profile_dir("shopee", STATE_PATH), on_progress=print
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://shopee.tw/buyer/login", wait_until="domcontentloaded")
        _prefill(page)

        print("\n" + "=" * 70)
        print("A browser window is open. Log in to Shopee manually")
        print("(handle any captcha / OTP). When you can see your account,")
        print("come back here and press Enter to save the session.")
        print("=" * 70)
        input("\nPress Enter once you are logged in... ")

        try:
            ctx.storage_state(path=str(out))
            print(f"\n✓ Session saved to {out.resolve()}")
            print(f"  Make sure .env has: SHOPEE_STORAGE_STATE={STATE_PATH}")
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
