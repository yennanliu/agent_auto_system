# ────────────────────────────────────────────────────────────────────────────
# Stage 1 – dependency installer
#   Uses uv for fast, reproducible installs from the lock file.
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Pull the uv binary from its official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Copy only the files that affect dependency resolution first so that this
# layer is rebuilt only when dependencies actually change.
COPY pyproject.toml uv.lock .python-version ./

# Install production dependencies into a virtual environment inside the image.
# --frozen  → fail if lock file is out of sync with pyproject.toml
# --no-dev  → omit pytest, httpx, etc.
RUN uv sync --frozen --no-dev


# ────────────────────────────────────────────────────────────────────────────
# Stage 2 – runtime image
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DATABASE_URL=sqlite:///./data/auto.db \
    # Run the venv's binaries (uvicorn, python) directly — no uv at runtime
    PATH="/app/.venv/bin:$PATH" \
    # Suppress interactive apt prompts
    DEBIAN_FRONTEND=noninteractive

# Copy only the installed virtual environment from the builder stage
COPY --from=builder /app/.venv /app/.venv

# WeasyPrint renders the 利潤健檢 PDF in pure Python — it just needs Pango/Cairo
# at runtime (pulled in by libpango) plus Noto CJK fonts for the Chinese report.
# No headless browser, so the image stays ~1 GB lighter than a Chromium build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Copy application source (changes most often → last layer)
COPY src/ ./src/
COPY ui/  ./ui/

# Persistent runtime directories; mount named volumes here in prod:
#   data/    → SQLite database
#   uploads/ → user-uploaded files (e.g. profit_health_check CSVs)
#   reports/ → generated PDF reports
RUN mkdir -p data uploads reports

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=4 \
    CMD python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
    || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]


# ────────────────────────────────────────────────────────────────────────────
# Stage 3 – test image  (CI only, never deployed)
#   Inherits the runtime image — same OS libs (incl. WeasyPrint's Pango/CJK
#   fonts) and the same venv — then layers on the dev dependency group and the
#   test suite. Lets CI run pytest *inside* the deployed environment.
#       docker build --target test -t agent-auto-system:test .
#       docker run --rm agent-auto-system:test            # runs the suite
# ────────────────────────────────────────────────────────────────────────────
FROM runtime AS test

# uv + lock files are needed to add the dev group (pytest, httpx, …) to the venv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen

# tests/ is excluded from the runtime image (.dockerignore) — bring it in here,
# along with the sample CSV fixtures the profit_calc tests read.
COPY tests/ ./tests/
COPY shopee/sample_data/ ./shopee/sample_data/
# Golden eval cases — read by tests/unit/test_evalset.py (offline eval tier).
COPY evals/ ./evals/

# Default command: run the suite, skipping e2e (needs a real browser + API keys)
CMD ["pytest", "tests/unit", "tests/integration", "-v", "--tb=short", "-m", "not e2e"]
