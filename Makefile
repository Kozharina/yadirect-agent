# yadirect-agent — developer workflow
#
# Contract: every target either succeeds with exit 0 or prints a red diagnostic
# and exits non-zero. Never "succeed" with ignored errors.
#
# `make check` is what CI runs. If it's green locally, CI should be green.

.PHONY: help install install-hooks test test-cov lint fix type check run-cli run-mcp clean

SHELL := /bin/bash
PYTHON ?= python3.11
VENV ?= .venv
BIN := $(VENV)/bin

help:  ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install:  ## Create venv (if missing) and install the package with dev extras
	@test -d $(VENV) || uv venv --python 3.11 $(VENV) || $(PYTHON) -m venv $(VENV)
	@$(BIN)/python -m pip install --upgrade pip >/dev/null
	@if command -v uv >/dev/null 2>&1; then \
	  uv pip install --python $(BIN)/python -e ".[dev]"; \
	else \
	  $(BIN)/pip install -e ".[dev]"; \
	fi

install-hooks:  ## Install pre-commit git hooks
	@$(BIN)/pre-commit install

test:  ## Run the test suite
	@$(BIN)/pytest -q

test-cov:  ## Run tests with coverage report
	@$(BIN)/pytest --cov=src/yadirect_agent --cov-report=term-missing --cov-report=html

lint:  ## Ruff check + format check (no writes)
	@$(BIN)/ruff check .
	@$(BIN)/ruff format --check .

fix:  ## Apply ruff autofixes and formatting
	@$(BIN)/ruff check --fix .
	@$(BIN)/ruff format .

type:  ## mypy strict on src/
	@$(BIN)/mypy src/

check: lint type test  ## Everything CI runs: lint + type + test

run-cli:  ## Run the agent CLI.  Usage:  make run-cli ARGS="run 'list campaigns'"
	@$(BIN)/yadirect-agent $(ARGS)

run-mcp:  ## Start the MCP stdio server
	@$(BIN)/yadirect-mcp

clean:  ## Remove build artefacts and caches
	@rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
