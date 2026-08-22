.PHONY: help setup lock frontend schema css-watch server worker migrate migrations test test-cov lint format typecheck audit \
        docker-up docker-down docker-build docker-logs

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# Setup

setup: ## Initial project setup (copy env, install deps, build CSS, migrate)
	@test -f .env || cp .env.example .env
	pip install --require-hashes -r requirements-dev.txt
	$(MAKE) frontend
	python manage.py migrate
	@echo ""
	@echo "Setup complete. Run 'python manage.py createsuperuser' to create an admin account."
	@echo "Then run 'make server' to start the app."

lock: ## Recompile requirements*.txt from requirements*.in (run after editing either)
	pip-compile --generate-hashes --strip-extras --allow-unsafe \
		--output-file requirements.txt requirements.in
	pip-compile --generate-hashes --strip-extras --allow-unsafe \
		--output-file requirements-dev.txt requirements-dev.in

# Frontend
#
# The vendored libraries in static/js/vendor/ are committed, so a clone with no
# Node at all still serves working JavaScript. Only the Tailwind bundle needs
# building: it is a gitignored artefact.
#
# static/flows/flow-schema.json is generated but committed, because the flow
# builder's bundle imports it at build time and a clone should not need a Python
# environment to produce it. `make schema` regenerates it and a test asserts the
# committed copy matches the registry, so it cannot silently go stale.

frontend: ## Install JS deps and build the Tailwind bundle + vendored JS + flow schema
	npm ci
	npm run build
	$(MAKE) schema

schema: ## Regenerate static/flows/flow-schema.json from the node registry (issue #6)
	python manage.py export_flow_schema

css-watch: ## Rebuild the Tailwind bundle on save (leave running alongside `make server`)
	npm run watch:css

# Development

server: ## Start Django dev server
	python manage.py runserver

worker: ## Start the background task worker
	python manage.py process_tasks

# Database

migrate: ## Run database migrations
	python manage.py migrate

migrations: ## Create new migrations
	python manage.py makemigrations

# Code quality

# -n auto is opted into here rather than in pyproject's addopts, so that a bare
# `pytest some/one_test.py` stays single-process. Under --dist loadfile a
# one-file run lands entirely on one worker anyway, so parallelism there buys
# nothing and costs a test-database build per idle worker. Drop the -n to debug
# a suspected cross-worker race.
test: ## Run tests (parallel; drop -n to debug a cross-worker race)
	pytest -n auto

test-cov: ## Run tests with coverage
# COVERAGE_CORE=sysmon: coverage on CPython 3.12's sys.monitoring rather than the
# trace callback. Same numbers, and it cuts the instrumentation cost of the full
# suite from ~81% to ~10%. Matches the CI invocation.
	COVERAGE_CORE=sysmon pytest -n auto --cov=apps --cov-report=term-missing

lint: ## Run linter and format check (ruff's S rules are the security lint)
	ruff check .
	ruff format --check .

format: ## Auto-fix lint and formatting issues
	ruff check --fix .
	ruff format .

typecheck: ## Run mypy type checker
	mypy apps/ config/ theme/ tests/ --ignore-missing-imports

audit: ## Run the dependency audits CI runs (SECURITY-BASELINE §10)
	pip-audit --strict --requirement requirements.txt --requirement requirements-dev.txt
	npm audit --audit-level=low
	@echo "Checking the audit gate itself rejects a known-vulnerable pin..."
	@if pip-audit --no-deps --requirement tests/fixtures/vulnerable-requirements.txt; then \
		echo "FAIL: the vulnerable fixture was reported clean — the audit gate is not working."; \
		exit 1; \
	fi
	@echo "OK: pip-audit rejected the vulnerable fixture."

# Docker

docker-up: ## Start all Docker services
	docker compose up -d

docker-down: ## Stop all Docker services
	docker compose down

docker-build: ## Rebuild Docker images
	docker compose build

docker-logs: ## Tail logs from all Docker services
	docker compose logs -f
