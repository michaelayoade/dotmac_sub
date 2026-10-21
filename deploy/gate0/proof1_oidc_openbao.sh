#!/bin/sh
# Gate-0 Proof 1 modes (design §3.4/§6), run by openbao-jwt-proof.yml jobs.
#   positive  rows 1+8+9: gate0-proof login; staging canary sha256 matches
#             EXPECTED_CANARY_SHA256 (value never printed); production
#             canary read = 403; sys/ + auth/jwt/role reads = 403.
#   negative  rows 2-7: login MUST be refused by the server (HTTP 400/403 with a
#             JSON errors array, not a role-missing answer). Any other outcome
#             (network error, 404/5xx, non-JSON) is INCONCLUSIVE and exits 1.
# Row 10 (JWT replay, decision D5) is documented, never asserted.
# Env: OPENBAO_ADDR, PROOF_MODE, PROOF_ROLE, PROOF_AUDIENCE,
#      EXPECTED_CANARY_SHA256 (positive), PROOF_SLEEP_SECONDS (row 6).
# Exit: 0 = expectation met, 1 = expectation violated, 2 = usage/env error.
set -eu
[ -n "${OPENBAO_ADDR:-}" ] && [ -n "${PROOF_MODE:-}" ] && [ -n "${PROOF_ROLE:-}" ] || {
  echo "missing required env (OPENBAO_ADDR/PROOF_MODE/PROOF_ROLE)" >&2; exit 2; }
AUD="${PROOF_AUDIENCE:-dotmac-openbao}"
# Exact host match for plain HTTP: loopback names only, optional numeric
# port and path. A prefix glob would accept http://localhost.evil.tld.
loopback_http() {
  rest=${1#http://}
  hostport=${rest%%/*}
  case "$hostport" in *@*|"") return 1 ;; esac
  case "$hostport" in
    "[::1]") return 0 ;;
    "[::1]":*) port=${hostport#"[::1]":} ;;
    127.0.0.1) return 0 ;;
    127.0.0.1:*) port=${hostport#127.0.0.1:} ;;
    localhost) return 0 ;;
    localhost:*) port=${hostport#localhost:} ;;
    *) return 1 ;;
  esac
  case "$port" in ""|*[!0-9]*) return 1 ;; esac
  return 0
}
case "$OPENBAO_ADDR" in
  https://*) ;;
  http://*) loopback_http "$OPENBAO_ADDR" || {
    echo "refusing non-loopback plain-HTTP OPENBAO_ADDR" >&2; exit 2; } ;;
  *) echo "refusing non-loopback plain-HTTP OPENBAO_ADDR" >&2; exit 2 ;;
esac
[ -n "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] && [ -n "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ] || {
  echo "OIDC request env absent (job lacks id-token: write?)" >&2; exit 2; }
# Direct-curl login per design §3.2 (no vault-action).
# Secrets travel on stdin (curl -K -, --data @-), never on argv, so they are
# not visible in /proc/<pid>/cmdline on the shared runner host. printf is a
# shell builtin and does not exec.
JWT=$(printf 'header = "Authorization: bearer %s"\n' "$ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  | curl -fsS -K - "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${AUD}" | jq -er '.value')
[ "${PROOF_SLEEP_SECONDS:-0}" != "0" ] && { echo "row 6: waiting ${PROOF_SLEEP_SECONDS}s for JWT expiry"; sleep "$PROOF_SLEEP_SECONDS"; }
LOGIN_BODY=$(mktemp)
trap 'rm -f "$LOGIN_BODY"' EXIT
LOGIN_CODE=$(JWT="$JWT" jq -nc --arg role "$PROOF_ROLE" '{role:$role,jwt:env.JWT}' \
  | curl -sS -o "$LOGIN_BODY" -w '%{http_code}' -X POST -H 'Content-Type: application/json' --data @- \
    "$OPENBAO_ADDR/v1/auth/jwt/login" || true)
TOKEN=$(jq -r '.auth.client_token // empty' "$LOGIN_BODY" 2>/dev/null || true)
if [ "$PROOF_MODE" = "negative" ]; then
  if [ -n "$TOKEN" ]; then
    echo "negative FAILED: login unexpectedly succeeded role=$PROOF_ROLE" >&2; exit 1
  fi
  # A negative case passes ONLY on a definite server-side login refusal: an
  # HTTP 400/403 whose body is a JSON errors array. A transport failure
  # (000), a 404/5xx, a non-JSON body (proxy page) or a "role not found"
  # answer (the role is missing, so no claim was ever tested) is
  # INCONCLUSIVE and fails the proof rather than counting as a refusal.
  case "$LOGIN_CODE" in
    400|403) ;;
    *) echo "negative INCONCLUSIVE: no definite login refusal (http=$LOGIN_CODE) role=$PROOF_ROLE" >&2; exit 1 ;;
  esac
  jq -e '(.errors | type == "array") and (.errors | length > 0)' "$LOGIN_BODY" >/dev/null 2>&1 || {
    echo "negative INCONCLUSIVE: refusal body is not a JSON errors array (http=$LOGIN_CODE) role=$PROOF_ROLE" >&2; exit 1; }
  if jq -e '[.errors[] | tostring | ascii_downcase | select(test("not found|no such|unknown role|could not be found|does not exist"))] | length > 0' \
    "$LOGIN_BODY" >/dev/null 2>&1; then
    echo "negative INCONCLUSIVE: role missing, claims were not evaluated role=$PROOF_ROLE" >&2; exit 1
  fi
  echo "negative OK: login refused (http=$LOGIN_CODE) role=$PROOF_ROLE aud=$AUD"; exit 0
fi
[ "$PROOF_MODE" = "positive" ] || { echo "unknown PROOF_MODE=$PROOF_MODE" >&2; exit 2; }
[ -n "$TOKEN" ] || { echo "positive FAILED: login refused" >&2; exit 1; }
echo "::add-mask::$TOKEN"
VALUE=$(printf 'header = "X-Vault-Token: %s"\n' "$TOKEN" | curl -fsS -K - \
  "$OPENBAO_ADDR/v1/secret/data/dotmac/sub/staging/gate0-canary" | jq -er '.data.data.value')
ACTUAL_SHA=$(printf '%s' "$VALUE" | sha256sum | awk '{print $1}')
[ -n "${EXPECTED_CANARY_SHA256:-}" ] || { echo "EXPECTED_CANARY_SHA256 required (vars.GATE0_CANARY_SHA256)" >&2; exit 2; }
[ "$ACTUAL_SHA" = "$EXPECTED_CANARY_SHA256" ] || { echo "positive FAILED: canary sha256 mismatch" >&2; exit 1; }
status() { printf 'header = "X-Vault-Token: %s"\n' "$TOKEN" | curl -sS -K - -o /dev/null -w '%{http_code}' "$OPENBAO_ADDR/v1/$1"; }
PROD=$(status "secret/data/dotmac/sub/production/gate0-negative")
SYS=$(status "sys/policies/acl/gate0-proof")
ROLE=$(status "auth/jwt/role/gate0-proof")
echo "canary sha256 OK; production=$PROD sys=$SYS role=$ROLE (all want 403)"
[ "$PROD" = "403" ] && [ "$SYS" = "403" ] && [ "$ROLE" = "403" ] && { echo "positive OK"; exit 0; }
echo "positive FAILED" >&2; exit 1
