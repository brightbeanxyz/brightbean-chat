.PHONY: help setup lock frontend css-watch server worker migrate migrations test test-cov lint format typecheck audit \
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

frontend: ## Install JS deps and build the Tailwind bundle + vendored JS
	npm ci
	npm run build

css-watch: ## Rebuild the Tailwind bundle on save (leave running alongside `make server`)
	npm run watch:css

# Development

server: ## Start Django dev server
	python manage.py runserver

worker: ## Start the background task worker (the command itself lands with issue #5)
	python manage.py process_tasks

# Database

migrate: ## Run database migrations
	python manage.py migrate

migrations: ## Create new migrations
	python manage.py makemigrations

# Code quality

test: ## Run tests
	pytest

test-cov: ## Run tests with coverage
	pytest --cov=apps --cov-report=term-missing

lint: ## Run linter and format check (ruff's S rules are the security lint)
	ruff check .
	ruff format --check .

format: ## Auto-fix lint and formatting issues
	ruff check --fix .
	ruff format .

typecheck: ## Run mypy type checker
	mypy apps/ config/ tests/ --ignore-missing-imports

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
