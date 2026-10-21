#!/bin/sh
# Host-side artifact verification, value-free: cosign for the signature,
# `gh attestation verify` for the GitHub build provenance (a Sigstore bundle
# that cosign's legacy attestation lookup cannot see).
# Usage: verify_artifact.sh <image_ref_with_digest> <expected_digest>
# Exit: 0 verified; 1 verification failed; 2 usage error; 3 tool missing.
# gh needs read access to the attestations API (GH_TOKEN or a prior gh auth
# login) and registry credentials for the image.
# The identity regexp is fixed here (host config in the agent era): the
# artifact/document being verified never chooses whose signatures count.
set -eu
REF="${1:-}"; EXPECTED="${2:-}"
[ -n "$REF" ] && [ -n "$EXPECTED" ] || { echo "usage: $0 <image@sha256:...> <expected_digest>" >&2; exit 2; }
case "$EXPECTED" in sha256:*) ;; *) echo "expected digest must be sha256:..." >&2; exit 2 ;; esac
case "$REF" in *@sha256:*) ;; *) echo "image ref must be pinned by digest" >&2; exit 2 ;; esac
for tool in cosign gh; do
  command -v "$tool" >/dev/null 2>&1 || { echo "required tool missing: $tool" >&2; exit 3; }
done
[ "${REF##*@}" = "$EXPECTED" ] || { echo "ARTIFACT VERIFY: FAIL (digest mismatch)"; exit 1; }
IDENTITY='^https://github\.com/michaelayoade/dotmac_sub/\.github/workflows/release-candidate\.yml@refs/heads/main$'
ISSUER='https://token.actions.githubusercontent.com'
cosign verify --certificate-identity-regexp "$IDENTITY" \
  --certificate-oidc-issuer "$ISSUER" --output json "$REF" >/dev/null 2>&1 || {
  echo "ARTIFACT VERIFY: FAIL (cosign signature)"; exit 1; }
gh attestation verify "oci://$REF" \
  --repo michaelayoade/dotmac_sub \
  --signer-workflow michaelayoade/dotmac_sub/.github/workflows/release-candidate.yml \
  --source-ref refs/heads/main >/dev/null 2>&1 || {
  echo "ARTIFACT VERIFY: FAIL (GitHub build provenance)"; exit 1; }
echo "ARTIFACT VERIFY: PASS"
