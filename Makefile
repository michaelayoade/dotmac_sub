.PHONY: help test lint type-check format security check lint-file type-check-file check-file migrate dev docker-up docker-down docker-logs worker beat coverage clean

# Default target
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── Quality ──────────────────────────────────────────────

lint: ## Run ruff linter
	poetry run ruff check app/

format: ## Format code with ruff
	poetry run ruff format app/
	poetry run ruff check --fix app/

type-check: ## Run mypy type checker
	poetry run mypy app/ --ignore-missing-imports

security: ## Run bandit security scan
	poetry run bandit -r app/ -c pyproject.toml -q

check: lint type-check security ## Run all quality checks (lint + type-check + security)

lint-file: ## Lint a single file (usage: make lint-file FILE=app/services/nas.py)
	poetry run ruff check $(FILE)
	poetry run ruff format --check $(FILE)

type-check-file: ## Type-check a single file (usage: make type-check-file FILE=app/services/nas.py)
	poetry run mypy $(FILE) --ignore-missing-imports

check-file: lint-file type-check-file ## Lint + type-check a single file (usage: make check-file FILE=app/services/nas.py)

# ─── Testing ──────────────────────────────────────────────

test: ## Run test suite
	poetry run pytest tests/ -q

test-v: ## Run test suite (verbose)
	poetry run pytest tests/ -v

test-cov: ## Run tests with coverage report
	poetry run pytest tests/ --cov=app --cov-report=term-missing

test-fast: ## Run tests, stop on first failure
	poetry run pytest tests/ -x --tb=short

test-e2e: ## Run end-to-end browser tests
	poetry run pytest tests/e2e/ -v --headed

# ─── Database ─────────────────────────────────────────────

migrate: ## Apply all pending migrations
	poetry run alembic upgrade head

migrate-new: ## Create a new migration (usage: make migrate-new msg="add users table")
	poetry run alembic revision --autogenerate -m "$(msg)"

migrate-down: ## Rollback last migration
	poetry run alembic downgrade -1

migrate-history: ## Show migration history
	poetry run alembic history --verbose

# ─── Development ──────────────────────────────────────────

dev: ## Run dev server with hot reload
	python -m uvicorn app.main:app --reload --port 8000

worker: ## Run Celery worker
	celery -A app.celery_app worker --loglevel=info

beat: ## Run Celery beat scheduler
	celery -A app.celery_app beat --loglevel=info

# ─── Docker ───────────────────────────────────────────────

docker-up: ## Start all Docker containers
	docker compose up -d

docker-down: ## Stop all Docker containers
	docker compose down

docker-logs: ## Tail Docker container logs
	docker compose logs -f --tail=100

docker-rebuild: ## Rebuild and restart app container
	docker compose build app && docker compose up -d app

docker-shell: ## Open shell in app container
	docker exec -it dotmac_sub_app bash

docker-migrate: ## Run migrations inside Docker
	docker exec dotmac_sub_app alembic upgrade head

# ─── Credentials ──────────────────────────────────────────

encrypt-credentials: ## Encrypt existing NAS credentials (dry run)
	poetry run python scripts/encrypt_nas_credentials.py --dry-run

encrypt-credentials-execute: ## Encrypt existing NAS credentials (execute)
	poetry run python scripts/encrypt_nas_credentials.py --execute

generate-encryption-key: ## Generate a new credential encryption key
	@python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# ─── Pre-commit ───────────────────────────────────────────

pre-commit-install: ## Install pre-commit hooks
	poetry run pre-commit install

pre-commit-run: ## Run pre-commit on all files
	poetry run pre-commit run --all-files

# ─── Cleanup ──────────────────────────────────────────────

clean: ## Remove build artifacts and caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf htmlcov/ .coverage
