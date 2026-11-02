"""Architecture guards for exact-service IPAM ownership migration."""

import ast
from pathlib import Path

from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
)
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/ip_assignment_lifecycle.py"
SCRIPT = ROOT / "scripts/one_off/repair_ipam_to_served.py"
LIFECYCLE_SCRIPT = ROOT / "scripts/one_off/repair_service_ipv4_assignment.py"
PROJECTION_SCRIPT = ROOT / "scripts/one_off/repair_service_ipv4_projection.py"
SESSION_DIVERGENCE_AUDIT = ROOT / "scripts/one_off/audit_nas_session_ip_divergence.py"
WEB_ADAPTER = ROOT / "app/services/web_catalog_subscriptions.py"
WEB_WORKFLOW = ROOT / "app/services/web_catalog_subscription_workflows.py"
ADMIN_ROUTE = ROOT / "app/web/admin/catalog.py"
SUBSCRIPTION_FORM = ROOT / "templates/admin/catalog/subscription_form.html"
SUBSCRIPTION_DETAIL = ROOT / "templates/admin/catalog/subscription_detail.html"
CUSTOMER_DETAIL_SERVICE = ROOT / "app/services/web_customer_details.py"
CUSTOMER_DETAIL = ROOT / "templates/admin/customers/detail.html"
PROJECTION_ACTION = (
    ROOT / "templates/admin/catalog/_ipv4_projection_reconciliation.html"
)

# The complete set of modules allowed to construct an IPAssignment while the
# lifecycle owner is in its SHADOWING phase. Everything other than the owner is
# declared migration debt in docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md, to be
# migrated behind the owner command at the runtime cutover.
#
# This list may SHRINK without discussion. Growing it means a fifth parallel
# writer to the address-allocation authority, which is the exact drift the
# shadowing phase exists to end -- so a new entry needs an architecture
# decision recording the alternative owner and how drift is prevented, not a
# green diff.
_IP_ASSIGNMENT_WRITERS = {
    "app/services/ip_assignment_lifecycle.py",  # the owner
    "app/services/network/ip.py",  # debt: provisioning allocation/reactivation
    "app/services/network/subscriber_wan_ipam.py",  # debt: ONT WAN claims
    "app/services/web_network_ip.py",  # debt: admin assignment + bulk import
}

# Modules that retire an allocation (`assignment.is_active = False`). This set
# is LARGER than the constructor set and only partly overlaps it: three modules
# retire an address without ever creating one. Pinning constructors alone would
# have left retirement -- reclaiming a live customer address -- unguarded.
_IP_ASSIGNMENT_DEACTIVATORS = {
    "app/services/connectivity_backup.py",  # debt: snapshot restore (assign.is_active)
    "app/services/ip_assignment_lifecycle.py",  # the owner
    "app/services/ip_lifecycle.py",  # debt: terminal lifecycle release
    "app/services/network/ip.py",  # debt: provisioning allocation/reactivation
    "app/services/network/ont_inventory.py",  # debt: ONT inventory WAN release
    "app/services/network/subscriber_wan_ipam.py",  # debt: ONT WAN claims
    "app/services/provisioning_helpers.py",  # debt: provisioning reclaim
    "app/services/web_system_restore_tool.py",  # debt: restore tooling
}


def test_ipam_ownership_owner_has_complete_transaction_contract() -> None:
    service = service_relationship("network.ip_assignment_lifecycle")

    assert service.module == "app.services.ip_assignment_lifecycle"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.SHADOWING
    concerns = {concern.name: concern for concern in service.contract.concerns}
    assert set(concerns) == {
        "exact service ownership of active IPv4 assignments",
        "reviewed exact-service IPv4 assignment lifecycle repair",
        "reviewed exact-service IPv4 served projection repair",
        "subscriber hard-delete IPv4 and IPv6 assignment release",
    }
    assert (
        concerns["exact service ownership of active IPv4 assignments"].role
        is OwnerRole.RECONCILER
    )
    for name in (
        "reviewed exact-service IPv4 assignment lifecycle repair",
        "reviewed exact-service IPv4 served projection repair",
        "subscriber hard-delete IPv4 and IPv6 assignment release",
    ):
        assert concerns[name].role is OwnerRole.COMMAND_WRITER
        assert concerns[name].canonical_writer == "network.ip_assignment_lifecycle"
    deletion = concerns["subscriber hard-delete IPv4 and IPv6 assignment release"]
    assert deletion.input_names == (
        "canonical subscriber IP assignments",
        "exact customer hard-delete command",
    )
    inputs = {value.name: value for value in service.contract.authoritative_inputs}
    assert (
        inputs["exact customer hard-delete command"].owner
        == "customer.account_deletion"
    )
    assert "FOR UPDATE NOWAIT" in service.contract.transaction.locking
    assert (
        "network.ip_assignment_lifecycle.invalid_customer_deletion_participant"
        in service.contract.errors.domain_codes
    )


