"""Deployment-descriptor evidence stays canonical and non-authoritative.

These guards keep the evidence slice honest without pinning the Foundation
package yet: the product manifest and conformance inventory must equal their
generators, the descriptor must classify exactly the Compose topology, its
migration heads must equal Alembic's composed repository heads, and nothing
in the live deploy path may start consuming the descriptor in this slice.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from scripts.ci.migrated_test_database import repository_heads
from scripts.deployment_conformance import (
    canonical_inventory_bytes,
)
from scripts.product_manifest import verify_product_manifest

ROOT = Path(__file__).resolve().parents[2]
DESCRIPTOR = ROOT / "deploy/product.toml"


def _descriptor() -> dict[str, object]:
    return tomllib.loads(DESCRIPTOR.read_text(encoding="utf-8"))


def test_product_manifest_is_canonical_and_bound_to_descriptor() -> None:
    snapshot = verify_product_manifest(
        path=ROOT / "deploy/product-manifest.json",
        version_file=ROOT / "VERSION",
    )

    assembly = _descriptor()["assembly"]
    assert isinstance(assembly, dict)
    assert assembly["manifest_path"] == "deploy/product-manifest.json"
    assert assembly["manifest_digest"] == str(snapshot.digest)


def test_conformance_inventory_is_canonical() -> None:
    inventory = ROOT / "deploy/conformance-inventory.json"
    assert inventory.read_bytes() == canonical_inventory_bytes()


def test_descriptor_identity_and_unbound_image_are_explicit() -> None:
    descriptor = _descriptor()
    assert descriptor["schema"] == "ProductDeploymentSpec.v1"
    assert descriptor["product"] == "dotmac-sub"

    image = descriptor["image"]
    assert isinstance(image, dict)
    # The all-zero sentinel is the honest unbound marker recorded in
    # deploy/README.md; a real digest may only replace it together with CI
    # evidence tying it to source_revision.
    assert image["reference"].endswith("@sha256:" + "0" * 64)
    assert len(str(image["source_revision"])) == 40


def test_descriptor_roles_match_inventory_classification() -> None:
    descriptor = _descriptor()
    inventory = json.loads(
        (ROOT / "deploy/conformance-inventory.json").read_text(encoding="utf-8")
    )
    classified_roles = {
        record["descriptor_code"]
        for record in inventory["services"]
        if record["classification"] == "role"
    }
    descriptor_roles = {role["code"] for role in descriptor["roles"]}
    assert descriptor_roles == classified_roles

    classified_managed = {
        record["descriptor_code"]
        for record in inventory["services"]
        if record["classification"] == "managed_dependency"
    }
    descriptor_managed = {
        dep["code"] for dep in descriptor["external_dependencies"] if dep.get("image")
    }
    assert descriptor_managed == classified_managed


def test_descriptor_migration_heads_match_repository_heads() -> None:
    migration = _descriptor()["migration"]
    assert isinstance(migration, dict)
    assert set(migration["expected_heads"]) == set(repository_heads())
    # Credential separation by name: no runtime role may hold the owner
    # material (the Foundation cross-field rule, checked without the pin).
    owner = migration["owner_material"]
    for role in _descriptor()["roles"]:
        assert owner not in role.get("materials", [])


def test_descriptor_is_not_consumed_by_the_live_deploy_path() -> None:
    deploy_script = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "deploy/product.toml" not in deploy_script
    assert "product.toml" not in compose
