"""Deploy derives its recreate set from Compose: every app-image service.

``team-inbox-smtp`` runs ``${APP_IMAGE}`` but a hand-kept list in
``scripts/deploy.sh`` omitted it, so it stayed on an old image across releases
(a stale enum made inbound emails defer). Deploy now derives the set with
``scripts.deploy_app_image_services``; this guard asserts, on the repo's real
docker-compose.yml, that the derivation covers every service declaring
``image: ${APP_IMAGE...}`` and that deploy.sh has no hand-kept list again.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.deploy_app_image_services import app_image_services

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "registry.example.invalid/app@sha256:" + "a" * 64


def _compose_services() -> dict[str, str]:
    """Map each top-level service to its raw `image:` value (line-based)."""
    images: dict[str, str] = {}
    in_services = False
    current: str | None = None
    for line in (ROOT / "docker-compose.yml").read_text().splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^\S", line) and not line.startswith("#"):
            break
        if not in_services:
            continue
        name = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if name:
            current = name.group(1)
        elif current and (image := re.match(r"^    image:\s*(\S.*?)\s*$", line)):
            images[current] = image.group(1)
    return images


def _resolved(images: dict[str, str]) -> dict[str, object]:
    return {
        "services": {
            name: {"image": RELEASE if image.startswith("${APP_IMAGE") else image}
            for name, image in images.items()
        }
    }


def test_derivation_covers_every_app_image_service_in_compose() -> None:
    images = _compose_services()
    expected = sorted(n for n, i in images.items() if i.startswith("${APP_IMAGE"))
    assert {"app", "celery-worker", "team-inbox-smtp"} <= set(expected)
    assert app_image_services(_resolved(images), RELEASE) == expected


def test_derivation_ignores_non_app_images() -> None:
    derived = app_image_services(_resolved(_compose_services()), RELEASE)
    assert "vmagent" not in derived and "freeradius" not in derived


def test_deploy_script_has_no_hand_kept_app_service_list() -> None:
    text = (ROOT / "scripts" / "deploy.sh").read_text()
    assert re.search(r"^APP_SERVICES=\(\)", text, re.M)
    assert "scripts.deploy_app_image_services" in text


def test_every_app_image_service_has_a_running_replica_in_product_toml() -> None:
    """Zero-replica roles are skipped by drift comparison; running ones must not be."""
    import tomllib

    descriptor = tomllib.loads((ROOT / "deploy" / "product.toml").read_text())
    replicas = {role["code"]: role.get("replicas", 0) for role in descriptor["roles"]}
    for service in app_image_services(_resolved(_compose_services()), RELEASE):
        assert replicas.get(service, 0) >= 1, (
            f"{service} runs the app image but product.toml declares "
            f"replicas={replicas.get(service)}; drift comparison would skip it"
        )
