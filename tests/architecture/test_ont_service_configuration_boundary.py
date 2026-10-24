"""Architecture guards for lifecycle-safe ONT Configure delivery."""

import re
from pathlib import Path

from tests.architecture import ont_direct_writer_scan

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_configure_route_is_only_a_typed_transport_adapter():
    source = _source("app/web/admin/network_onts.py")
    configure_slice = source.split("def ont_configure_submit(", 1)[1].split(
        "def ont_configure_retry(", 1
    )[0]

    assert "ConfigureOntServiceCommand(" in configure_slice
    assert "configure_ont_service(" in configure_slice
    assert "update_ont_config(" not in configure_slice
    assert "reconcile_ont(" not in configure_slice
    assert "create_genieacs_client" not in configure_slice
    assert ".delay(" not in configure_slice
    assert "send_task(" not in configure_slice
    assert "pppoe_password: str = Form" not in configure_slice
    assert "pppoe_username: str = Form" not in configure_slice


def test_worker_claims_dispatch_and_never_creates_an_operation():
    source = _source("app/tasks/ont_service_configuration.py")

    assert "managed_network_operation_dispatch" in source
    assert "execute_ont_service_configuration(" in source
    assert "network_operations.start" not in source
    assert "NetworkOperation(" not in source


def test_section_delivery_scopes_cross_owner_to_reconciler_without_secret_values():
    owner_source = _source("app/services/network/ont_service_configuration.py")
    state_source = _source("app/services/network/reconcile/state.py")

    assert "OntWifiDeliveryScope" in state_source
    assert "OntWanProposedChange" in state_source
    assert "changed_fields: frozenset[OntWifiDeliveryField]" in state_source
    assert "wifi_scope = _wifi_delivery_scope(revision)" in owner_source
    assert "wan_scope = _wan_delivery_scope(db, ont, revision)" in owner_source
    assert "wifi_delivery_scope=wifi_scope," in owner_source
    assert "proposed_change=wan_scope," in owner_source
    scope_slice = owner_source.split("def _wifi_delivery_scope(", 1)[1].split(
        "def _execution_locked(", 1
    )[0]
    assert "wifi.password" in scope_slice
    assert "wifi_password_ref" in scope_slice
    assert "encrypt_credential" not in scope_slice
    assert "decrypt" not in scope_slice


def test_verification_never_sets_sync_status_directly():
    """``out_of_sync`` has one canonical writer:
    ``app.services.network.ont_status.set_sync_status``, called from inside
    ``reconcile_ont``. The readback-verification path must call INTO
    ``reconcile_ont`` for that decision rather than assign
    ``OntUnit.sync_status``/``out_of_sync`` itself.
    """
    owner_source = _source("app/services/network/ont_service_configuration.py")
    verify_slice = owner_source.split("def _verify_locked(", 1)[1].split(
        "def verify_ont_service_configuration_readback(", 1
    )[0]

    assert "sync_status" not in verify_slice
    assert "out_of_sync" not in verify_slice
    assert "set_sync_status" not in verify_slice


def test_verification_dispatch_is_structurally_readback_only():
    """The dispatched ``ont_service_config_verify.v1`` invocation carries no
    field that could select a write path. It may carry only the bounded attempt
    count, while the worker task fixes ``force_readback_only=True`` and repair
    mode unconditionally — not from any operation payload argument.
    """
    dispatch_source = _source("app/services/network_operation_dispatch.py")
    task_source = _source("app/tasks/ont_service_configuration.py")

    verify_invocation_slice = dispatch_source.split(
        "def _ont_service_config_verify_invocation(", 1
    )[1].split("def _cpe_tr069_invocation(", 1)[0]
    assert "verification_attempt" in verify_invocation_slice
    assert "explicit_repair" not in verify_invocation_slice

    verify_task_slice = task_source.split("def verify_readback(", 1)[1]
    # The task's own signature carries no ``force_readback_only`` parameter
    # (checked against the args-block above the docstring) — every mention
    # of it below is a hardcoded ``=True``, never something a caller,
    # the dispatch payload, or the celery signature could override.
    task_signature = task_source.split("def verify_readback(", 1)[1].split(
        ") -> dict[str, Any]:", 1
    )[0]
    assert "force_readback_only" not in task_signature
    assert "verification_attempt" in task_signature
    assert "force_readback_only=True" in verify_task_slice
    assert "explicit_repair=True" in verify_task_slice
    non_docstring_slice = verify_task_slice.split('"""', 2)[-1]
    assert non_docstring_slice.count("force_readback_only") == 1


def test_pending_readback_never_reenters_the_apply_command():
    owner_source = _source("app/services/network/ont_service_configuration.py")
    execution_slice = owner_source.split("def _execution_locked(", 1)[1].split(
        "def execute_ont_service_configuration(", 1
    )[0]
    retry_slice = execution_slice.split("readback_pending = bool(", 1)[1]

    assert "NetworkOperationCommand.ont_service_config_apply_v1" not in retry_slice
    assert (
        retry_slice.count("NetworkOperationCommand.ont_service_config_verify_v1") == 2
    )
    assert 'mode="sweep" if readback_only or command.explicit_repair' in execution_slice


