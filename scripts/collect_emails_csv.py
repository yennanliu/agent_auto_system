#!/usr/bin/env python3
"""Collect unique emails from every `email_collect` run into a local CSV.

Incremental + idempotent: rows already in the CSV are kept untouched and only
emails not yet present get appended, so it's safe to re-run after each new batch
of collection runs. Dedup key is the lowercased email address.

Columns: email, company, conf

Usage:
    uv run python scripts/collect_emails_csv.py                 # defaults
    uv run python scripts/collect_emails_csv.py --conf medium   # only one tier
    uv run python scripts/collect_emails_csv.py --out leads.csv --db data/auto.db
"""
import argparse
import csv
import json
import os
import sqlite3

DEFAULT_DB = "data/auto.db"
DEFAULT_OUT = "data/collected_emails.csv"
FIELDS = ["email", "company", "conf"]


def iter_leads(db_path: str):
    """Yield (email, company, conf) for every lead in every successful
    email_collect run, newest run first (so the newest company/label wins)."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT r.result FROM run r JOIN job j ON r.job_id = j.id "
        "WHERE j.job_type = 'email_collect' AND r.status = 'success' "
        "AND r.result IS NOT NULL ORDER BY r.id DESC"
    ).fetchall()
    conn.close()
    for (result,) in rows:
        try:
            leads = json.loads(result).get("leads", [])
        except (json.JSONDecodeError, TypeError):
            continue
        for lead in leads:
            email = (lead.get("email") or "").strip()
            if not email:
                continue
            yield email, (lead.get("company") or "").strip(), lead.get("confidence") or ""


def load_existing(out_path: str) -> set[str]:
    """Lowercased emails already in the CSV (empty set if the file is new)."""
    if not os.path.exists(out_path):
        return set()
    with open(out_path, newline="", encoding="utf-8") as f:
        return {(r.get("email") or "").strip().lower()
                for r in csv.DictReader(f) if r.get("email")}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--conf", default=None,
                    help="keep only this confidence tier (e.g. medium)")
    args = ap.parse_args()

    existing = load_existing(args.out)
    seen = set(existing)          # everything we won't write again
    new_rows: list[dict] = []
    for email, company, conf in iter_leads(args.db):
        if args.conf and conf != args.conf:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        new_rows.append({"email": email, "company": company, "conf": conf})

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    is_new_file = not os.path.exists(args.out)
    with open(args.out, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new_file:
            w.writeheader()
        w.writerows(new_rows)

    total = len(existing) + len(new_rows)
    print(f"CSV: {args.out}")
    print(f"  already present : {len(existing)}")
    print(f"  newly appended  : {len(new_rows)}")
    print(f"  total unique    : {total}")


if __name__ == "__main__":
    main()
