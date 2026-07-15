"""
One-time interactive login for the tasker.com.tw auto-apply automation.

tasker.com.tw is a JS-rendered SPA whose login is phone-based and frequently
guarded by captcha / SMS-OTP, so we log in ONCE in a real (headed) browser and
persist the session. The tasker_apply automation then reuses that session
headlessly on every run.

Usage:
    uv run playwright install chromium      # first time only
    uv run python scripts/tasker_login.py

A Chromium window opens at tasker.com.tw/auth/login. Log in manually (complete
any captcha / OTP). Once you can see your account, return to the terminal and
press Enter. The session is written to TASKER_STORAGE_STATE
(default: data/tasker_state.json).
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from src.automation.browser_session import open_login_context, profile_dir

load_dotenv()

STATE_PATH = os.getenv("TASKER_STORAGE_STATE", "data/tasker_state.json")
USERNAME = os.getenv("TASKER_USERNAME", "")
PASSWORD = os.getenv("TASKER_PASSWORD", "")
_LOGIN_URL = "https://www.tasker.com.tw/auth/legacy" if "@" in USERNAME \
    else "https://www.tasker.com.tw/auth/login"


def _prefill(page) -> None:
    """Best-effort: type the .env credentials so you only solve the captcha/OTP."""
    if not (USERNAME and PASSWORD):
        return
    filled = False
    for sel in (
        'input[type="email"]', 'input[name*="account" i]', 'input[name*="email" i]',
        'input[name*="phone" i]', 'input[name*="mobile" i]',
        'input[placeholder*="帳號"]', 'input[placeholder*="信箱"]',
        'input[placeholder*="手機"]', 'input[placeholder*="電話"]',
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
            pw, user_data_dir=profile_dir("tasker", STATE_PATH), on_progress=print
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(_LOGIN_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        _prefill(page)

        print("\n" + "=" * 70)
        print("A browser window is open. Log in to tasker.com.tw manually")
        print("(handle any captcha / OTP). When you can see your account,")
        print("come back here and press Enter to save the session.")
        print("=" * 70)
        input("\nPress Enter once you are logged in... ")

        try:
            ctx.storage_state(path=str(out))
            print(f"\n✓ Session saved to {out.resolve()}")
            print(f"  Make sure .env has: TASKER_STORAGE_STATE={STATE_PATH}")
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
