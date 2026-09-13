"""Architecture guards for service_intent.offer_access_requirement.

Sensitivity is proven both ways using an ISOLATED temp directory (never the
real source tree — planting files into the live repo is unsafe under this
repo's 4-worker parallel test execution, since another worker's clean-tree
scan could observe the planted file mid-test): a planted leak into forbidden
territory must be caught, and an unrelated near-miss must not be flagged.
"""

from __future__ import annotations

import ast
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

#: The specific cross-domain vocabulary a forbidden fallback would need to
#: reference: the exact regression the brief names — an access-requirement-
#: to-PPPoE/connection-type fallback hiding INSIDE an already-allowed file
#: (e.g. near ``ConnectionType``/``pppoe`` in ``app/models/catalog.py``),
#: which the wholesale allowlist skip in ``_find_leaks`` cannot see.
_CROSS_DOMAIN_TOKENS = ("ConnectionType", "pppoe", "PPPoE", "PPPOE")

#: How many lines of slack either side of a guarded-token line still count
#: as "nearby" for the content guard below — wide enough to catch a
#: same-block ``if``/``return`` fallback, narrow enough that unrelated
#: mentions elsewhere in a large allowed file (e.g. a docstring explaining
#: the prohibition, or an unrelated class hundreds of lines away) do not
#: false-positive. Verified against every current allowed ``*.py`` file.
_NEARBY_WINDOW = 3


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


def _enclosing_function_span(
    tree: ast.AST, lineno: int
) -> tuple[int, int] | None:
    """The smallest (innermost) function/async-function body containing
    ``lineno`` (1-based), as an inclusive ``(start_line, end_line)`` span, or
    ``None`` if ``lineno`` sits outside every function (e.g. a module-level
    import, class-body field, or top-level constant)."""

    best: tuple[int, int] | None = None
    best_size = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", None) or node.lineno
        if start <= lineno <= end:
            size = end - start
            if best_size is None or size < best_size:
                best = (start, end)
                best_size = size
    return best


def _find_cross_domain_leaks_within_allowed_files(
    root: Path,
    *,
    guarded: tuple[str, ...],
    cross_domain: tuple[str, ...],
    allowed: tuple[str, ...],
    window: int = _NEARBY_WINDOW,
) -> list[str]:
    """Content-level scan INSIDE the allowed files themselves.

    ``_find_leaks`` treats every path in ``allowed`` as entirely out of
    scope — that is correct for the guarded token itself (this is where it
    is meant to live), but it also means a hypothetical
    access-requirement-to-PPPoE/connection-type fallback added inside one of
    those already-allowed files (e.g. near the existing
    ``ConnectionType``/PPPoE definitions in ``app/models/catalog.py``) would
    never be scanned at all.

    For a guarded-token line that sits inside a function/async-function
    body, this checks the ENTIRE enclosing function body for a cross-domain
    token — an AST scope check, not a fixed line-window, so a fallback split
    across an ``if``/``elif`` (condition on one line, the forbidden token
    several lines later in the body) is caught exactly the same as one
    planted on a single line. A guarded-token line with no enclosing
    function (e.g. a class-body field or a top-level import) falls back to
    the narrow ``window``-line check, which is verified against every
    current allowed file's real content: none of them trip this today, and
    such lines are far less likely to hide a derived fallback than a
    function body is.

    Restricted to ``*.py`` allowed paths: the design docs in ``allowed``
    (``docs/...md``) legitimately DISCUSS this exact prohibition in prose
    (e.g. "never treated like PPPoE or any connection-type fallback") and
    are out of scope here, matching ``_find_leaks``'s own ``*.py``-only
    scope.
    """

    leaks: list[str] = []
    for relative in allowed:
        if not relative.endswith(".py"):
            continue
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        guarded_line_indexes = [
            index
            for index, line in enumerate(lines)
            if any(token in line for token in guarded)
        ]
        for index in guarded_line_indexes:
            lineno = index + 1
            span = _enclosing_function_span(tree, lineno) if tree is not None else None
            if span is not None:
                start_line, end_line = span
                scope_text = "\n".join(lines[start_line - 1 : end_line])
            else:
                start = max(0, index - window)
                end = min(len(lines), index + window + 1)
                scope_text = "\n".join(lines[start:end])
            if any(token in scope_text for token in cross_domain):
                leaks.append(f"{relative}:{index + 1}")
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