def test_template_uses_owner_projection_for_retry_and_hides_ppp_secret_inputs():
    source = _source("templates/admin/network/onts/_configure_form.html")

    assert "lifecycle.next_action.value" in source
    assert "Retry current configuration" in source
    assert 'name="pppoe_password"' not in source
    assert 'name="pppoe_username"' not in source
    assert "Historical attempts and legacy evidence" in source
    assert "latest_failure" not in source


def test_legacy_synchronous_configuration_writer_is_retired():
    package_source = _source("app/services/web_network_ont_actions/__init__.py")
    legacy_source = _source("app/services/web_network_ont_actions/db_config.py")

    assert "update_ont_config" not in package_source
    assert "def update_ont_config" not in legacy_source


# -- Single-writer ratchet (2026-10-19 decision) ------------------------------

_BASELINE = ROOT / "tests/architecture/ont_direct_writer_baseline.txt"


def _load_direct_writer_baseline() -> dict[tuple[str, str], int]:
    entries: dict[tuple[str, str], int] = {}
    for line in _BASELINE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        path, name, count = line.split()
        entries[(path, name)] = int(count)
    return entries


def test_web_and_api_modules_gain_no_direct_ont_olt_acs_writers():
    """The lifecycle is the only writer: the baseline may only shrink."""
    baseline = _load_direct_writer_baseline()
    current = ont_direct_writer_scan.scan()

    grown = sorted(
        f"{path}: {name} {baseline.get((path, name), 0)} -> {count}"
        for (path, name), count in current.items()
        if count > baseline.get((path, name), 0)
    )
    assert not grown, (
        "New direct ONT/OLT/ACS writer in a web/api module. Route it through "
        "configure_ont_service (the lifecycle) or make it break-glass:\n"
        + "\n".join(grown)
    )

    stale = sorted(
        f"{path}: {name} baseline {count} > actual {current.get((path, name), 0)}"
        for (path, name), count in baseline.items()
        if current.get((path, name), 0) < count
    )
    assert not stale, (
        "Direct-writer baseline is stale; lower it in "
        "tests/architecture/ont_direct_writer_baseline.txt:\n" + "\n".join(stale)
    )


def test_retired_direct_writers_stay_retired():
    retired = {
        "app/web/admin/network_onts.py": ("service_port_service_write",),
        "app/web/admin/network_onts_actions.py": ("reconcile_ont",),
        "app/web/admin/network_onts_inventory.py": ("ont_write",),
        "app/api/network_ont_ops.py": ("reconcile_ont",),
    }
    current = ont_direct_writer_scan.scan()
    for path, names in retired.items():
        for name in names:
            assert (path, name) not in current, (path, name)
    api = _source("app/api/network_ont_ops.py")
    assert not re.search(
        r"ont_write\.(update_management_ip|update_service_port|move_ont)", api
    )
    assert not (ROOT / "app/web/admin/network_ont_service_ports.py").exists()


def test_break_glass_routes_are_admin_only_reasoned_and_audited():
    actions = _source("app/web/admin/network_onts_actions.py")
    bind_slice = actions.split("def ont_bind_internet_wan(", 1)[0].rsplit(
        "@router.post(", 1
    )[1]
    assert "require_role(BREAK_GLASS_ROLE)" in bind_slice
    bind_body = actions.split("def ont_bind_internet_wan(", 1)[1].split(
        "@router.post(", 1
    )[0]
    assert bind_body.index("authorize_break_glass(") < bind_body.index(
        "bind_internet_wan("
    )

    tr069 = _source("app/web/admin/network_tr069.py")
    for route, call in (
        ("/tr069/devices/{device_id}/action", "queue_device_job("),
        ("/tr069/devices/{device_id}/config", "create_config_push_job("),
    ):
        decorator = tr069.split(f'"{route}"', 1)[1].split("def ", 1)[0]
        assert "require_role(BREAK_GLASS_ROLE)" in decorator, route
        body = tr069.split(f'"{route}"', 1)[1].split("@router.post", 1)[0]
        assert body.index("_break_glass_refusal(") < body.index(call), route


def test_wifi_and_management_controls_use_the_lifecycle_admission():
    actions = _source("app/web/admin/network_onts_actions.py")
    for name in ("ont_set_wifi_ssid", "ont_set_wifi_password"):
        body = actions.split(f"def {name}(", 1)[1].split("@router.post(", 1)[0]
        assert "submit_wifi_change(" in body
        assert "reconcile_ont" not in body
    forms = _source("app/services/network/ont_web_forms.py")
    mgmt = forms.split("def update_mgmt_ip_from_form(", 1)[1].split(
        "def mgmt_ip_modal_context(", 1
    )[0]
    assert "submit_management_change(" in mgmt
    assert "set_desired_config_values(" not in mgmt
    admission = _source("app/services/web_network_ont_actions/lifecycle_admission.py")
    assert "configure_ont_service(" in admission
    assert "reconcile_ont(" not in admission
