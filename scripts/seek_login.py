"""
One-time interactive login for the Seek / JobStreet Quick Apply automation
(seek_apply).

Seek AU / NZ and JobStreet SG / MY / PH / ID run on one platform but are
separate logins, so there are two saved sessions:

    --site au | nz             → SEEK_STORAGE_STATE      (default data/seek_state.json)
    --site sg | my | ph | id   → JOBSTREET_STORAGE_STATE (default data/jobstreet_state.json)

Usage:
    uv run playwright install chromium        # first time only
    uv run python scripts/seek_login.py --site au

A Chrome window opens on the market's sign-in page (a persistent profile under
data/, so Cloudflare's check is remembered between logins). Sign in by hand —
e-mail code / password / Google, whatever your account uses — and once you see
your name in the header, come back here and press Enter. The session is saved
under data/ (git-ignored); seek_apply reuses it on every run. Nothing is ever
applied to from this script.
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from src.automation.browser_session import _seek_logged_in, open_login_context, profile_dir
from src.automation.tools.seek_apply_tool import SITES, resolve_site, site_info

load_dotenv()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", default="au", help=f"market: {', '.join(SITES)} (default au)")
    args = ap.parse_args()
    code = resolve_site(args.site)
    if code is None:
        print(f"Unknown site '{args.site}' — use one of: {', '.join(SITES)}")
        return 2
    info = site_info(code)
    state_path = os.getenv(info["state_env"], info["state_default"])
    out = Path(state_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = open_login_context(pw, user_data_dir=profile_dir(info["session"], state_path),
                                 locale="en-US", on_progress=print)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{info['base']}/oauth/login?returnUrl=%2F", wait_until="domcontentloaded")

        print("\n" + "=" * 70)
        print(f"A browser window is open on {info['label']}. Sign in by hand (solve any")
        print("'verify you are human' check). When the header shows your name,")
        print("come back here and press Enter to save the session.")
        print("=" * 70)
        input("\nPress Enter once you are signed in... ")

        # Verify before saving: a logged-out snapshot fails every run with a
        # confusing "not logged in". The sign-in check is not verified against
        # a live account yet, so when unsure the operator decides.
        for attempt in range(3):
            if _seek_logged_in(page):
                break
            if attempt < 2:
                print("\n⚠ The page still looks signed OUT (or is on the sign-in / bot-check page). "
                      "Finish signing in until you see the home page with your name, then press Enter.")
                input("Press Enter to re-check (or Ctrl-C to abort)... ")
        else:
            answer = input("\n? Could not confirm the sign-in automatically. If the browser shows "
                           "you signed in, save anyway? [y/N] ").strip().lower()
            if answer != "y":
                print("✗ Not saved.")
                ctx.close()
                return 1

        try:
            ctx.storage_state(path=str(out))
            print(f"\n✓ Session saved to {out.resolve()}")
            print(f"  ({info['state_env']}={state_path}; used by seek_apply for site={code})")
        except Exception as exc:  # noqa: BLE001 — browser may have been closed manually
            print(f"\n✗ Could not save session (was the browser closed?): {exc}")
            return 1
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
