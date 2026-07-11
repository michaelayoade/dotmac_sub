#!/usr/bin/env bash
# Deploy dotmac_sub from a registry-built (GHCR) image — no host build.
#
# This is the RECOMMENDED production path: it runs the exact image CI built and
# tested, decoupled from the box's git working tree (which has repeatedly drifted
# — feature branches, dirty trees, hand-applied migrations). `make prod-deploy`
# (host build) is kept only as an air-gapped/registry-down fallback.
#
# Usage:
#   deploy.sh sha-abc1234        deploy this image tag (CI builds one per commit on main)
#   deploy.sh --status           show pinned vs running image
#   SKIP_BACKUP=1 deploy.sh ...  skip the pre-migration DB backup (NOT recommended)
#
# Procedure:
#   verify image on GHCR -> DB backup -> pin APP_IMAGE in .env -> pull ->
#   alembic upgrade heads (one-off container) -> recreate app+workers -> health gate.
#
# On a failed health gate the previous image is re-pinned and the services are
# recreated on it. Migrations are NOT reverted automatically — new revisions must
# be backward-compatible with the previous release.
set -euo pipefail

# Deploy dir == repo root (sub deploys in place). Override with DEPLOY_DIR.
DEPLOY_DIR="${DEPLOY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO_DIR="${REPO_DIR:-${DEPLOY_DIR}}"
IMAGE_REPO="ghcr.io/michaelayoade/dotmac_sub"
APP_CONTAINER="dotmac_sub_app"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8001/health}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-180}"
IMAGE_RETAIN_COUNT="${IMAGE_RETAIN_COUNT:-5}"
# Every service that runs the app image and must be recreated on a new build.
APP_SERVICES=(app celery-worker celery-worker-bandwidth celery-worker-ingestion \
  celery-worker-billing celery-worker-tr069 celery-beat bandwidth-poller \
  syslog-listener)

log() { printf '\n==> %s\n' "$*"; }

# Deploy-integrity gate. The immutable image must not be shadowed by a host
# source bind-mount: a `/app/app` mount means a dev overlay (docker-compose.dev.yml,
# or a legacy auto-loaded docker-compose.override.yml) got layered on, so the
# RUNNING code is the host working tree — not the tag we just deployed. This is
# invisible to the health gate (host code can be perfectly healthy), so check it
# explicitly. `/app/uploads` and other named volumes are fine; only `/app/app`
# (the Python package) shadowing the image is the failure.
assert_no_source_mount() {
  local mounts
  mounts="$(docker inspect "${APP_CONTAINER}" \
    --format '{{range .Mounts}}{{println .Destination}}{{end}}' 2>/dev/null || true)"
  if grep -qx '/app/app' <<<"${mounts}"; then
    echo "DEPLOY INTEGRITY FAILURE: ${APP_CONTAINER} has a host bind-mount at /app/app —" >&2
    echo "the working tree is shadowing the image, so '${TAG:-?}' is NOT the running code." >&2
    echo "Cause: a dev overlay was loaded (stray docker-compose.override.yml, or a bare" >&2
    echo "'docker compose up/restart' on the host). Fix on the host, then redeploy:" >&2
    echo "  docker compose -f docker-compose.yml up -d --force-recreate ${APP_SERVICES[*]}" >&2
    return 1
  fi
}

cd "${DEPLOY_DIR}"
COMPOSE=(docker compose -f docker-compose.yml)

pinned_image() { grep -E '^APP_IMAGE=' .env | cut -d= -f2-; }

if [[ "${1:-}" == "--status" ]]; then
  echo "pinned:  $(pinned_image)"
  echo "running: $(docker inspect "${APP_CONTAINER}" --format '{{.Config.Image}}' 2>/dev/null || echo 'not running')"
  exit 0
fi

TAG="${1:?usage: deploy.sh <image-tag>, e.g. deploy.sh sha-abc1234 (or --status)}"
IMAGE="${IMAGE_REPO}:${TAG}"
PREV_IMAGE="$(pinned_image)"

