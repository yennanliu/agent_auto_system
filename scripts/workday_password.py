"""Store the password of a Workday candidate account (one per employer tenant).

ats_apply signs in to a Workday employer only when its password is stored;
otherwise that employer's jobs are listed for manual apply. Create the account
yourself on the employer's careers site (with the same e-mail as the run's
``email``), then:

    uv run python scripts/workday_password.py set 3m.wd1      # prompts, never echoes
    uv run python scripts/workday_password.py status 3m.wd1
    uv run python scripts/workday_password.py clear 3m.wd1

The password is encrypted in the settings store with APP_SECRET (set it in
.env first: a store encrypted with the dev default is not secret). Alternatives
read by the adapter: the env var WORKDAY_PASSWORD_<TENANT>_<WDN>, or (macOS) a
Keychain item with service "agent-auto-system workday <tenant>.<wdN>" and the
e-mail as account.
"""
import argparse
import getpass
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("set", "status", "clear"))
    ap.add_argument("host", help="the tenant, e.g. 3m.wd1 (from <tenant>.<wdN>.myworkdayjobs.com)")
    args = ap.parse_args(argv)
    host = args.host.strip().lower()
    if not re.fullmatch(r"[a-z0-9-]+\.wd\d+", host):
        ap.error(f"'{args.host}' is not a Workday tenant like 3m.wd1")

    from dotenv import load_dotenv
    load_dotenv()
    from src.database import init_db
    from src.settings_store import (
        clear_workday_password,
        get_workday_password,
        set_workday_password,
    )
    init_db()

    if args.action == "set":
        if not os.getenv("APP_SECRET"):
            print("APP_SECRET is not set: the password would be encrypted with the dev default key.",
                  file=sys.stderr)
            return 2
        pw = getpass.getpass(f"Workday password for {host}: ")
        if not pw or pw != getpass.getpass("Again: "):
            print("Passwords empty or different; nothing stored.", file=sys.stderr)
            return 1
        set_workday_password(host, pw)
        print(f"Stored (encrypted) for {host}.")
    elif args.action == "clear":
        clear_workday_password(host)
        print(f"Cleared {host}.")
    else:
        print(f"{host}: {'stored' if get_workday_password(host) else 'not stored'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
