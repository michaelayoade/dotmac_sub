#!/usr/bin/env bash
# OpenBao secrets initialization for dotmac_sub.
# Seeds all project secrets into KV v2 at secret/<path>.
#
# Usage:
#   ./scripts/openbao_init.sh              # uses .env values
#   BAO_ADDR=http://openbao:8200 ./scripts/openbao_init.sh  # override address
#
# Requires: docker exec access to dotmac_sub_openbao container
#           OR bao CLI installed locally with BAO_ADDR + BAO_TOKEN set.

set -euo pipefail

CONTAINER="dotmac_sub_openbao"
BAO_ADDR="${BAO_ADDR:-http://127.0.0.1:8200}"
BAO_TOKEN="${BAO_TOKEN:-dotmac-sub-dev-token}"

run_bao() {
    docker exec \
        -e BAO_ADDR=http://127.0.0.1:8200 \
        -e BAO_TOKEN="$BAO_TOKEN" \
        "$CONTAINER" bao "$@"
}

echo "==> Waiting for OpenBao to be ready..."
for i in $(seq 1 30); do
    if run_bao status -format=json 2>/dev/null | grep -q '"sealed":false'; then
        break
    fi
    sleep 1
done

echo "==> Seeding secrets into OpenBao KV v2 (secret/)..."

# ─── Auth & Encryption Keys ─────────────────────────────────────────
run_bao kv put secret/auth \
    jwt_secret="${JWT_SECRET:-r1VZDcEfRaQfT7qBnOvhpiCM6IkjdL9oT1XzbkzVxcw=}" \
    totp_encryption_key="${TOTP_ENCRYPTION_KEY:-LRjCAO0ew_mAJLzHGBfDsUQkuoVna7xQu2nLpC05G10=}" \
    credential_encryption_key="${CREDENTIAL_ENCRYPTION_KEY:-m6c5_ZDKKOpTcEihbuPuHJuvoJ-6EJSsighX872RJbE=}" \
    wireguard_key_encryption_key="${WIREGUARD_KEY_ENCRYPTION_KEY:-n9EgNfu2ejUTa8P7oJi5zFwdFMvgGGtWXsULJg2dSJQ=}"
echo "  [OK] secret/auth"

# ─── Database ────────────────────────────────────────────────────────
run_bao kv put secret/database \
    url="${DATABASE_URL:-postgresql+psycopg://postgres:Bes3SpEVyg61QebbXSpUse7_4vbOVOa_@localhost:5434/dotmac_sub}" \
    password="${POSTGRES_PASSWORD:-Bes3SpEVyg61QebbXSpUse7_4vbOVOa_}"
echo "  [OK] secret/database"

# ─── Redis ───────────────────────────────────────────────────────────
run_bao kv put secret/redis \
    password="${REDIS_PASSWORD:-nP9DMP9A_XxHIhTT7AVH3LEJGzklQ-Rq}" \
    url="${REDIS_URL:-redis://:nP9DMP9A_XxHIhTT7AVH3LEJGzklQ-Rq@localhost:6379/0}" \
    broker_url="${CELERY_BROKER_URL:-redis://:nP9DMP9A_XxHIhTT7AVH3LEJGzklQ-Rq@localhost:6379/0}" \
    result_backend="${CELERY_RESULT_BACKEND:-redis://:nP9DMP9A_XxHIhTT7AVH3LEJGzklQ-Rq@localhost:6379/1}"
echo "  [OK] secret/redis"

# ─── Paystack ────────────────────────────────────────────────────────
run_bao kv put secret/paystack \
    secret_key="${PAYSTACK_SECRET_KEY:-sk_test_d800a38448aebcccd4642034c62515c8ad376558}" \
    public_key="${PAYSTACK_PUBLIC_KEY:-pk_test_08238794d560ec2f0f739d12589f1e10db969973}"
echo "  [OK] secret/paystack"

# ─── RADIUS ──────────────────────────────────────────────────────────
run_bao kv put secret/radius \
    db_password="${RADIUS_DB_PASS:-l2f3clS-Ws9WgTXcsW3HoznBnEq3n7N-}" \
    db_dsn="${RADIUS_DB_DSN:-postgresql://radius_readonly:radius_ro_2026@localhost:5437/radius}"
echo "  [OK] secret/radius"

# ─── GenieACS ────────────────────────────────────────────────────────
run_bao kv put secret/genieacs \
    mongodb_dsn="${GENIEACS_MONGODB_DSN:-mongodb://genieacs_readonly:genieacs_ro_2026@localhost:27017/genieacs?authSource=admin}" \
    mongodb_password="${GENIEACS_MONGODB_PASSWORD:-Rfys2820k6c0Mkq3xw5CTjVzhn6WeAjm}" \
    jwt_secret="${GENIEACS_UI_JWT_SECRET:-l5z4WECFJ_pJ0RLg2hUx1HRo8X3ifQs7xZlGnNlHhng}" \
    cwmp_user="${GENIEACS_CWMP_USER:-acs_dotmac}" \
    cwmp_pass="${GENIEACS_CWMP_PASS:-4TUHM0AssAtu8elrW6NzwwYRYAwVf0jO}"
echo "  [OK] secret/genieacs"

# ─── S3 / MinIO ─────────────────────────────────────────────────────
run_bao kv put secret/s3 \
    access_key="${S3_ACCESS_KEY:-dotmac_900808e8519d5e6a}" \
    secret_key="${S3_SECRET_KEY:-B13hKiqAKhLwcITwXpchvKCEkq-eJg2W6cH6qyE4s2k}"
echo "  [OK] secret/s3"

# ─── Migration (SmartOLT + Splynx) ──────────────────────────────────
run_bao kv put secret/migration \
    smartolt_api_key="${SMARTOLT_API_KEY:-f1fad2bc9506403cb1f2d09496f9a8de}" \
    splynx_mysql_pass="${SPLYNX_MYSQL_PASS:-MigDotmac2026!}"
echo "  [OK] secret/migration"

# ─── SMTP / Notifications ───────────────────────────────────────────
run_bao kv put secret/notifications \
    smtp_host="${SMTP_HOST:-}" \
    smtp_port="${SMTP_PORT:-587}" \
    smtp_username="${SMTP_USERNAME:-}" \
    smtp_password="${SMTP_PASSWORD:-}" \
    sms_api_key="${SMS_API_KEY:-}" \
    sms_api_secret="${SMS_API_SECRET:-}"
echo "  [OK] secret/notifications"

echo ""
echo "==> All secrets seeded. Listing paths:"
run_bao kv list secret/ 2>/dev/null || echo "(dev mode listing)"
echo ""
echo "==> Done. Secrets are at: ${BAO_ADDR}/v1/secret/data/<path>"