if [[ "${IMAGE}" == "${PREV_IMAGE}" ]]; then
  log "Image ${IMAGE} is already pinned — re-running deploy steps idempotently."
fi

log "Deploying ${IMAGE} (currently pinned: ${PREV_IMAGE:-none})"

log "Verifying image exists on registry"
docker manifest inspect "${IMAGE}" >/dev/null

if [[ "${SKIP_BACKUP:-0}" != "1" ]]; then
  log "Backing up database before migrations (SKIP_BACKUP=1 to skip)"
  bash "${REPO_DIR}/scripts/db_backup.sh"
fi

repin_prev() {
  [[ -n "${PREV_IMAGE}" ]] && sed -i "s|^APP_IMAGE=.*|APP_IMAGE=${PREV_IMAGE}|" "${DEPLOY_DIR}/.env"
}
trap 'repin_prev; echo "Deploy FAILED — APP_IMAGE restored to ${PREV_IMAGE:-none} (running containers untouched)" >&2' ERR

log "Pinning APP_IMAGE=${IMAGE}"
if grep -q '^APP_IMAGE=' .env; then
  sed -i "s|^APP_IMAGE=.*|APP_IMAGE=${IMAGE}|" .env
else
  printf 'APP_IMAGE=%s\n' "${IMAGE}" >> .env
fi
# Best-effort deploy record: resolve the tag's short sha to a full commit sha.
if git -C "${REPO_DIR}" rev-parse --verify --quiet "${TAG#sha-}^{commit}" >/dev/null 2>&1; then
  FULL_SHA="$(git -C "${REPO_DIR}" rev-parse "${TAG#sha-}^{commit}")"
  if grep -q '^GIT_SHA=' .env; then
    sed -i "s|^GIT_SHA=.*|GIT_SHA=${FULL_SHA}|" .env
  else
    printf 'GIT_SHA=%s\n' "${FULL_SHA}" >> .env
  fi
fi

log "Pulling image"
"${COMPOSE[@]}" pull app

# Multi-head safe: sub has hit multi-head states (e.g. the bundles migration that
# merged heads), so use `heads` (plural), never `head`.
log "Applying migrations (alembic upgrade heads)"
"${COMPOSE[@]}" run --rm --no-deps app alembic upgrade heads

log "Recreating services: ${APP_SERVICES[*]}"
"${COMPOSE[@]}" up -d "${APP_SERVICES[@]}"

log "Verifying deploy integrity (no host source bind-mount shadowing the image)"
if ! assert_no_source_mount; then
  trap - ERR
  exit 1
fi

# The app service has no docker healthcheck, so gate on its HTTP /health endpoint.
log "Waiting for app health at ${HEALTH_URL} (timeout ${HEALTH_TIMEOUT_SECONDS}s)"
deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
healthy=0
while ((SECONDS < deadline)); do
  if curl -fsS -o /dev/null "${HEALTH_URL}" 2>/dev/null; then
    healthy=1
    break
  fi
  sleep 5
done

if ((healthy == 0)); then
  trap - ERR
  log "Health gate FAILED (${HEALTH_URL} never became healthy) — rolling back to ${PREV_IMAGE:-none}"
  if [[ -n "${PREV_IMAGE}" ]]; then
    repin_prev
    "${COMPOSE[@]}" pull app || true
    "${COMPOSE[@]}" up -d "${APP_SERVICES[@]}"
    log "Rolled back to ${PREV_IMAGE}. NOTE: migrations from ${TAG} were NOT reverted."
  else
    log "No previous image recorded — cannot auto-roll-back. Investigate the app container."
  fi
  exit 1
fi

trap - ERR
log "Deployed ${TAG} successfully (was ${PREV_IMAGE:-none})"

log "Pruning old ${IMAGE_REPO} images (keeping ${IMAGE_RETAIN_COUNT} rollback images)"
IMAGE_REPO="${IMAGE_REPO}" RETAIN_IMAGES="${IMAGE_RETAIN_COUNT}" \
  bash "${REPO_DIR}/scripts/docker_image_retention.sh" || \
  log "Image retention failed; deploy is healthy, but old image cleanup needs attention"
