# Agent Auto System — Design Document

> **Status**: Draft · **Date**: 2026-05-14  
> **Stack**: Python · uv · CrewAI · OpenAI · FastAPI · SQLite · Playwright · pytest

---

## 1. Overview

A CrewAI-powered automation platform that runs background jobs driven by AI agents. The first job is auto-filling a Google Form on behalf of a user. A minimal HTML/JS frontend shows the landing page and automation run history.

---

## 2. Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| Agent framework | CrewAI (Flows + Crews) | Structured orchestration with autonomous agents |
| LLM | OpenAI GPT-4o-mini | Cost-efficient, tool-calling support |
| Package manager | uv | Fast, lockfile-based, replaces pip/venv |
| Backend | FastAPI | Async, auto-docs, minimal boilerplate |
| Database | SQLite (via SQLModel) | Zero-ops, good for single-node automation |
| Browser automation | Playwright (Python) | Reliable form interaction, headless support |
| Frontend | HTML + Vanilla JS | Simple, no build step |

---

## 3. Project Structure

```
agent_auto_system/
├── .env                          # API keys (never committed)
├── pyproject.toml                # uv project + dependencies
├── doc/
│   └── design.md                 # this file
├── src/
│   ├── main.py                   # FastAPI app entry point
│   ├── database.py               # SQLite connection + init
│   ├── models.py                 # SQLModel table definitions
│   ├── routers/
│   │   ├── jobs.py               # Job CRUD endpoints
│   │   └── runs.py               # Run history endpoints
│   └── automation/
│       ├── flows/
│       │   └── form_fill_flow.py # CrewAI Flow orchestration
│       ├── crews/
│       │   └── form_crew/
│       │       ├── crew.py       # @CrewBase class
│       │       └── config/
│       │           ├── agents.yaml
│       │           └── tasks.yaml
│       └── tools/
│           └── playwright_form_tool.py  # Playwright tool for CrewAI
├── tests/
│   ├── conftest.py               # shared fixtures
│   ├── unit/
│   │   ├── test_models.py
│   │   ├── test_form_tool.py
│   │   └── test_flow.py
│   ├── integration/
│   │   ├── test_jobs_api.py
│   │   ├── test_runs_api.py
│   │   └── test_db.py
│   └── e2e/
│       └── test_form_fill.py     # requires --e2e flag
└── ui/
    ├── index.html                # Landing page + history UI
    └── js/                       # ES modules (entry: main.js): API calls + DOM rendering
```

---

## 4. Database Schema

### `jobs`
Defines a reusable automation job template.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | auto-increment |
| `name` | TEXT | human label, e.g. "AI Consultant Form" |
| `job_type` | TEXT | e.g. `"google_form_fill"` |
| `payload` | TEXT | JSON config (form URL, field values) |
| `created_at` | DATETIME | UTC |

### `runs`
Records every execution of a job.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | auto-increment |
| `job_id` | INTEGER FK → jobs.id | |
| `status` | TEXT | `pending` · `running` · `success` · `failed` |
| `result` | TEXT | JSON output or error message |
| `started_at` | DATETIME | UTC |
| `finished_at` | DATETIME | UTC, nullable |

---

## 5. API Design

Base URL: `http://localhost:8000`

### Jobs

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/jobs` | List all jobs |
| `POST` | `/api/jobs` | Create a new job |
| `GET` | `/api/jobs/{id}` | Get job detail |
| `DELETE` | `/api/jobs/{id}` | Delete job |

### Runs

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/jobs/{id}/run` | Trigger a job run (async) |
| `GET` | `/api/runs` | List all runs (with pagination) |
| `GET` | `/api/runs/{id}` | Get run detail + result |

### System

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Serve `ui/index.html` |
| `GET` | `/health` | Health check |

---

## 6. CrewAI Architecture

### Flow: `FormFillFlow`

```
FormFillFlow (Pydantic state: job_id, payload, run_id)
    │
    ├─ @start()  validate_payload()
    │       └─ parse and validate form field values
    │
    └─ @listen(validate_payload)  execute_crew()
            └─ delegates to FormFillerCrew
                    └─ returns: { success: bool, screenshot_path, message }
```

### Crew: `FormFillerCrew`

**Agent — `form_agent`**
- Role: Web Form Automation Specialist
- Goal: Fill and submit the target Google Form accurately
- Tools: `PlaywrightFormTool`
- LLM: `gpt-4o-mini`

