# Running Agent Auto System in Docker

This document covers everything needed to build, run, and operate the system
inside a container — from a quick one-liner to production-style deployment with
persistent storage.

---

## Prerequisites

| Tool | Minimum version | Install |
|---|---|---|
| Docker | 24.x | [docs.docker.com](https://docs.docker.com/get-docker/) |
| Docker Compose | v2 (bundled with Docker Desktop) | — |
| An OpenAI API key | — | [platform.openai.com](https://platform.openai.com/api-keys) |

---

## Quick start — one command

```bash
docker run -d \
  --name agent-auto \
  -p 8000:8000 \
  -e OPENAI_API_KEY=sk-... \
  -v agent_data:/app/data \
  agent-auto-system:local
```

Then open **http://localhost:8000** in your browser.

> The image must be built locally first (see [Build the image](#build-the-image)).
> There is no published registry image yet.

---

## Build the image

```bash
# From the project root (same directory as Dockerfile)
docker build --target runtime --tag agent-auto-system:local .
```

The build is two-stage:

| Stage | Base | What it does |
|---|---|---|
| `builder` | `python:3.12-slim` | Installs Python dependencies with `uv sync --frozen --no-dev` |
| `runtime` | `python:3.12-slim` | Copies the venv from builder, installs Playwright Chromium + system libs, copies app source |

First build takes **4–8 min** (Playwright Chromium download ~200 MB).
Subsequent builds are fast because dependency layers are cached unless
`pyproject.toml` or `uv.lock` changes.

---

## Run with docker compose (recommended)

```bash
# 1. Copy and fill in the environment file
cp .env.example .env
#    → set OPENAI_API_KEY (required)
#    → set GMAIL_ADDRESS + GMAIL_APP_PASSWORD (optional, for email sender)

# 2. Build and start
docker compose up --build -d

# 3. Follow logs
docker compose logs -f

# 4. Stop
docker compose down
```

The compose file mounts a named volume `app_data` at `/app/data` so the
SQLite database survives container restarts and re-deploys.

---

## Environment variables

Pass these via `-e`, `--env-file`, or the `env_file:` key in compose.

| Variable | Required | Default in image | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | **Yes** | — | API key for all LLM-backed automations |
| `DATABASE_URL` | No | `sqlite:///./data/auto.db` | SQLAlchemy DB URL; change to use an external DB |
| `GMAIL_ADDRESS` | No | — | Gmail address for the Email Sender automation |
| `GMAIL_APP_PASSWORD` | No | — | Gmail App Password (not your login password) |
| `LOG_FORMAT` | No | `text` | Set `json` for one-JSON-object-per-line logs (recommended when a log collector reads the container's output); see [dev-notes](dev-notes.md#structured-logging) |
| `LOG_LEVEL` | No | — | Root log level (`DEBUG`/`INFO`/…); `INFO` in json mode when unset |

> **Never bake secrets into the image.** The `.env` file is in `.dockerignore`
> and will never be included in a build context.

---

## Persistent data

The SQLite database is stored at `/app/data/auto.db` inside the container.
Mount a volume to persist it:

```bash
# Named volume (docker manages the path)
docker run ... -v agent_data:/app/data agent-auto-system:local

# Bind mount to a host directory
docker run ... -v $(pwd)/data:/app/data agent-auto-system:local
```

Without a volume the database is lost when the container is removed.

---

## Useful commands

```bash
# Open a shell inside the running container
docker exec -it agent-auto bash

# Tail live application logs
docker logs -f agent-auto

# Check the health status
docker inspect --format='{{.State.Health.Status}}' agent-auto

# Manually hit the health endpoint
curl http://localhost:8000/health

# Run unit + integration tests inside the container
# (requires rebuilding without --no-dev; use the host uv instead for testing)
uv run pytest tests/unit tests/integration -v

# Copy the database out of the container
docker cp agent-auto:/app/data/auto.db ./backup.db

# Rebuild after a code change (compose handles restart automatically)
docker compose up --build -d
```

---

## Example API calls against a running container

```bash
BASE=http://localhost:8000

# Health check
curl $BASE/health

# List all automation types and their source code
curl $BASE/api/system | python3 -m json.tool | head -60

# Create a Hacker News Digest job
curl -X POST $BASE/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"name":"HN Digest","job_type":"hacker_news_digest","payload":{"limit":5}}'

# Trigger the job (use the id returned above)
curl -X POST $BASE/api/jobs/1/run

# Stream live progress (press Ctrl-C to stop)
curl -N $BASE/api/runs/1/stream

# Get all run history
curl "$BASE/api/runs?limit=20" | python3 -m json.tool

# Get performance stats
curl $BASE/api/stats | python3 -m json.tool

# Delete all completed runs
curl -X DELETE "$BASE/api/runs?delete_all=true"

# Open the interactive API docs
open $BASE/docs
```

---

## Image size and build tips

| Layer | Approx. size |
|---|---|
| Base Python 3.12-slim | ~130 MB |
| uv + Python packages | ~400 MB |
| Playwright Chromium | ~290 MB |
| App source + UI | ~2 MB |
| **Total (compressed)** | **~500 MB** |

**To reduce rebuild time:**
- Keep `pyproject.toml` and `uv.lock` changes minimal; those layers are
  cached and only invalidated when dependencies change.
- Use `--cache-from agent-auto-system:local` in CI to reuse the layer cache
  from a previous build.

---

## CI/CD

The repository ships a GitHub Actions workflow at
`.github/workflows/ci.yml` with two jobs:

| Job | What it does |
|---|---|
| `test` | Runs `uv run pytest tests/unit tests/integration` on the host runner (no Docker, no real API key needed) |
| `docker-smoke` | Builds the image, starts the container with a placeholder key, then runs 8 smoke tests against the live API |

The smoke tests exercise:

```
GET  /health                        → {status: ok}
GET  /                              → UI HTML 200
GET  /api/system                    → ≥5 agents, ≥6 tools, email_sender present
POST /api/jobs                      → job created with correct type
GET  /api/jobs                      → list contains the created job
GET  /api/stats                     → all keys present, 7-day trend
DELETE /api/runs?delete_all=true    → 0 deleted (no runs yet)
GET  /docs                          → OpenAPI UI 200
```

No real OpenAI or Gmail calls are made during CI — only the structural
endpoints are tested. To run a full end-to-end automation, add
`OPENAI_API_KEY` as a GitHub Actions secret and trigger a job from
the smoke test.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Container exits immediately | Missing `OPENAI_API_KEY` | Pass `-e OPENAI_API_KEY=sk-...` |
| `playwright._impl._errors.Error: Executable doesn't exist` | Browser not installed | The `playwright install` step in the Dockerfile should handle this; rebuild the image |
| Database locked errors | Multiple containers sharing the same file | Use a single container or switch to PostgreSQL |
| Port 8000 already in use | Another process on the host | Change host port: `-p 8001:8000` |
| `uv: command not found` inside container | uv not in PATH | The Dockerfile copies uv to `/usr/local/bin`; verify with `docker exec agent-auto which uv` |
