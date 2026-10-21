#!/bin/sh
# Installed root-owned release only. Provisioning pins the immutable file digest.
set -eu
PATH=/usr/local/bin:/usr/bin:/bin
export PATH
RELEASE=/opt/dotmac-deploy-agent/release
PIN=/etc/dotmac-deploy-agent/agent.sha256
for directory in /opt/dotmac-deploy-agent "$RELEASE" /etc/dotmac-deploy-agent; do
  [ -d "$directory" ] && [ ! -L "$directory" ] || exit 1
  [ "$(stat -c %u "$directory")" = 0 ] || exit 1
  [ -z "$(find "$directory" -maxdepth 0 -perm /022 -print)" ] || exit 1
done
for path in "$RELEASE/agent.pyz" "$RELEASE/agent.bundle.json" "$PIN"; do
  [ -f "$path" ] && [ ! -L "$path" ] || exit 1
  [ "$(stat -c %u "$path")" = 0 ] || exit 1
  [ -z "$(find "$path" -perm /022 -print)" ] || exit 1
done
cd "$RELEASE"
sha256sum --check --strict "$PIN"
env -i PATH="$PATH" LANG=C.UTF-8 /usr/local/bin/cosign verify-blob --bundle agent.bundle.json \
  --certificate-identity 'https://github.com/michaelayoade/dotmac_sub/.github/workflows/deploy-agent-release.yml@refs/heads/main' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  agent.pyz >/dev/null
exec env -i PATH="$PATH" LANG=C.UTF-8 /opt/dotmac-deploy-agent/venv/bin/python agent.pyz --config /etc/dotmac-deploy-agent/config.toml
