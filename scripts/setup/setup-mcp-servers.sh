#!/usr/bin/env bash
# setup-mcp-servers.sh — Install MCP server dependencies and create read-only DB users
# Idempotent: safe to re-run.
#
# Usage:
#   bash scripts/setup-mcp-servers.sh
#
# Prerequisites:
#   - Docker containers running: dotmac_sub_radius_db, dotmac_sub_genieacs_mongodb
#   - VictoriaMetrics container already running (no setup needed)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[SKIP]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; }

# ─── 1. Install mcp-victoriametrics binary ──────────────────────────────────

echo ""
echo "=== Step 1: Install mcp-victoriametrics binary ==="

if command -v mcp-victoriametrics &>/dev/null; then
    info "mcp-victoriametrics already installed at $(command -v mcp-victoriametrics)"
else
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  ARCH_SUFFIX="x86_64" ;;
        aarch64) ARCH_SUFFIX="arm64" ;;
        *)       fail "Unsupported architecture: $ARCH"; exit 1 ;;
    esac

    OS=$(uname -s)
    TARBALL="mcp-victoriametrics_${OS}_${ARCH_SUFFIX}.tar.gz"
    URL="https://github.com/VictoriaMetrics-Community/mcp-victoriametrics/releases/latest/download/${TARBALL}"

    echo "Downloading ${TARBALL}..."
    if curl -sL "$URL" | tar xz -C /tmp mcp-victoriametrics 2>/dev/null; then
        sudo mv /tmp/mcp-victoriametrics /usr/local/bin/mcp-victoriametrics
        sudo chmod +x /usr/local/bin/mcp-victoriametrics
        info "Installed mcp-victoriametrics to /usr/local/bin/"
    else
        fail "Failed to download mcp-victoriametrics from $URL"
        echo "  You can install it manually from: https://github.com/VictoriaMetrics-Community/mcp-victoriametrics/releases"
    fi
fi

# ─── 2. Create read-only RADIUS DB user ─────────────────────────────────────

echo ""
echo "=== Step 2: Create read-only RADIUS DB user ==="

RADIUS_RO_USER="radius_readonly"
RADIUS_RO_PASS="radius_ro_2026"