**Task — `fill_form_task`**
- Description: Navigate to the form URL, fill each field with the provided values, submit, and confirm success
- Expected output: JSON `{ "submitted": true, "confirmation_text": "..." }`

### Tool: `PlaywrightFormTool`

A CrewAI `BaseTool` wrapping Playwright. Input schema:

```python
class FormInput(BaseModel):
    url: str
    company_name: str
    company_size: Literal["0-10", "11-100", "200 up", "其他"]
    ai_problem: str
```

Internally launches a headless Chromium browser, fills each field by label/selector, submits, and returns the confirmation message text.

---

## 7. Target Form: AI Consultant Survey

URL: `https://docs.google.com/forms/d/e/1FAIpQLSc0E2-jTMy8WNFLlHc5rG4zw3U1QaCykBra3mdqFv0DNb8i9Q/viewform`

| Field | Label (zh) | Type | Options |
|---|---|---|---|
| 1 | 公司名稱 (Company Name) | Text input | — |
| 2 | 公司規模 (Company Size) | Radio | `0-10` / `11-100` / `200 up` / `其他` |
| 3 | 想用AI解決的問題 (AI Problem) | Text input | — |

Job payload example:
```json
{
  "url": "https://docs.google.com/forms/d/e/1FAIpQLSc0E2-jTMy8WNFLlHc5rG4zw3U1QaCykBra3mdqFv0DNb8i9Q/viewform",
  "company_name": "Acme Corp",
  "company_size": "11-100",
  "ai_problem": "Automate customer support ticket triage"
}
```

---

## 8. Run Status — How the Client Knows

### Problem
`POST /api/jobs/{id}/run` returns `202 Accepted` immediately. The CrewAI flow runs in a background thread/task. The client needs to track when it finishes and whether it succeeded.

### Solution: Server-Sent Events (SSE) + Polling fallback

**Primary: SSE stream per run**

```
GET /api/runs/{run_id}/stream
Content-Type: text/event-stream
```

FastAPI yields events as the run progresses:

```
data: {"status": "running", "message": "Launching browser..."}

data: {"status": "running", "message": "Filling form fields..."}

data: {"status": "success", "result": {"submitted": true, "confirmation_text": "..."}}
```

The frontend opens an `EventSource` on the run ID immediately after `POST .../run` returns. The stream closes when status is `success` or `failed`.

**Fallback: Polling**  
For browsers/proxies that don't support SSE, the UI falls back to `GET /api/runs/{run_id}` every 3 seconds until `status != "running"`.

### Backend mechanics

- `POST .../run` creates a `Run` row with `status = "pending"`, spawns `asyncio.create_task(run_flow(run_id, payload))`
- The flow function updates the DB row at each stage: `pending → running → success/failed`
- SSE endpoint reads `runs` table on a short interval and streams diffs; no message broker needed at this scale

### Status lifecycle

```
pending ──► running ──► success
                   └──► failed
```

`finished_at` is set on terminal states. `result` contains JSON on success, error string on failure.

---

## 9. TDD Approach

Tests are written **before** implementation. Each feature starts with a failing test that defines the contract.

### Test Structure

```
tests/
├── conftest.py              # shared fixtures (test DB, mock playwright, test client)
├── unit/
│   ├── test_models.py       # SQLModel table creation, field validation
│   ├── test_form_tool.py    # PlaywrightFormTool with mocked browser
│   └── test_flow.py         # FormFillFlow logic with mocked crew
├── integration/
│   ├── test_jobs_api.py     # FastAPI job CRUD endpoints
│   ├── test_runs_api.py     # run trigger, status, SSE stream
│   └── test_db.py           # SQLite read/write, FK constraints
└── e2e/
    └── test_form_fill.py    # full flow against real (or stubbed) Google Form
```

### Key Test Cases

**Unit — `test_form_tool.py`**
```python
def test_fill_form_succeeds_with_valid_input(mock_playwright):
    tool = PlaywrightFormTool()
    result = tool._run(url=FORM_URL, company_name="Acme", company_size="0-10", ai_problem="triage")
    assert result["submitted"] is True

def test_fill_form_raises_on_invalid_size(mock_playwright):
    with pytest.raises(ValidationError):
        tool._run(..., company_size="999")
```

