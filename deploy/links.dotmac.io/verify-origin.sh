#!/usr/bin/env bash
# Health check for the verified redirect boundary's ORIGIN.
#
# Read-only: it fetches, it never deploys. Run it from anywhere with network
# access to the origin, and after every certificate renewal or content change.
#
# Everything is a knob with a documented default:
#   $1 / ORIGIN         default https://links.dotmac.io
#   $2 / CALLBACK_PATH  default /oidc/field/callback
#
#   ./verify-origin.sh
#   ./verify-origin.sh https://links.staging.dotmac.io /oidc/field/callback
#
# This checks the PROPERTIES the OS verifiers depend on. It does NOT check that
# the identifiers inside the documents are correct -- that is
# scripts/check_field_applinks.py --require-real, which runs offline in CI.
set -uo pipefail

ORIGIN="${1:-${ORIGIN:-https://links.dotmac.io}}"
CALLBACK_PATH="${2:-${CALLBACK_PATH:-/oidc/field/callback}}"
ORIGIN="${ORIGIN%/}"

fail=0
note() { printf '  %s\n' "$*"; }
ok()   { printf 'PASS  %s\n' "$*"; }
bad()  { printf 'FAIL  %s\n' "$*"; fail=$((fail + 1)); }

check_document() {
  local url="$1" want_type="$2"

  # --max-redirs 0 is the whole point: Android's verifier and Apple's CDN follow
  # ZERO redirects and read one as "document absent".
  local head
  head="$(curl -sS -o /dev/null -D - --max-redirs 0 \
            -w '\nHTTP_CODE=%{http_code}\nREDIRECTS=%{num_redirects}\n' \
            "$url" 2>&1)" || { bad "$url: request failed"; note "$head"; return; }

  local code type redirects
  code="$(printf '%s' "$head" | sed -n 's/^HTTP_CODE=//p')"
  redirects="$(printf '%s' "$head" | sed -n 's/^REDIRECTS=//p')"
  type="$(printf '%s' "$head" | tr -d '\r' \
            | sed -n 's/^[Cc]ontent-[Tt]ype: *//p' | tail -1 | cut -d';' -f1)"

  [ "$code" = "200" ] && ok "$url -> 200" || bad "$url -> HTTP $code (expected 200)"
  [ "${redirects:-0}" = "0" ] && ok "$url served with no redirect" \
    || bad "$url followed $redirects redirect(s); both verifiers follow none"
  [ "$type" = "$want_type" ] && ok "$url Content-Type: $type" \
    || bad "$url Content-Type is '${type:-<none>}' (expected $want_type)"

  # Anonymous fetch: no auth, no cookie, no bot filter. Apple fetches through
  # its own CDN from addresses nobody can allow-list in advance.
  local body
  body="$(curl -sS --max-redirs 0 "$url")" || { bad "$url: body fetch failed"; return; }
  if printf '%s' "$body" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
    ok "$url is valid JSON"
  else
    bad "$url is not valid JSON"
  fi
  case "$body" in
    *__ANDROID_CERT_SHA256__*|*__APPLE_TEAM_ID__*)
      bad "$url STILL CONTAINS AN IDENTITY PLACEHOLDER -- it must not be published" ;;
    *) ok "$url carries no identity placeholder" ;;
  esac
}

printf 'Verified redirect boundary health check\n  origin: %s\n\n' "$ORIGIN"

# --- TLS: publicly trusted, unexpired. Both verifiers refuse a bad chain, and
#     they refuse it SILENTLY.
if curl -sS -o /dev/null "$ORIGIN/" --max-redirs 0 2>/dev/null \
   || curl -sS -o /dev/null "$ORIGIN$CALLBACK_PATH" --max-redirs 0 2>/dev/null; then
  ok "$ORIGIN presents a certificate curl's default trust store accepts"
else
  bad "$ORIGIN TLS handshake/validation failed with the default trust store"
fi

check_document "$ORIGIN/.well-known/assetlinks.json" "application/json"
check_document "$ORIGIN/.well-known/apple-app-site-association" "application/json"

# --- The callback path itself must exist for the browser fallback.
code="$(curl -sS -o /dev/null --max-redirs 0 -w '%{http_code}' "$ORIGIN$CALLBACK_PATH")"
[ "$code" = "200" ] && ok "$ORIGIN$CALLBACK_PATH -> 200 (browser fallback)" \
  || bad "$ORIGIN$CALLBACK_PATH -> HTTP $code (expected 200)"

# --- Google's own hosted verifier: what Android actually asks.
printf '\nGoogle Digital Asset Links API (what Android asks at install time):\n'
host="${ORIGIN#https://}"
note "https://digitalassetlinks.googleapis.com/v1/statements:list?source.web.site=https://${host}&relation=delegate_permission/common.handle_all_urls"

printf '\n'
if [ "$fail" -eq 0 ]; then
  printf 'All origin checks passed.\n'
else
  printf '%d check(s) FAILED.\n' "$fail"
fi
exit "$fail"
