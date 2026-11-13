# Thin aliases for the commands documented in CLAUDE.md, plus the automation scaffold.

.PHONY: help install lint test coverage new-automation

help:
	@echo "make install                   uv sync"
	@echo "make lint                      ruff check src/ tests/ scripts/"
	@echo "make test                      unit + integration tests (no e2e)"
	@echo "make coverage                  same tests, with the CI coverage gate (pyproject.toml fail_under)"
	@echo "make new-automation NAME=foo   scaffold spec + flow + crew + tests for a new job type"

install:
	uv sync

lint:
	uv run ruff check src/ tests/ scripts/

test:
	uv run pytest tests/unit tests/integration -v -m "not e2e"

# Mirrors the CI steps: measure src/ over unit+integration, then apply the gate.
coverage:
	uv run pytest tests/unit tests/integration -q -m "not e2e" --cov=src --cov-report= --cov-fail-under=0
	uv run coverage report --skip-covered

new-automation:
	@test -n "$(NAME)" || { echo "usage: make new-automation NAME=my_thing [TITLE='My Thing']"; exit 2; }
	uv run python scripts/new_automation.py "$(NAME)" $(if $(TITLE),--title "$(TITLE)")
