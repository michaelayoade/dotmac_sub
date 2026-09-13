"""Architecture guards for service_intent.offer_access_requirement.

Sensitivity is proven both ways: a planted leak of ``AccessRequirement`` into
forbidden territory must be caught (see the inline near-miss checks below),
and the guard must not fire on the current, clean tree.
"""

from __future__ import annotations

from pathlib import Path

from app.services.rbac_catalog import _PERMISSION_KEY_PATTERN
from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]

# Every location allowed to reference AccessRequirement (the model enum) or
# the string "access_requirement" as a real field/column/permission concept.
_ALLOWED_PATHS = (
    "app/services/catalog/offer_access_requirement.py",
    "app/models/catalog.py",
    "app/schemas/catalog.py",
    "app/services/catalog/offers.py",
    "app/api/catalog.py",
    "app/services/sot_registry/domains/service_intent_control_plane.py",
    "app/services/events/types.py",
    "alembic/versions/607_offer_access_requirement.py",
    "alembic/versions/608_offer_access_requirement_classify_permission.py",
    "scripts/catalog/classify_offer_access_requirement.py",
    "docs/SOT_RELATIONSHIP_MAP.md",
    "docs/designs/CATALOG_ACCESS_REQUIREMENT_AUTHORITY.md",
)

# Directories/files that must NEVER mention AccessRequirement, per the brief:
# connection-type/PPPoE/RADIUS/enforcement/missing_login code.
_FORBIDDEN_PATHS = (
    "app/models/network.py",
    "app/services/radius.py",
    "app/services/catalog/radius.py",
    "app/services/network",
)


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _iter_python_files():
    for path in ROOT.rglob("*.py"):
        if "/.venv/" in str(path) or "/node_modules/" in str(path):
            continue
        yield path


def test_access_requirement_reference_is_confined_to_the_owner_and_declared_seams():
    token = "AccessRequirement"
    leaks: list[str] = []
    for path in _iter_python_files():
        relative = str(path.relative_to(ROOT))
        if relative.startswith("tests/"):
            continue
        if relative in _ALLOWED_PATHS:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if token in text:
            leaks.append(relative)
    assert leaks == [], f"AccessRequirement leaked outside its owner: {leaks}"


def test_confinement_guard_catches_a_planted_leak_in_forbidden_territory(tmp_path):
    """Sensitivity proof: a planted reference in forbidden territory is caught."""

    planted = ROOT / "app" / "services" / "network" / "_planted_leak_test_only.py"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("from app.models.catalog import AccessRequirement\n")
    try:
        leaks = [
            str(path.relative_to(ROOT))
            for path in _iter_python_files()
            if not str(path.relative_to(ROOT)).startswith("tests/")
            and str(path.relative_to(ROOT)) not in _ALLOWED_PATHS
            and "AccessRequirement" in path.read_text(encoding="utf-8", errors="ignore")
        ]
        assert "app/services/network/_planted_leak_test_only.py" in leaks
    finally:
        planted.unlink()


def test_confinement_guard_does_not_flag_an_unrelated_lowercase_near_miss():
    """Near-miss proof: an unrelated lowercase field name is not mistaken for
    the guarded ``AccessRequirement`` class-name token."""

    planted = ROOT / "app" / "services" / "network" / "_planted_near_miss_test_only.py"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(
        "# An unrelated lowercase 'access_requirement' string key, e.g. from a\n"
        "# third-party payload, is not the guarded token and must not trip the\n"
        "# confinement guard.\n"
        "PAYLOAD_KEY = 'access_requirement'\n"
    )
    try:
        leaks = [
            str(path.relative_to(ROOT))
            for path in _iter_python_files()
            if not str(path.relative_to(ROOT)).startswith("tests/")
            and str(path.relative_to(ROOT)) not in _ALLOWED_PATHS
            and "AccessRequirement" in path.read_text(encoding="utf-8", errors="ignore")
        ]
        assert "app/services/network/_planted_near_miss_test_only.py" not in leaks
    finally:
        planted.unlink()


def test_offer_access_requirement_permission_key_matches_the_repo_pattern():
    assert _PERMISSION_KEY_PATTERN.fullmatch(
        "catalog:offer_access_requirement:classify"
    )
    assert not _PERMISSION_KEY_PATTERN.fullmatch(
        "catalog:offer-access-requirement:classify"
    )


def test_offer_access_requirement_permission_is_not_seeded_into_any_role():
    migration = _source(
        "alembic/versions/608_offer_access_requirement_classify_permission.py"
    )
    assert "INSERT INTO role_permissions" not in migration
    assert "catalog:billing_write" not in migration


def test_offer_access_requirement_never_reuses_billing_write():
    owner = _source("app/services/catalog/offer_access_requirement.py")
    assert "catalog:billing_write" not in owner


def test_offer_versions_create_delegates_admission_to_the_new_owner():
    offers_service = _source("app/services/catalog/offers.py")
    assert "offer_access_requirement.validate_admission_access_requirement(" in (
        offers_service
    )
    assert "offer_access_requirement.assert_access_requirement_immutable(" in (
        offers_service
    )


def test_offer_access_requirement_owner_never_completes_its_own_transaction():
    owner = _source("app/services/catalog/offer_access_requirement.py")
    assert "execute_owner_command(" in owner
    assert "db.commit(" not in owner
    assert "db.rollback(" not in owner


def test_offer_access_requirement_is_registered_as_a_new_contracted_owner():
    service = service_relationship("service_intent.offer_access_requirement")
    assert service.module == "app.services.catalog.offer_access_requirement"
    assert service.contract is not None
    assert set(service.owns) == {
        "access-classified offer-version admission",
        "immutable access requirement for an exact offer version",
        "reviewed classification of legacy/unclassified versions",
    }


def test_catalog_policy_is_left_completely_untouched():
    """Negative control: the deliberately separate owner is unchanged."""

    policies = _source("app/services/catalog/policies.py")
    assert "offer_access_requirement" not in policies
    assert "AccessRequirement" not in policies
    catalog_policy = service_relationship("service_intent.catalog_policy")
    assert catalog_policy.module == "app.services.catalog.policies"
    assert catalog_policy.owns == (
        "catalog policy lookup",
        "offer policy interpretation",
    )
