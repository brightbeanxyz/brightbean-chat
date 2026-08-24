.PHONY: help setup lock frontend schema css-watch js-watch server worker migrate migrations test test-cov lint format typecheck audit \
        docker-up docker-down docker-build docker-logs \
        prod-secrets prod-up prod-down prod-logs prod-migrate smoke

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
# Node at all still serves the shell's JavaScript. Two things are built: the
# Tailwind bundle and the flow-builder island (issue #10), both gitignored
# artefacts. Without them the app runs, unstyled and with the builder page
# showing its "not built" notice.
#
# static/flows/flow-schema.json is generated but committed, because the flow
# builder's bundle imports it at build time and a clone should not need a Python
# environment to produce it. `make schema` regenerates it and a test asserts the
# committed copy matches the registry, so it cannot silently go stale.
#
# `schema` therefore runs BEFORE `npm run build`, not after: `build:js` inlines
# the artefact, so the old order built the island from a stale copy of a
# registry change made in the same commit — with nothing red to say so.

frontend: ## Install JS deps, regenerate the flow schema, build every bundle
	npm ci
	$(MAKE) schema
	npm run build

js-watch: ## Rebuild the flow-builder bundle on save (leave running alongside `make server`)
	npm run watch:js

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

# Production (issue #28)
#
# The reference stack is docker-compose.prod.yml; docs/self-hosting.md is the
# walkthrough. These targets exist so the commands in that guide are one name
# each rather than a flag-laden line to retype.

# python3, not python: docs/self-hosting.md asks for Docker and a domain and
# nothing else, and a host that meets exactly those prerequisites usually has
# no `python` on PATH at all. This is the first command that guide runs, so it
# failing with "command not found" is the worst possible first impression.
prod-secrets: ## Generate the required secrets for a production .env
	@python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(50)); print('ENCRYPTION_KEY_SALT=' + secrets.token_urlsafe(50)); print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24)); print('TICK_TOKEN=' + secrets.token_urlsafe(32))"
	@echo ""
	@echo "Paste these into .env (start from deploy/env.prod.example)."
	@echo "SECRET_KEY and ENCRYPTION_KEY_SALT decrypt your stored platform"
	@echo "credentials: back them up somewhere other than the database."

prod-up: ## Start the production stack (reads .env — see deploy/env.prod.example)
	docker compose -f docker-compose.prod.yml up -d

prod-down: ## Stop the production stack (volumes are kept)
	docker compose -f docker-compose.prod.yml down

prod-logs: ## Tail logs from the production stack
	docker compose -f docker-compose.prod.yml logs -f

prod-migrate: ## Run migrations against the production stack (the upgrade step)
	docker compose -f docker-compose.prod.yml run --rm migrate

smoke: ## Check a deployment is healthy and hardened (make smoke URL=https://chat.example.com)
	@test -n "$(URL)" || { echo "usage: make smoke URL=https://chat.example.com [ARGS='--insecure']"; exit 2; }
	scripts/smoke.sh $(URL) $(ARGS)
