#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required but was not found on PATH" >&2
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "Docker Compose v2 is required but 'docker compose' is unavailable" >&2
    exit 1
fi

if [[ ! -f .env ]]; then
    echo ".env is required; copy .env.example or use the generated deployment .env" >&2
    exit 1
fi

if grep -Eq 'REPLACE_WITH|change-me|project-dsn' .env; then
    echo ".env still contains placeholder values; replace shared Redis, S3, OpenBao, and GlitchTip secrets first" >&2
    exit 1
fi

chmod 600 .env

docker compose config >/dev/null
docker compose up -d --build
docker compose ps
