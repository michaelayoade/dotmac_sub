#!/bin/sh
# Gate-0 Proof 1 modes (design §3.4/§6), run by openbao-jwt-proof.yml jobs.
#   positive  rows 1+8+9: gate0-proof login; staging canary sha256 matches
#             EXPECTED_CANARY_SHA256 (value never printed); production
#             canary read = 403; sys/ + auth/jwt/role reads = 403.
#   negative  rows 2-7: login MUST be refused (no client_token issued).
# Row 10 (JWT replay, decision D5) is documented, never asserted.
# Env: OPENBAO_ADDR, PROOF_MODE, PROOF_ROLE, PROOF_AUDIENCE,
#      EXPECTED_CANARY_SHA256 (positive), PROOF_SLEEP_SECONDS (row 6).
# Exit: 0 = expectation met, 1 = expectation violated, 2 = usage/env error.
set -eu
[ -n "${OPENBAO_ADDR:-}" ] && [ -n "${PROOF_MODE:-}" ] && [ -n "${PROOF_ROLE:-}" ] || {
  echo "missing required env (OPENBAO_ADDR/PROOF_MODE/PROOF_ROLE)" >&2; exit 2; }
AUD="${PROOF_AUDIENCE:-dotmac-openbao}"
case "$OPENBAO_ADDR" in
  http://127.0.0.1*|http://localhost*|http://\[::1\]*|https://*) ;;
  *) echo "refusing non-loopback plain-HTTP OPENBAO_ADDR" >&2; exit 2 ;;
esac
[ -n "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] && [ -n "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ] || {
  echo "OIDC request env absent (job lacks id-token: write?)" >&2; exit 2; }
# Direct-curl login per design §3.2 (no vault-action).
JWT=$(curl -fsS -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${AUD}" | jq -er '.value')
[ "${PROOF_SLEEP_SECONDS:-0}" != "0" ] && { echo "row 6: waiting ${PROOF_SLEEP_SECONDS}s for JWT expiry"; sleep "$PROOF_SLEEP_SECONDS"; }
LOGIN_JSON=$(curl -sS -X POST -H 'Content-Type: application/json' \
  -d "{\"role\":\"$PROOF_ROLE\",\"jwt\":\"$JWT\"}" \
  "$OPENBAO_ADDR/v1/auth/jwt/login" || true)
TOKEN=$(printf '%s' "$LOGIN_JSON" | jq -r '.auth.client_token // empty')
if [ "$PROOF_MODE" = "negative" ]; then
  [ -z "$TOKEN" ] && { echo "negative OK: login refused role=$PROOF_ROLE aud=$AUD"; exit 0; }
  echo "negative FAILED: login unexpectedly succeeded role=$PROOF_ROLE" >&2; exit 1
fi
[ "$PROOF_MODE" = "positive" ] || { echo "unknown PROOF_MODE=$PROOF_MODE" >&2; exit 2; }
[ -n "$TOKEN" ] || { echo "positive FAILED: login refused" >&2; exit 1; }
echo "::add-mask::$TOKEN"
VALUE=$(curl -fsS -H "X-Vault-Token: $TOKEN" \
  "$OPENBAO_ADDR/v1/secret/data/dotmac/sub/staging/gate0-canary" | jq -er '.data.data.value')
ACTUAL_SHA=$(printf '%s' "$VALUE" | sha256sum | awk '{print $1}')
[ -n "${EXPECTED_CANARY_SHA256:-}" ] || { echo "EXPECTED_CANARY_SHA256 required (vars.GATE0_CANARY_SHA256)" >&2; exit 2; }
[ "$ACTUAL_SHA" = "$EXPECTED_CANARY_SHA256" ] || { echo "positive FAILED: canary sha256 mismatch" >&2; exit 1; }
status() { curl -sS -o /dev/null -w '%{http_code}' -H "X-Vault-Token: $TOKEN" "$OPENBAO_ADDR/v1/$1"; }
PROD=$(status "secret/data/dotmac/sub/production/gate0-negative")
SYS=$(status "sys/policies/acl/gate0-proof")
ROLE=$(status "auth/jwt/role/gate0-proof")
echo "canary sha256 OK; production=$PROD sys=$SYS role=$ROLE (all want 403)"
[ "$PROD" = "403" ] && [ "$SYS" = "403" ] && [ "$ROLE" = "403" ] && { echo "positive OK"; exit 0; }
echo "positive FAILED" >&2; exit 1
