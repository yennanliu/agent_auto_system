"""
One-time interactive login for the 104.com.tw auto-apply automation.

104 guards password login with captcha / SMS-OTP / device checks, so we log in
ONCE in a real (headed) browser and persist the session (cookies). The
tw104_apply automation then reuses that session headlessly on every run — the
same pattern as the Shopee / tasker.com.tw scripts.

Usage:
    uv run playwright install chromium      # first time only
    uv run python scripts/104_login.py

A Chromium window opens at 104.com.tw. Log in manually (complete any captcha /
OTP), and once you can see your account (我的104), return to the terminal and
press Enter. The session is written to TW104_STORAGE_STATE
(default: data/tw104_state.json).
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright

from src.automation.browser_session import open_login_context, profile_dir

load_dotenv()

STATE_PATH = os.getenv("TW104_STORAGE_STATE", "data/tw104_state.json")
USERNAME = os.getenv("TW104_USERNAME", "")
PASSWORD = os.getenv("TW104_PASSWORD", "")
_LOGIN_URL = "https://login.104.com.tw/login"


def _prefill(page: Page) -> None:
    """Best-effort: type the .env credentials into the login form so the user
    only has to solve the captcha/OTP. Silently skipped if fields aren't found."""
    if not (USERNAME and PASSWORD):
        return
    filled = False
    for sel in (
        'input[name="identity"]', 'input[name*="account" i]', 'input[name*="email" i]',
        'input[type="email"]', 'input[name*="username" i]',
        'input[placeholder*="身分證"]', 'input[placeholder*="帳號"]',
        'input[placeholder*="Email" i]',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.fill(USERNAME, timeout=6000)
                filled = True
                break
        except Exception:
            continue
    try:
        pw = page.locator('input[type="password"]').first
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
            pw, user_data_dir=profile_dir("tw104", STATE_PATH), on_progress=print
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(_LOGIN_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        _prefill(page)

        print("\n" + "=" * 70)
        print("A browser window is open. Log in to 104.com.tw manually")
        print("(handle any captcha / OTP). When you can see your account (我的104),")
        print("come back here and press Enter to save the session.")
        print("=" * 70)
        input("\nPress Enter once you are logged in... ")

        try:
            ctx.storage_state(path=str(out))
            print(f"\n✓ Session saved to {out.resolve()}")
            print(f"  Make sure .env has: TW104_STORAGE_STATE={STATE_PATH}")
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
