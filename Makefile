# ---------------------------------------------------------------------------
# DataFlow Mini ETL - developer shortcuts
# ---------------------------------------------------------------------------
PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: help venv install run run-fixture docker-up docker-etl dashboard test lint format clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	$(PY) -m venv $(VENV)

install: ## Install dev dependencies into the venv
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements-dev.txt

run: ## Live run (PostgreSQL when DATABASE_URL is set, else SQLite)
	$(BIN)/python -m etl run

run-fixture: ## Offline replay of committed fixtures (no network needed)
	$(BIN)/python -m etl run --source fixture --backend sqlite

docker-up: ## Start PostgreSQL + pipeline via Docker Compose
	docker compose up --build

docker-etl: ## Re-run the pipeline container only
	docker compose run --rm etl

dashboard: ## Serve the GitHub Pages dashboard locally
	$(BIN)/python -m http.server 8080 --directory docs

test: ## Run the test suite
	$(BIN)/python -m pytest -q

lint: ## Lint with ruff
	$(BIN)/ruff check etl tests

format: ## Auto-format with ruff
	$(BIN)/ruff check --fix etl tests

clean: ## Remove generated outputs and caches
	rm -rf artifacts data .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
