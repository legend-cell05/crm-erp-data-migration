# =============================================================================
# Shortcuts. Every target is a command you could type yourself -- the Makefile
# is a reminder, not a layer.
# =============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help install env lint format typecheck test test-integration coverage check \
        seed init-db serve dry-run gate load migrate reconcile rejects crosswalk runs \
        doctor up down clean-volumes logs psql docker-build clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Development -------------------------------------------------------------

install: ## Install the package and its development dependencies
	python -m pip install --upgrade pip
	pip install -e ".[dev]"

env: ## Create .env from the example if it does not exist
	@test -f .env || (cp .env.example .env && echo "created .env -- edit it before running")

lint: ## Run ruff (lint + format check)
	python -m ruff check .
	python -m ruff format --check .

format: ## Apply ruff formatting and safe fixes
	python -m ruff check --fix .
	python -m ruff format .

typecheck: ## Run mypy on src/
	python -m mypy

test: ## Run the unit tests (no database needed)
	python -m pytest tests/unit -v

test-integration: ## Run the full suite (requires a live PostgreSQL)
	KEYSTONE_RUN_INTEGRATION=1 python -m pytest tests -v

coverage: ## Full suite with a coverage report
	KEYSTONE_RUN_INTEGRATION=1 python -m pytest tests --cov --cov-report=term-missing

check: lint typecheck test ## Everything CI runs, minus the database

# --- The migration -----------------------------------------------------------

seed: ## Generate the simulated legacy CRM and its exports
	keystone seed

init-db: ## Create the schemas and tables
	keystone init-db

serve: ## Start the simulated Atlas Cloud target
	keystone serve --host 0.0.0.0 --port 8080

dry-run: ## Plan the migration and write the impact report
	keystone dry-run

gate: ## Show whether a load would be allowed
	keystone gate

load: ## Migrate into the target
	keystone load

migrate: ## Dry-run, load and reconcile
	keystone migrate

reconcile: ## Compare source, crosswalk and target
	keystone reconcile

rejects: ## Show what did not migrate, and why
	keystone rejects --show-examples

crosswalk: ## Show what became what
	keystone crosswalk

runs: ## Show the run history
	keystone runs

doctor: ## Check configuration and connectivity
	keystone doctor

# --- Docker ------------------------------------------------------------------

up: env ## Start the full stack and run the migration
	docker compose up --build

down: ## Stop the stack (keeps the database volume)
	docker compose down

clean-volumes: ## Stop the stack AND delete the database volume
	docker compose down -v

logs: ## Follow the container logs
	docker compose logs -f

psql: ## Open a psql shell on the compose database
	docker compose exec postgres psql -U $${KEYSTONE_DB_USER:-keystone_app} -d $${KEYSTONE_DB_NAME:-keystone}

docker-build: ## Build the application image only
	docker build -t crm-erp-data-migration:local .

# --- Housekeeping ------------------------------------------------------------

clean: ## Remove caches and generated data (keeps .env)
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml junit-*.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -f data/legacy_exports/*.csv data/reports/*.json data/reports/*.md