def test_no_allowed_file_hides_a_connection_type_or_pppoe_fallback():
    """The allowlist skip does not create a blind spot in the real tree:
    none of today's allowed files contain an access-requirement-to-PPPoE/
    connection-type fallback."""

    leaks = _find_cross_domain_leaks_within_allowed_files(
        ROOT,
        guarded=_GUARDED_TOKENS,
        cross_domain=_CROSS_DOMAIN_TOKENS,
        allowed=_ALLOWED_PATHS,
    )
    assert leaks == [], (
        "an access-requirement-to-connection-type/PPPoE fallback leaked "
        f"inside an already-allowed file: {leaks}"
    )


def test_content_guard_catches_a_fallback_planted_inside_an_allowed_file(
    tmp_path,
):
    """Sensitivity proof for the content-level guard: a hypothetical
    access-requirement-to-PPPoE fallback planted INSIDE an already-allowed
    file (mirroring the brief's own example: near ``ConnectionType``/PPPoE
    in ``app/models/catalog.py``) is caught, proving the wholesale
    allowlist skip in ``_find_leaks`` no longer hides this regression."""

    (tmp_path / "app" / "models").mkdir(parents=True)
    planted = tmp_path / "app" / "models" / "catalog.py"
    planted.write_text(
        "class ConnectionType(enum.Enum):\n"
        "    pppoe = 'pppoe'\n"
        "\n"
        "def resolve_default_connection(access_requirement):\n"
        "    fallback = ConnectionType.pppoe if access_requirement == "
        "AccessRequirement.unclassified else None\n"
        "    return fallback\n"
    )

    leaks = _find_cross_domain_leaks_within_allowed_files(
        tmp_path,
        guarded=_GUARDED_TOKENS,
        cross_domain=_CROSS_DOMAIN_TOKENS,
        allowed=("app/models/catalog.py",),
    )
    assert any(leak.startswith("app/models/catalog.py:") for leak in leaks)


def test_content_guard_catches_a_fallback_split_across_an_if_elif_block(
    tmp_path,
):
    """Sensitivity proof for the AST-scope broadening: a fallback whose two
    halves are more than ``_NEARBY_WINDOW`` (3) physical lines apart — the
    guarded token only in an ``if``/``elif`` CONDITION, the cross-domain
    token several lines later in the body — used to pass the old fixed
    line-window check undetected. It must still be caught because both
    tokens live inside the SAME enclosing function."""

    (tmp_path / "app" / "models").mkdir(parents=True)
    planted = tmp_path / "app" / "models" / "catalog.py"
    planted.write_text(
        "def resolve_default_connection(offer_version):\n"
        "    if offer_version.access_requirement == 'unclassified':\n"
        "        # filler line 1\n"
        "        # filler line 2\n"
        "        # filler line 3\n"
        "        # filler line 4\n"
        "        fallback = 'pppoe'\n"
        "    elif offer_version.access_requirement == 'network_access':\n"
        "        fallback = None\n"
        "    else:\n"
        "        fallback = None\n"
        "    return fallback\n"
    )

    leaks = _find_cross_domain_leaks_within_allowed_files(
        tmp_path,
        guarded=_GUARDED_TOKENS,
        cross_domain=_CROSS_DOMAIN_TOKENS,
        allowed=("app/models/catalog.py",),
    )
    assert any(leak.startswith("app/models/catalog.py:2") for leak in leaks), leaks


def test_content_guard_does_not_flag_unrelated_definitions_sharing_one_file(
    tmp_path,
):
    """Near-miss proof: real, unrelated definitions merely sharing one
    allowed file — the ``AccessRequirement`` enum's own declaration, and,
    many lines away, an unrelated ``ConnectionType``/``pppoe`` enum that
    never appears near a guarded-token line — are not flagged. Distinguishes
    a genuine same-block fallback from two unrelated concepts that happen to
    live in the same large file."""

    (tmp_path / "app" / "models").mkdir(parents=True)
    planted = tmp_path / "app" / "models" / "catalog.py"
    filler = "\n".join(f"# unrelated filler line {i}" for i in range(20))
    planted.write_text(
        "class AccessRequirement(enum.Enum):\n"
        "    network_access = 'network_access'\n"
        "    no_network_access = 'no_network_access'\n"
        "    unclassified = 'unclassified'\n"
        f"\n{filler}\n\n"
        "class ConnectionType(enum.Enum):\n"
        "    pppoe = 'pppoe'\n"
    )

    leaks = _find_cross_domain_leaks_within_allowed_files(
        tmp_path,
        guarded=_GUARDED_TOKENS,
        cross_domain=_CROSS_DOMAIN_TOKENS,
        allowed=("app/models/catalog.py",),
    )
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
