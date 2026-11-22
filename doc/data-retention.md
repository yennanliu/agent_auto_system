# Data retention & personal-data handling

Runs keep their progress `log` and `result` JSON in the database. For `email_collect`
the result holds leads, and with `gcis_enrich` those include `responsible` (負責人) —
a natural person's name, i.e. personal data under PDPA / GDPR. `profit_health_check`
additionally stores uploaded CSVs (`uploads/<upload_id>/`) and a rendered PDF
(`reports/<run_id>.pdf`). Without a policy all of this is kept forever.

Code: `src/retention.py` (policy, purge, export/erase) · `src/routers/retention.py` (admin API).

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `RETENTION_ENABLED` | `0` (**off**) | `1` starts the automatic daily purge. Off by default because deletion is irreversible — upgrading never starts deleting data on its own. |
| `RETENTION_RUN_DAYS` | `90` | Finished runs (and their report PDFs) older than this are deleted. `0` = never purge runs. |
| `RETENTION_UPLOAD_DAYS` | `30` | Upload folders older than this **and not referenced by any job** are deleted. `0` = never purge uploads. |
| `RETENTION_INTERVAL_HOURS` | `24` | Hours between automatic sweeps. |

A malformed or negative `*_DAYS` value disables that category (logged as a warning)
rather than being guessed. The windows also apply to a manual purge (below) even when
`RETENTION_ENABLED=0`.

## What the purge deletes — and what it never touches

One sweep (`retention.purge_expired()`), started from `main.lifespan` about a minute
after boot and then every `RETENTION_INTERVAL_HOURS`:

1. **Runs** whose `finished_at` (or `started_at` if never finished) is older than
   `RETENTION_RUN_DAYS`, plus `reports/<run_id>.pdf` for each.
2. **Orphan report PDFs** — `reports/<n>.pdf` older than the same window whose run row
   no longer exists (left behind by runs deleted before this feature).
3. **Uploads** — `uploads/<upload_id>/` whose newest file is older than
   `RETENTION_UPLOAD_DAYS`.

It never deletes:

- runs that are `pending` or `running`, whatever their age;
- an upload whose id appears in **any existing job's payload** (including nested
  pipeline steps) — that job can still be re-run or fire on a schedule. Delete the job
  first if the upload should go;
- jobs, users, settings, custom automations;
- anything in `uploads/` / `reports/` that isn't named like one of our artefacts
  (32-hex upload id, `<run_id>.pdf`), and anything reached through a symlink. Files are
  only removed as direct children of the configured `UPLOAD_DIR` / reports directory.

Aggregate stats (`/api/stats`, Analytics) are computed from run rows, so history older
than the window disappears from them once purged.

Independently of the purge, deleting a run through the API (`DELETE /api/runs/{id}`,
bulk delete) now also deletes its report PDF.

## Admin API

All routes are admin-only.

| Route | Purpose |
|---|---|
| `GET /api/admin/retention` | Effective policy + summary of the last automatic sweep |
| `POST /api/admin/retention/purge` | Sweep now. **Dry run by default** (returns what would be deleted); add `?dry_run=false` to delete |
| `GET /api/admin/users/{id}/export` | Zip of one user's data: `data.json` (account without the password hash, jobs, runs incl. `result`/`log`), the upload CSVs their jobs reference, their runs' PDFs |
| `DELETE /api/admin/users/{id}/data?confirm=<username>` | Irreversibly erase that same data |

```bash
# Preview, then run, a purge
curl -b cookies.txt -X POST 'http://localhost:8000/api/admin/retention/purge'
curl -b cookies.txt -X POST 'http://localhost:8000/api/admin/retention/purge?dry_run=false'

# Subject-access export, then erasure, for user 7 ("bob")
curl -b cookies.txt -o bob.zip 'http://localhost:8000/api/admin/users/7/export'
curl -b cookies.txt -X DELETE 'http://localhost:8000/api/admin/users/7/data?confirm=bob'
```

**Scope of "a user's data"** (identical for export and erase): jobs they created
(`job.created_by`), runs they triggered (`run.user_id`), and every run of their jobs —
including ones another user triggered, since those results derive from this user's job
payload. Erase also removes the matching PDFs and the uploads their jobs reference,
except an upload still referenced by another user's job.

Erase:

- returns `409` and deletes nothing while any of those runs is `pending`/`running` —
  cancel them first;
- keeps the account. Remove it afterwards with `DELETE /api/admin/users/{id}`.

## Known limits

- Rows with no owner (`created_by` / `user_id` NULL — created before ownership was
  tracked, or by the desktop single-user mode) belong to no user: export/erase skip
  them; the age-based purge still covers the runs.
- Uploads carry no owner. One that was uploaded but never attached to a job can't be
  attributed to a user; it is only removed by the age-based purge.
- This addresses *our* data subjects' rights over a **user account's** data. Erasing
  one third-party lead (e.g. a single 負責人 across all runs) is not implemented;
  shorten `RETENTION_RUN_DAYS` or delete the affected runs.
- Not covered: Langfuse traces (configure retention in Langfuse), server logs,
  `data/screenshots/` debug screenshots, browser `storage_state` files, backups.
- The timer is in-memory and per process: with several workers each runs the
  (idempotent) sweep; a restart re-anchors it.
- The export is built in memory — fine for typical accounts, heavy for users with
  very large histories.