**Integration — `test_runs_api.py`**
```python
async def test_trigger_run_returns_202(client, seed_job):
    resp = await client.post(f"/api/jobs/{seed_job.id}/run")
    assert resp.status_code == 202
    assert "run_id" in resp.json()

async def test_sse_stream_emits_terminal_event(client, seed_job, mock_flow):
    run_id = (await client.post(f"/api/jobs/{seed_job.id}/run")).json()["run_id"]
    events = await collect_sse(client, f"/api/runs/{run_id}/stream", timeout=5)
    assert events[-1]["status"] in ("success", "failed")
```

**E2E — `test_form_fill.py`** (runs only in CI with `--e2e` flag, uses real Playwright + a stub form)
```python
@pytest.mark.e2e
async def test_full_form_fill_flow():
    flow = FormFillFlow(payload={...})
    result = await flow.kickoff_async()
    assert result["submitted"] is True
```

### Tooling

| Tool | Purpose |
|---|---|
| `pytest` + `pytest-asyncio` | async test runner |
| `httpx[asyncio]` | async test client for FastAPI |
| `pytest-mock` | mock Playwright browser |
| `respx` | mock external HTTP (Google Forms) |
| `factory-boy` | DB fixture factories |

### TDD Workflow per Feature

1. Write failing test that captures the contract
2. Run `uv run pytest` — confirm red
3. Implement minimum code to pass
4. Run `uv run pytest` — confirm green
5. Refactor; tests stay green

---

## 11. Frontend UI

Single-page `index.html` with two sections:

### Landing Section
- System name + tagline
- "New Run" button that opens a modal form with three fields (company name, size, AI problem)
- On submit → `POST /api/jobs` then `POST /api/jobs/{id}/run`

### History Section
- Table of recent runs: ID · Job Name · Status badge · Started At · Duration · Result link
- Auto-refreshes every 5 seconds via `setInterval` polling `GET /api/runs`
- Click a row to expand the JSON result inline

No framework — plain `fetch()` API and DOM manipulation. Styled with a minimal CSS design (dark sidebar, clean cards).

---

## 12. Configuration

### `.env`
```
OPENAI_API_KEY=sk-...
DATABASE_URL=sqlite:///./data/auto.db
```

### `pyproject.toml` (key dependencies)
```toml
[project]
name = "agent-auto-system"
version = "0.1.0"
requires-python = ">=3.11"

dependencies = [
  "crewai[tools]>=0.80.0",
  "fastapi>=0.115.0",
  "uvicorn[standard]>=0.30.0",
  "sqlmodel>=0.0.21",
  "playwright>=1.44.0",
  "python-dotenv>=1.0.0",
  "openai>=1.30.0",
]
```

---

## 13. Run Instructions (local dev)

```bash
# 1. Install dependencies
uv sync

# 2. Install Playwright browsers
uv run playwright install chromium

# 3. Copy and fill env
cp .env.example .env

# 4. Run tests (unit + integration)
uv run pytest tests/unit tests/integration -v

# 5. Run e2e tests (real browser, needs valid OPENAI_API_KEY)
uv run pytest tests/e2e --e2e -v

# 6. Start the server
uv run uvicorn src.main:app --reload --port 8000

# 7. Open UI
open http://localhost:8000
```

---

## 14. Sequence Diagram

```
User (Browser)          FastAPI              CrewAI Flow           Playwright
     │                     │                      │                     │
     │── POST /api/jobs ──►│                      │                     │
     │◄─ 201 {job_id} ─────│                      │                     │
     │                     │                      │                     │
     │── POST /jobs/1/run ►│                      │                     │
     │◄─ 202 {run_id} ─────│                      │                     │
     │                     │── asyncio.create_task(run_flow) ──────────►│
     │                     │                      │                     │
     │── GET /runs/1/stream (SSE)                 │                     │
     │◄═ data: {status: "running"} ───────────────│                     │
     │                     │                      │── launch browser ──►│
     │                     │                      │                     │── fill fields
     │◄═ data: {status: "running", msg: "filling"}│── submit ──────────►│
     │                     │                      │◄── confirmation ────│
     │                     │── UPDATE runs (success)                    │
     │◄═ data: {status: "success", result: {...}} │                     │
     │  [SSE stream closes]                        │                     │
```

---

## 15. Future Jobs (Roadmap)

| Job Type | Description |
|---|---|
| `email_outreach` | Draft and send templated emails via Gmail API |
| `linkedin_connect` | Auto-connect with filtered LinkedIn profiles |
| `sheet_updater` | Write structured data into Google Sheets |
| `web_scraper` | Extract data from a target URL and save to DB |

Each new job type: add a row to `job_type` enum, a new Crew under `automation/crews/`, and a matching payload schema.
