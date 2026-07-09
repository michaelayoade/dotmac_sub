#!/usr/bin/env bash
# Prune old deployment image tags while keeping rollback headroom.
set -euo pipefail

IMAGE_REPO="${IMAGE_REPO:-ghcr.io/michaelayoade/dotmac_sub}"
RETAIN_IMAGES="${RETAIN_IMAGES:-5}"
TAG_REGEX="${TAG_REGEX:-^sha-[0-9a-f]+$}"
DRY_RUN="${DRY_RUN:-0}"

if ! [[ "${RETAIN_IMAGES}" =~ ^[0-9]+$ ]] || [[ "${RETAIN_IMAGES}" -lt 1 ]]; then
  echo "RETAIN_IMAGES must be a positive integer" >&2
  exit 1
fi

declare -A used_ids=()
while IFS= read -r container_image; do
  [[ -n "${container_image}" ]] || continue
  image_id="$(docker image inspect --format '{{.Id}}' "${container_image}" 2>/dev/null || true)"
  [[ -n "${image_id}" ]] && used_ids["${image_id}"]=1
done < <(docker ps -a --format '{{.Image}}' | sort -u)

mapfile -t image_rows < <(
  docker image ls "${IMAGE_REPO}" \
    --format '{{.CreatedAt}}\t{{.ID}}\t{{.Repository}}:{{.Tag}}' \
    | sort -r
)

kept_unused=0
for row in "${image_rows[@]}"; do
  image_id="$(cut -f2 <<<"${row}")"
  image_ref="$(cut -f3 <<<"${row}")"
  image_tag="${image_ref##*:}"
  [[ "${image_tag}" =~ ${TAG_REGEX} ]] || continue
  full_id="$(docker image inspect --format '{{.Id}}' "${image_ref}" 2>/dev/null || true)"

  if [[ -n "${full_id}" && -n "${used_ids[$full_id]:-}" ]]; then
    echo "keeping in-use image: ${image_ref}"
    continue
  fi

  if (( kept_unused < RETAIN_IMAGES )); then
    echo "keeping rollback image: ${image_ref}"
    kept_unused=$((kept_unused + 1))
    continue
  fi

  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "would remove old image: ${image_ref} (${image_id})"
  else
    echo "removing old image: ${image_ref} (${image_id})"
    docker image rm "${image_ref}"
  fi
done
