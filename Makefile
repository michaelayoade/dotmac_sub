.PHONY: help test lint type-check format security check lint-file type-check-file check-file migrate dev docker-up docker-down docker-logs worker beat coverage clean prod-build prod-pin prod-deploy prod-up prod-down prod-logs prod-restart prod-migrate prod-check bump-version prod-ghcr-pin prod-ghcr-deploy deploy

# Production runs IMMUTABLE images: the base docker-compose.yml has no source
# bind-mounts and pulls code only from the baked image (built by `prod-build`).
# `-f docker-compose.yml` deliberately EXCLUDES docker-compose.override.yml (the
# dev-only overlay that re-adds build:/bind-mounts), so prod never runs from this
# working tree. Plain `docker compose` (dev) auto-loads the override.
PROD_COMPOSE = docker compose -f docker-compose.yml
# Image tag baked/run by the prod stack. Override per-deploy, e.g.
#   make prod-build APP_IMAGE=dotmac_sub:$(git rev-parse --short HEAD)
APP_IMAGE ?= dotmac_sub:latest

# Default target
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── Quality ──────────────────────────────────────────────

lint: lint-imports ## Run ruff linter + import boundary checks
	poetry run ruff check app/

lint-imports: ## Check import boundaries (import-linter contracts)
	poetry run lint-imports

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

# ─── Release ──────────────────────────────────────────────

bump-version: ## Bump app version (usage: make bump-version BUMP=patch or VERSION=1.2.3)
	@if [ -n "$(VERSION)" ]; then \
		python3 scripts/bump_version.py --set "$(VERSION)"; \
	else \
		python3 scripts/bump_version.py "$(BUMP)"; \
	fi

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
	docker exec dotmac_sub_app alembic upgrade heads

prod-build: ## Build + tag the immutable prod image from a CLEAN checkout of HEAD (working-tree edits are NOT baked)
	@set -eu; \
	if [ -n "$$(git status --porcelain)" ]; then \
		echo "WARNING: working tree has uncommitted changes — building committed HEAD only; they will NOT be in the image."; \
	fi; \
	sha=$$(git rev-parse --short HEAD); \
	wt=$$(mktemp -d "$${TMPDIR:-/tmp}/dotmac-prod-build.XXXXXX"); \
	trap 'git worktree remove --force "$$wt" >/dev/null 2>&1 || rm -rf "$$wt"' EXIT INT TERM; \
	git worktree add --detach --quiet "$$wt" HEAD; \
	echo "Building $(APP_IMAGE) (+ dotmac_sub:latest, dotmac_sub:$$sha) from clean HEAD $$sha"; \
	docker build -t $(APP_IMAGE) -t dotmac_sub:latest -t "dotmac_sub:$$sha" "$$wt"

prod-deploy: ## Full deploy: build image, pin it in .env, migrate, recreate app+workers
	$(MAKE) prod-build
	$(MAKE) prod-pin
	$(MAKE) prod-migrate
	$(MAKE) prod-restart

prod-pin: ## Point .env APP_IMAGE at the freshly-built HEAD image (compose's source of truth)
	@sha=$$(git rev-parse --short HEAD); \
	img="dotmac_sub:$$sha"; \
	if grep -q '^APP_IMAGE=' .env 2>/dev/null; then \
		sed -i.bak "s#^APP_IMAGE=.*#APP_IMAGE=$$img#" .env && rm -f .env.bak; \
	else \
		printf 'APP_IMAGE=%s\n' "$$img" >> .env; \
	fi; \
	echo "Pinned APP_IMAGE=$$img in .env (compose now runs this image)"

prod-up: ## Start the production (immutable-image) Docker stack
	$(PROD_COMPOSE) up -d

prod-down: ## Stop the production Docker stack
	$(PROD_COMPOSE) down

prod-logs: ## Tail production Docker logs
	$(PROD_COMPOSE) logs -f --tail=100

prod-restart: ## Recreate prod app + worker services from the current image (APP_IMAGE)
	$(PROD_COMPOSE) up -d app celery-worker celery-worker-bandwidth celery-worker-billing celery-worker-tr069 celery-beat bandwidth-poller syslog-listener

prod-migrate: ## Apply DB migrations in the prod stack (alembic baked into the image)
	$(PROD_COMPOSE) run --rm app alembic upgrade heads

# ─── GHCR deploy (RECOMMENDED) ─────────────────────────────────────────────
# Pull the exact CI-built, CI-tested image instead of building on the host —
# decoupled from the box's git tree (which drifts). `make prod-deploy` above is
# a host-build fallback for air-gapped / registry-down situations only.
#
# `make deploy TAG=sha-<shortsha>` runs the hardened scripts/deploy.sh:
#   verify image on GHCR -> DB backup -> pin APP_IMAGE -> pull ->
#   alembic upgrade heads -> recreate app+workers -> health gate -> auto-rollback.
# CI (.github/workflows/ghcr.yml) pushes ghcr.io/<owner>/dotmac_sub per main push;
# the host must `docker login ghcr.io` (PAT with read:packages) once.
deploy: ## Hardened GHCR deploy. Usage: make deploy TAG=sha-abc1234
	@test -n "$(TAG)" || { echo "usage: make deploy TAG=sha-<shortsha> (see: scripts/deploy.sh --status)"; exit 1; }
	bash scripts/deploy.sh "$(TAG)"

GHCR_IMAGE ?= ghcr.io/michaelayoade/dotmac_sub
GHCR_TAG ?= latest

prod-ghcr-pin: ## Point .env APP_IMAGE at the GHCR image (GHCR_IMAGE:GHCR_TAG)
	@img="$(GHCR_IMAGE):$(GHCR_TAG)"; \
	if grep -q '^APP_IMAGE=' .env 2>/dev/null; then \
		sed -i.bak "s#^APP_IMAGE=.*#APP_IMAGE=$$img#" .env && rm -f .env.bak; \
	else \
		printf 'APP_IMAGE=%s\n' "$$img" >> .env; \
	fi; \
	echo "Pinned APP_IMAGE=$$img in .env (compose now runs the CI-built image)"

prod-ghcr-deploy: ## Deploy from the CI-built GHCR image (pull + migrate + restart; no host build)
	$(MAKE) prod-ghcr-pin
	$(PROD_COMPOSE) pull app
	$(MAKE) prod-migrate
	$(MAKE) prod-restart

prod-check: ## Run deployment reconciliation checks in the production stack
	$(PROD_COMPOSE) run --rm app python scripts/setup/deploy_reconcile.py

# ─── Credentials ──────────────────────────────────────────

encrypt-credentials: ## Encrypt existing NAS credentials (dry run)
	poetry run python scripts/encrypt_nas_credentials.py --dry-run

encrypt-credentials-execute: ## Encrypt existing NAS credentials (execute)
	poetry run python scripts/encrypt_nas_credentials.py --execute

generate-encryption-key: ## Generate a new credential encryption key
	@python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# ─── GenieACS Setup ──────────────────────────────────────

setup-genieacs: ## Deploy provisions, virtual params, presets to GenieACS
	poetry run python scripts/setup_genieacs.py

setup-genieacs-dry-run: ## Preview GenieACS setup without making changes
	poetry run python scripts/setup_genieacs.py --dry-run

setup-genieacs-list: ## List current GenieACS provisions and presets
	poetry run python scripts/setup_genieacs.py --list

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