if docker ps --format '{{.Names}}' | grep -q 'dotmac_sub_radius_db'; then
    # Check if user already exists
    USER_EXISTS=$(docker exec dotmac_sub_radius_db psql -U radius -d radius -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname = '${RADIUS_RO_USER}';" 2>/dev/null || echo "")

    if [ "$USER_EXISTS" = "1" ]; then
        warn "User ${RADIUS_RO_USER} already exists in radius-db"
    else
        docker exec dotmac_sub_radius_db psql -U radius -d radius -c "
            CREATE ROLE ${RADIUS_RO_USER} WITH LOGIN PASSWORD '${RADIUS_RO_PASS}';
            GRANT CONNECT ON DATABASE radius TO ${RADIUS_RO_USER};
            GRANT USAGE ON SCHEMA public TO ${RADIUS_RO_USER};
            GRANT SELECT ON ALL TABLES IN SCHEMA public TO ${RADIUS_RO_USER};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ${RADIUS_RO_USER};
        " 2>/dev/null && info "Created ${RADIUS_RO_USER} user in radius-db" \
                       || fail "Failed to create ${RADIUS_RO_USER} user"
    fi
else
    warn "radius-db container not running — skipping user creation"
    echo "  Start it with: docker compose up -d radius-db"
    echo "  Then re-run this script."
fi

# ─── 3. Create read-only GenieACS MongoDB user ──────────────────────────────

echo ""
echo "=== Step 3: Create read-only GenieACS MongoDB user ==="

MONGO_RO_USER="genieacs_readonly"
MONGO_RO_PASS="genieacs_ro_2026"
MONGO_ADMIN_USER="genieacs"
MONGO_ADMIN_PASS="Rfys2820k6c0Mkq3xw5CTjVzhn6WeAjm"

if docker ps --format '{{.Names}}' | grep -q 'dotmac_sub_genieacs_mongodb'; then
    # Check if user already exists
    USER_EXISTS=$(docker exec dotmac_sub_genieacs_mongodb mongo \
        --username "$MONGO_ADMIN_USER" --password "$MONGO_ADMIN_PASS" --authenticationDatabase admin \
        --quiet --eval "db.getSiblingDB('admin').getUser('${MONGO_RO_USER}')" 2>/dev/null || echo "null")

    if [ "$USER_EXISTS" != "null" ] && [ -n "$USER_EXISTS" ]; then
        warn "User ${MONGO_RO_USER} already exists in genieacs-mongodb"
    else
        docker exec dotmac_sub_genieacs_mongodb mongo \
            --username "$MONGO_ADMIN_USER" --password "$MONGO_ADMIN_PASS" --authenticationDatabase admin \
            --quiet --eval "
                db.getSiblingDB('admin').createUser({
                    user: '${MONGO_RO_USER}',
                    pwd: '${MONGO_RO_PASS}',
                    roles: [{ role: 'read', db: 'genieacs' }]
                });
                print('User created successfully');
            " 2>/dev/null && info "Created ${MONGO_RO_USER} user in genieacs-mongodb" \
                           || fail "Failed to create ${MONGO_RO_USER} user (may already exist)"
    fi
else
    warn "genieacs-mongodb container not running — skipping user creation"
    echo "  Start it with: docker compose up -d genieacs-mongodb"
    echo "  Then re-run this script."
fi

# ─── 4. Verify .env DSN entries ─────────────────────────────────────────────

echo ""
echo "=== Step 4: Verify .env DSN entries ==="

ENV_FILE="${PROJECT_DIR}/.env"

if [ ! -f "$ENV_FILE" ]; then
    fail ".env file not found at ${ENV_FILE}"
    exit 1
fi

check_env_var() {
    local var_name="$1"
    local expected_prefix="$2"
    if grep -q "^${var_name}=" "$ENV_FILE"; then
        info "${var_name} is set in .env"
    else
        warn "${var_name} not found in .env — adding it"
        echo "${var_name}=${expected_prefix}" >> "$ENV_FILE"
        info "Added ${var_name} to .env"
    fi
}

check_env_var "RADIUS_DB_DSN" "postgresql://${RADIUS_RO_USER}:${RADIUS_RO_PASS}@localhost:5437/radius"
check_env_var "GENIEACS_MONGODB_DSN" "mongodb://${MONGO_RO_USER}:${MONGO_RO_PASS}@localhost:27017/genieacs?authSource=admin"

# ─── 5. Verify MCP server config ────────────────────────────────────────────

echo ""
echo "=== Step 5: Verify MCP server configuration ==="

MCP_FILE="${PROJECT_DIR}/.mcp.json"

if [ -f "$MCP_FILE" ]; then
    for server in radius-db genieacs-db victoriametrics; do
        if grep -q "\"${server}\"" "$MCP_FILE"; then
            info "${server} configured in .mcp.json"
        else
            fail "${server} missing from .mcp.json"
        fi
    done
else
    fail ".mcp.json not found"
fi

# ─── 6. Test connectivity ───────────────────────────────────────────────────

echo ""
echo "=== Step 6: Test connectivity ==="

# VictoriaMetrics
if curl -s -o /dev/null -w '%{http_code}' 'http://localhost:8428/api/v1/query?query=up' 2>/dev/null | grep -q '200'; then
    info "VictoriaMetrics responding on port 8428"
else
    warn "VictoriaMetrics not responding on port 8428"
fi

# RADIUS DB
if docker ps --format '{{.Names}}' | grep -q 'dotmac_sub_radius_db'; then
    if docker exec dotmac_sub_radius_db psql -U "$RADIUS_RO_USER" -d radius -c "SELECT 1;" &>/dev/null; then
        info "RADIUS DB: read-only user can connect"
    else
        warn "RADIUS DB: read-only user cannot connect (password may not match)"
    fi
else
    warn "RADIUS DB: container not running"
fi

# GenieACS MongoDB
if docker ps --format '{{.Names}}' | grep -q 'dotmac_sub_genieacs_mongodb'; then
    if docker exec dotmac_sub_genieacs_mongodb mongo \
        --username "$MONGO_RO_USER" --password "$MONGO_RO_PASS" --authenticationDatabase admin \
        --quiet --eval "db.stats()" genieacs &>/dev/null; then
        info "GenieACS MongoDB: read-only user can connect"
    else
        warn "GenieACS MongoDB: read-only user cannot connect"
    fi
else
    warn "GenieACS MongoDB: container not running"
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Start any stopped containers: docker compose up -d radius-db genieacs-mongodb genieacs"
echo "  2. Re-run this script if containers were just started"
echo "  3. Restart Claude Code to pick up new MCP servers"
echo "  4. Test with: /radius-query, /genieacs-devices, /bandwidth-query, /geocode"
