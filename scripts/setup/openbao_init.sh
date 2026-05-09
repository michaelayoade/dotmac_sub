#!/usr/bin/env bash
# OpenBao secrets initialization for dotmac_sub.
# Seeds project secrets into KV v2 at secret/<path> using real environment
# values only. The script never falls back to baked-in secrets.
#
# Usage:
#   ./scripts/openbao_init.sh
#   ./scripts/openbao_init.sh --check
#   ./scripts/openbao_init.sh --strict
#   BAO_ADDR=http://openbao:8200 ./scripts/openbao_init.sh
#
# Modes:
#   default  Write any secret groups whose required env vars are present.
#   --check  Report what would be written/skipped without changing OpenBao.
#   --strict Fail if any required env var for any group is missing.

set -euo pipefail

CONTAINER="dotmac_sub_openbao"
BAO_ADDR="${BAO_ADDR:-http://127.0.0.1:8200}"
BAO_TOKEN="${BAO_TOKEN:-dotmac-sub-dev-token}"
CHECK_ONLY=0
STRICT=0

for arg in "$@"; do
    case "$arg" in
        --check)
            CHECK_ONLY=1
            ;;
        --strict)
            STRICT=1
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

run_bao() {
    docker exec \
        -e BAO_ADDR="$BAO_ADDR" \
        -e BAO_TOKEN="$BAO_TOKEN" \
        "$CONTAINER" bao "$@"
}

require_vars() {
    local missing=0
    for name in "$@"; do
        if [ -z "${!name:-}" ]; then
            echo "  [MISSING] $name"
            missing=1
        fi
    done
    return $missing
}

put_secret() {
    local path="$1"
    shift
    if [ "$CHECK_ONLY" -eq 1 ]; then
        echo "  [CHECK] secret/${path}"
        return 0
    fi
    run_bao kv put "secret/${path}" "$@" >/dev/null
    echo "  [OK] secret/${path}"
}

seed_group() {
    local path="$1"
    shift
    local required_csv="$1"
    shift
    local required=()
    local missing=0
    IFS=',' read -r -a required <<<"$required_csv"

    echo "==> secret/${path}"
    if ! require_vars "${required[@]}"; then
        missing=1
    fi

    if [ "$missing" -eq 1 ]; then
        if [ "$STRICT" -eq 1 ]; then
            echo "  [FAIL] secret/${path} skipped because required env vars are missing" >&2
            return 1
        fi
        echo "  [SKIP] secret/${path} skipped because required env vars are missing"
        return 0
    fi

    put_secret "$path" "$@"
}

echo "==> Waiting for OpenBao to be ready..."
for i in $(seq 1 30); do
    if run_bao status -format=json 2>/dev/null | grep -q '"sealed":false'; then
        break
    fi
    sleep 1
done

echo "==> Seeding OpenBao KV v2 (real env values only)..."

seed_group auth \
    "JWT_SECRET,CREDENTIAL_ENCRYPTION_KEY" \
    "jwt_secret=${JWT_SECRET:-}" \
    "credential_encryption_key=${CREDENTIAL_ENCRYPTION_KEY:-}" \
    "totp_encryption_key=${TOTP_ENCRYPTION_KEY:-}" \
    "wireguard_key_encryption_key=${WIREGUARD_KEY_ENCRYPTION_KEY:-}"

seed_group database \
    "DATABASE_URL,POSTGRES_PASSWORD" \
    "url=${DATABASE_URL:-}" \
    "password=${POSTGRES_PASSWORD:-}"

seed_group redis \
    "REDIS_PASSWORD,REDIS_URL,CELERY_BROKER_URL,CELERY_RESULT_BACKEND" \
    "password=${REDIS_PASSWORD:-}" \
    "url=${REDIS_URL:-}" \
    "broker_url=${CELERY_BROKER_URL:-}" \
    "result_backend=${CELERY_RESULT_BACKEND:-}"

seed_group paystack \
    "PAYSTACK_SECRET_KEY,PAYSTACK_PUBLIC_KEY" \
    "secret_key=${PAYSTACK_SECRET_KEY:-}" \
    "public_key=${PAYSTACK_PUBLIC_KEY:-}"

seed_group radius \
    "RADIUS_DB_PASS" \
    "db_password=${RADIUS_DB_PASS:-}" \
    "db_dsn=${RADIUS_DB_DSN:-}"

seed_group genieacs \
    "GENIEACS_MONGODB_PASSWORD,GENIEACS_UI_JWT_SECRET,GENIEACS_CWMP_USER,GENIEACS_CWMP_PASS" \
    "mongodb_dsn=${GENIEACS_MONGODB_DSN:-}" \
    "mongodb_password=${GENIEACS_MONGODB_PASSWORD:-}" \
    "jwt_secret=${GENIEACS_UI_JWT_SECRET:-}" \
    "cwmp_user=${GENIEACS_CWMP_USER:-}" \
    "cwmp_pass=${GENIEACS_CWMP_PASS:-}"

seed_group s3 \
    "S3_ACCESS_KEY,S3_SECRET_KEY" \
    "access_key=${S3_ACCESS_KEY:-}" \
    "secret_key=${S3_SECRET_KEY:-}"

seed_group migration \
    "SPLYNX_MYSQL_PASS" \
    "smartolt_api_key=${SMARTOLT_API_KEY:-}" \
    "splynx_mysql_pass=${SPLYNX_MYSQL_PASS:-}"

seed_group notifications \
    "SMTP_PORT" \
    "smtp_host=${SMTP_HOST:-}" \
    "smtp_port=${SMTP_PORT:-}" \
    "smtp_username=${SMTP_USERNAME:-}" \
    "smtp_password=${SMTP_PASSWORD:-}" \
    "sms_api_key=${SMS_API_KEY:-}" \
    "sms_api_secret=${SMS_API_SECRET:-}"

echo ""
echo "==> Available OpenBao paths:"
run_bao kv list secret/ 2>/dev/null || echo "(listing unavailable)"