def test_legacy_projection_authority_and_partial_commit_paths_are_retired() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert "backfill_create" not in source
    assert "reclaim_stale" not in source
    assert "dedupe_active" not in source
    assert "db.commit()" not in source
    assert "db.rollback()" not in source
    assert "execute_owner_command(" in source
    assert source.count("subscription.ipv4_address =") == 1


def test_operator_adapter_is_dry_run_first_and_fingerprint_gated() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"--apply"' in source
    assert '"--fingerprint"' in source
    assert '"--idempotency-key"' in source
    assert "reconcile_ip_assignment_service_ownership(" in source
    assert ".commit(" not in source


def test_lifecycle_operator_adapter_is_dry_run_first_and_fingerprint_gated() -> None:
    source = LIFECYCLE_SCRIPT.read_text(encoding="utf-8")

    assert '"--apply"' in source
    assert '"--fingerprint"' in source
    assert '"--idempotency-key"' in source
    assert '"--desired-address-id"' in source
    assert '"--deactivate-assignment-id"' in source
    assert "preview_service_ipv4_assignment_repair(" in source
    assert "repair_service_ipv4_assignment(" in source
    assert ".commit(" not in source


def test_unbound_release_adapter_is_dry_run_first_and_names_its_class() -> None:
    source = (ROOT / "scripts/one_off/release_unbound_ip_assignment.py").read_text(
        encoding="utf-8"
    )

    assert '"--apply"' in source
    assert '"--fingerprint"' in source
    assert '"--idempotency-key"' in source
    assert '"--eligibility-class"' in source
    assert "preview_unbound_ip_assignment_release(" in source
    assert "release_unbound_ip_assignment(" in source
    assert ".commit(" not in source


def test_historic_clear_adapter_is_dry_run_first_and_fingerprint_gated() -> None:
    source = (ROOT / "scripts/one_off/clear_historic_served_ipv4.py").read_text(
        encoding="utf-8"
    )

    assert '"--apply"' in source
    assert '"--fingerprint"' in source
    assert '"--idempotency-key"' in source
    assert '"--restore"' in source
    assert "preview_historic_served_ipv4_clear(" in source
    assert "clear_historic_served_ipv4(" in source
    assert "restore_cleared_served_ipv4(" in source
    assert ".commit(" not in source
    assert "emit_event" not in source


def test_projection_operator_adapter_is_dry_run_first_and_fingerprint_gated() -> None:
    source = PROJECTION_SCRIPT.read_text(encoding="utf-8")

    assert '"--apply"' in source
    assert '"--fingerprint"' in source
    assert '"--idempotency-key"' in source
    assert '"--assignment-id"' in source
    assert "preview_service_ipv4_projection_repair(" in source
    assert "repair_service_ipv4_projection(" in source
    assert "outcome.event_id" in source
    assert "wait_for_event_terminal(" in source
    assert "EventTerminalDisposition.completed" in source
    assert "order_by(" not in source
    assert ".commit(" not in source


def test_projection_cutover_gate_cannot_inherit_session_or_output_scope() -> None:
    source = SESSION_DIVERGENCE_AUDIT.read_text(encoding="utf-8")

    assert "for sub_id in sorted(state):" in source
    assert "if len(infos) > 1:" in source
    assert '"session_gate_valid": session_gate_valid' in source
    assert '"gate_population": "all ACTIVE/BLOCKED projected subscriptions"' in source
    assert "for sub_id in sorted(scoped_subscription_ids):" not in source


def test_admin_ipv4_replacement_is_owner_backed_and_billing_isolated() -> None:
    adapter_tree = ast.parse(WEB_ADAPTER.read_text(encoding="utf-8"))
    replacement = next(
        node
        for node in adapter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "replace_subscription_ipv4_with_owner"
    )
    replacement_source = ast.unparse(replacement)
    assert "preview_service_ipv4_assignment_repair(" in replacement_source
    assert "preview_service_ipv4_projection_repair(" in replacement_source
    # One owner transaction (assignment + projection participants) so a
    # projection refusal cannot leave IPAM moved; no separate commits.
    assert "replace_service_ipv4(" in replacement_source
    assert "repair_service_ipv4_assignment(" not in replacement_source
    assert "repair_service_ipv4_projection(" not in replacement_source
    assert "sync_public_ip_addon_for_subscription(" not in replacement_source
    assert "update_subscription(" not in replacement_source

    generic_update = next(
        node
        for node in adapter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "update_subscription_with_audit"
    )
    assert "_sync_ipv4_assignments_for_subscription(" not in ast.unparse(generic_update)

    workflow_source = WEB_WORKFLOW.read_text(encoding="utf-8")
    route_source = ADMIN_ROUTE.read_text(encoding="utf-8")
    template_source = SUBSCRIPTION_FORM.read_text(encoding="utf-8")
    assert "handle_subscription_ipv4_replacement(" in workflow_source
    assert '"/subscriptions/{subscription_id}/ipv4/replace"' in route_source
    assert "/ipv4/replace" in template_source
    assert "It does not purchase or change an IP add-on" in template_source


