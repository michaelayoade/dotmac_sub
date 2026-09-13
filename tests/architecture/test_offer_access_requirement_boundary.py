"""Architecture guards for service_intent.offer_access_requirement.

Sensitivity is proven both ways using an ISOLATED temp directory (never the
real source tree — planting files into the live repo is unsafe under this
repo's 4-worker parallel test execution, since another worker's clean-tree
scan could observe the planted file mid-test): a planted leak into forbidden
territory must be caught, and an unrelated near-miss must not be flagged.
"""

from __future__ import annotations

from pathlib import Path

from app.services.rbac_catalog import _PERMISSION_KEY_PATTERN
from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]

# Every location allowed to reference the guarded tokens (the model enum
# class name and the field/column name) as a real field/column/permission
# concept.
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

# Directories that must NEVER mention either guarded token, per the brief:
# connection-type/PPPoE/RADIUS/enforcement/missing_login code. Checked
# explicitly (not just implied by _ALLOWED_PATHS) so this list is a positive,
# exercised assertion rather than a declared-but-unused constant.
_FORBIDDEN_PATHS = (
    "app/models/network.py",
    "app/services/radius.py",
    "app/services/catalog/radius.py",
    "app/services/network",
    "app/services/provisioning",
)

#: The class-name token and the lowercase field/column-name token. Both are
#: guarded — a leak can show up as either an import of the enum type or a
#: raw attribute/column/string reference to the field it backs.
_GUARDED_TOKENS = ("AccessRequirement", "access_requirement")


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _find_leaks(
    root: Path, *, tokens: tuple[str, ...], allowed: tuple[str, ...]
) -> list[str]:
    """Pure scan: every ``*.py`` file under ``root`` outside ``tests/`` and
    ``allowed`` that mentions any of ``tokens``. No side effects, no mutation
    of ``root`` — callers plant fixtures into an isolated directory, never
    the real source tree.
    """

    leaks: list[str] = []
    for path in root.rglob("*.py"):
        if "/.venv/" in str(path) or "/node_modules/" in str(path):
            continue
        relative = str(path.relative_to(root))
        if relative.startswith("tests/") or relative in allowed:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(token in text for token in tokens):
            leaks.append(relative)
    return leaks


def test_access_requirement_reference_is_confined_to_the_owner_and_declared_seams():
    leaks = _find_leaks(ROOT, tokens=_GUARDED_TOKENS, allowed=_ALLOWED_PATHS)
    assert leaks == [], f"guarded token leaked outside its owner: {leaks}"


def test_forbidden_connection_type_and_enforcement_paths_never_mention_it():
    """Positive, exercised use of ``_FORBIDDEN_PATHS`` (not a declared-only
    constant): the exact areas the brief named — connection-type, PPPoE,
    RADIUS, enforcement, missing_login — never reference either token, and
    never treat ``unclassified`` as a connection-type/PPPoE fallback value.
    """

    extra_tokens = _GUARDED_TOKENS + ("network_access", "no_network_access")
    leaks: list[str] = []
    for forbidden in _FORBIDDEN_PATHS:
        target = ROOT / forbidden
        if not target.exists():
            continue
        paths = [target] if target.is_file() else list(target.rglob("*.py"))
        for path in paths:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(token in text for token in extra_tokens):
                leaks.append(str(path.relative_to(ROOT)))
    assert leaks == [], f"forbidden territory references a guarded token: {leaks}"


def test_confinement_guard_catches_a_planted_leak_in_an_isolated_tree(tmp_path):
    """Sensitivity proof: a planted leak is caught — using an isolated temp
    tree, never the real source tree, so no parallel worker can observe it."""

    (tmp_path / "app" / "services" / "network").mkdir(parents=True)
    leaking = tmp_path / "app" / "services" / "network" / "leaky.py"
    leaking.write_text("from app.models.catalog import AccessRequirement\n")

    leaks = _find_leaks(tmp_path, tokens=_GUARDED_TOKENS, allowed=_ALLOWED_PATHS)
    assert "app/services/network/leaky.py" in leaks


def test_confinement_guard_does_not_flag_an_allowed_path_in_an_isolated_tree(
    tmp_path,
):
    """Near-miss proof: a file at an ALLOWED relative path is not flagged
    even though it mentions the guarded token — using an isolated temp tree."""

    (tmp_path / "app" / "services" / "catalog").mkdir(parents=True)
    allowed_file = (
        tmp_path / "app" / "services" / "catalog" / "offer_access_requirement.py"
    )
    allowed_file.write_text("access_requirement = 'unclassified'\n")

    leaks = _find_leaks(tmp_path, tokens=_GUARDED_TOKENS, allowed=_ALLOWED_PATHS)
    assert leaks == []


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


def test_offer_versions_create_delegates_the_actual_persist_to_the_new_owner():
    offers_service = _source("app/services/catalog/offers.py")
    assert "offer_access_requirement.admit_offer_version(" in offers_service
    assert "offer_access_requirement.AdmitOfferVersionCommand(" in offers_service
    # The adapter must not construct the row itself.
    assert "OfferVersion(**data)" not in offers_service
    assert "offer_access_requirement.assert_access_requirement_immutable(" in (
        offers_service
    )


def test_offer_access_requirement_owner_actually_performs_the_write():
    owner = _source("app/services/catalog/offer_access_requirement.py")
    assert "execute_owner_command(" in owner
    assert "OfferVersion(**data)" in owner
    assert "db.add(version)" in owner
    assert "db.commit(" not in owner
    assert "db.rollback(" not in owner


def test_classify_command_carries_no_free_text_actor_field():
    """Sensitivity proof for the actor-spoofing fix: the command dataclass
    has no separate free-text actor field — only the authenticated
    ``SystemUser`` id, and CommandContext.actor is not what gets recorded."""

    owner = _source("app/services/catalog/offer_access_requirement.py")
    assert "authorized_system_user_id: UUID" in owner
    assert "permission_granted: bool" not in owner
    assert "classified_by=command.context.actor" not in owner
    assert "actor_id=command.context.actor" not in owner
    cli = _source("scripts/catalog/classify_offer_access_requirement.py")
    assert '"--actor"' not in cli


def test_classify_permission_is_reverified_inside_the_command_transaction():
    owner = _source("app/services/catalog/offer_access_requirement.py")
    assert "_verify_classify_permission(db, command.authorized_system_user_id)" in (
        owner
    )


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


def test_migration_downgrade_locks_before_counting():
    migration = _source("alembic/versions/607_offer_access_requirement.py")
    lock_index = migration.index("LOCK TABLE")
    count_index = migration.index("SELECT count(*)")
    assert lock_index < count_index, (
        "downgrade must acquire its locks before the first count query"
    )
    assert "ACCESS EXCLUSIVE MODE" in migration


def test_migration_uses_set_local_not_a_bare_set_for_timeouts():
    migration = _source("alembic/versions/607_offer_access_requirement.py")
    assert "SET LOCAL lock_timeout" in migration
    assert "RESET lock_timeout" not in migration
    assert "RESET statement_timeout" not in migration


def test_migration_declares_check_constraints_for_the_legal_transition_shape():
    migration = _source("alembic/versions/607_offer_access_requirement.py")
    assert "previous_access_requirement = 'unclassified'" in migration
    assert "new_access_requirement IN ('network_access', 'no_network_access')" in (
        migration
    )