def test_subscription_pages_share_exact_service_ipv4_projection() -> None:
    adapter_tree = ast.parse(WEB_ADAPTER.read_text(encoding="utf-8"))
    form_rows = next(
        node
        for node in adapter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_subscription_ipv4_form_rows"
    )
    form_rows_source = ast.unparse(form_rows)
    form_source = SUBSCRIPTION_FORM.read_text(encoding="utf-8")
    detail_source = SUBSCRIPTION_DETAIL.read_text(encoding="utf-8")
    customer_service_source = CUSTOMER_DETAIL_SERVICE.read_text(encoding="utf-8")
    customer_detail_source = CUSTOMER_DETAIL.read_text(encoding="utf-8")

    assert "resolve_subscription_service_ipv4(" in form_rows_source
    assert "IPAssignment.subscriber_id" not in form_rows_source
    assert "IPAssignment.created_at.asc()" not in form_rows_source
    assert "initialAddresses.slice(0, 1)" not in form_source
    assert "service_ipv4.address" in detail_source
    assert "assigned_ipv4_list" not in detail_source
    assert "resolve_subscription_service_ipv4_batch(" in customer_service_source
    assert "service_ipv4.address" in customer_detail_source
    assert "subscription.ipv4_address" not in customer_detail_source


def test_admin_projection_reconciliation_is_a_thin_confirmed_owner_adapter() -> None:
    workflow_tree = ast.parse(WEB_WORKFLOW.read_text(encoding="utf-8"))
    execute = next(
        node
        for node in workflow_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "execute_subscription_ipv4_projection_reconciliation"
    )
    execute_source = ast.unparse(execute)
    assert "RepairServiceIPv4ProjectionCommand(" in execute_source
    assert "repair_service_ipv4_projection(" in execute_source
    for forbidden in (".commit(", ".rollback(", ".flush(", ".add(", ".delete("):
        assert forbidden not in execute_source

    route_source = ADMIN_ROUTE.read_text(encoding="utf-8")
    detail_source = SUBSCRIPTION_DETAIL.read_text(encoding="utf-8")
    action_source = PROJECTION_ACTION.read_text(encoding="utf-8")
    assert '"/subscriptions/{subscription_id}/ipv4/reconcile"' in route_source
    assert 'confirmed != "yes"' in route_source
    assert "_ipv4_projection_reconciliation.html" in detail_source
    assert "action_form(ipv4_projection_reconciliation_action)" in action_source
    assert "preview_service_ipv4_projection_repair(" not in detail_source


def test_ip_assignment_writers_are_pinned_during_shadowing() -> None:
    """The declared migration debt must not grow while the owner shadows.

    The design doc says the legacy writers "must not be treated as precedent or
    expanded". Prose does not stop a fifth writer landing in a green diff, so
    pin the set the same way the prepaid owners are pinned (see
    test_prepaid_timer_fields_have_one_canonical_writer). Deleting an entry as
    a caller migrates behind the owner command is the expected direction.
    """
    writers: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "app/models/network.py":
            continue  # the model's own definition, not a writer
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "IPAssignment"
            ):
                writers.add(relative)

    assert writers == _IP_ASSIGNMENT_WRITERS


def _is_assignment_like(node: ast.AST) -> bool:
    """`assignment`, `assign`, `record.assignment`: anything named like one.

    The earlier exact-word match missed ``assign.is_active = ...`` in
    ``connectivity_backup`` (snapshot restore).
    """
    if isinstance(node, ast.Name):
        return "assign" in node.id.lower()
    if isinstance(node, ast.Attribute):
        return "assign" in node.attr.lower()
    return False


def test_ip_assignment_deactivators_are_pinned_during_shadowing() -> None:
    """`is_active = False` retires an allocation, so pin it separately.

    Creation and retirement are different consequences with different blast
    radii: a parallel deactivator silently reclaims a live customer address
    without entering the fingerprint-gated command. Six modules retire an
    allocation against four that create one, so pinning constructors alone
    would have left the larger surface unguarded.
    """
    deactivators: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "app/models/network.py":
            continue
        source = path.read_text(encoding="utf-8")
        # Name-based AST matching cannot tell an IPAssignment from a team-inbox
        # assignment, so require the module to reference the IPAM models at all.
        # ``IPv4Address`` counts too: ``ont_inventory`` retires allocations via
        # ``IPv4Address.assignment`` without ever naming ``IPAssignment``.
        if "IPAssignment" not in source and "IPv4Address" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # setattr(assignment, "is_active", ...) / {"is_active": ...}
                # dict-driven retirement of an allocation.
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "setattr"
                    and len(node.args) >= 2
                    and _is_assignment_like(node.args[0])
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "is_active"
                ):
                    deactivators.add(relative)
                continue
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "is_active"
                    and _is_assignment_like(target.value)
                ):
                    deactivators.add(relative)

    assert deactivators == _IP_ASSIGNMENT_DEACTIVATORS
