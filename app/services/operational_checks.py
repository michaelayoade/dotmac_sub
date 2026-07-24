"""Question-driven operational evidence for NOC and admin surfaces.

This service deliberately does not collapse administrative expectation,
observed result, evidence age, customer impact, and retry into one status word.
Collectors and runtimes write facts; this read owner explains those facts and
the next machine/operator action. Templates only arrange the projection.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.billing import (
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventSource,
    PaymentProviderType,
)
from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationInstallation,
    IntegrationInstallationState,
    IntegrationValidationStatus,
)
from app.models.scheduler import ScheduledTask
from app.services import job_heartbeat
from app.services.integrations import registry as integration_registry
from app.services.integrations.connectors.dotmac_crm import (
    CRM_OPERATIONAL_OBSERVATION_CAPABILITY,
)
from app.services.payment_reconciliation import topup_reconciliation_backlog

TR069_SYNC_TASK = "app.tasks.tr069.sync_all_acs_devices"
CRM_OPERATION_OBSERVATION = "integration.crm.capability.crm.operational_observation.v1"
_CRM_CONNECTOR_KEY = "dotmac.crm"
_PAYSTACK_CONNECTOR_KEY = "paystack"
_PAYSTACK_CAPABILITIES = frozenset({"payments.webhook.v1", "payments.reconcile.v1"})
PAYSTACK_WEBHOOK_PATH = "/api/v1/payment-events/paystack"


@dataclass(frozen=True, slots=True)
class OperationalCheck:
    key: str
    subject: str
    expected: str
    last_result: str
    observed_at: datetime | None
    evidence: str
    impact: str
    next_step: str
    next_attempt_at: datetime | None
    action_url: str
    needs_attention: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _task_row(db: Session, task_name: str) -> ScheduledTask | None:
    return (
        db.query(ScheduledTask)
        .filter(ScheduledTask.task_name == task_name)
        .order_by(ScheduledTask.updated_at.desc())
        .first()
    )


def _task_result(task_name: str) -> tuple[dict[str, Any], datetime | None]:
    result = job_heartbeat.get_last_result(task_name)
    result = result if isinstance(result, dict) else {}
    return result, _parse_datetime(result.get("at"))


def bandwidth_poller_snapshot() -> dict[str, Any] | None:
    from app.services.poller_health import load_poller_health

    return load_poller_health()


def bandwidth_device_failures(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return []
    rows = snapshot.get("device_failures")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _bandwidth_check(
    snapshot: dict[str, Any] | None, *, now: datetime
) -> OperationalCheck:
    if not snapshot:
        return OperationalCheck(
            key="bandwidth_poller",
            subject="Live bandwidth collector",
            expected="A cycle should arrive every few seconds while the collector is enabled.",
            last_result="No collector cycle is available.",
            observed_at=None,
            evidence="The web process cannot verify the collector or its Redis projection.",
            impact="Live bandwidth graphs may be missing; this does not prove customer service is down.",
            next_step="Check the bandwidth-poller process and its Redis connection.",
            next_attempt_at=None,
            action_url="/admin/network/monitoring",
            needs_attention=True,
        )
    observed_at = _parse_datetime(snapshot.get("ts"))
    age = (now - observed_at).total_seconds() if observed_at else None
    failures = int(snapshot.get("devices_failing") or 0)
    total = int(snapshot.get("devices_total") or 0)
    too_old = age is None or age > 60
    if too_old:
        last_result = "The last collector cycle is older than one minute."
        next_step = "Check the collector process before interpreting router results."
    elif failures:
        last_result = f"Latest cycle completed; {failures} of {total} routers could not be polled."
        next_step = (
            "Open the router rows below; each shows the failed attempt and next retry."
        )
    else:
        last_result = f"Latest cycle completed for all {total} configured routers."
        next_step = "No collector action is required."
    return OperationalCheck(
        key="bandwidth_poller",
        subject="Live bandwidth collector",
        expected="A cycle should arrive every few seconds while the collector is enabled.",
        last_result=last_result,
        observed_at=observed_at,
        evidence=f"Cycle #{int(snapshot.get('poll_count') or 0)}; duration {float(snapshot.get('cycle_seconds') or 0):.3f}s.",
        impact=(
            "Live bandwidth samples are unavailable for the listed routers; "
            "reachability of customer service must be checked separately."
            if failures or too_old
            else "Live bandwidth sampling is current."
        ),
        next_step=next_step,
        next_attempt_at=None,
        action_url="/admin/network/monitoring",
        needs_attention=bool(failures or too_old),
    )


def _tr069_check(db: Session, *, now: datetime) -> OperationalCheck:
    schedule = _task_row(db, TR069_SYNC_TASK)
    enabled = bool(schedule and schedule.enabled)
    interval = int(schedule.interval_seconds or 0) if schedule else 0
    result, observed_at = _task_result(TR069_SYNC_TASK)
    last_success = job_heartbeat.get_last_success(TR069_SYNC_TASK)
    if last_success is not None and last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=UTC)
    status = str(result.get("status") or "")
    detail_value = result.get("detail")
    detail: dict[str, Any] = (
        dict(detail_value) if isinstance(detail_value, dict) else {}
    )
    synced = int(detail.get("servers_synced") or 0)
    total = int(detail.get("servers_total") or 0)
    errors = int(detail.get("errors") or 0)
    next_attempt_at = _parse_datetime(result.get("next_attempt_at"))
    if not result:
        last_result = "No completed or failed run has been recorded."
    elif status in {"ok", "success"}:
        last_result = f"Completed for {synced} of {total} active ACS servers."
    elif status == "partial":
        last_result = f"Completed for {synced} of {total} ACS servers; {errors} failed."
    elif status == "timeout":
        last_result = (
            f"Stopped at the task time limit after {synced} of {total} ACS servers."
        )
    else:
        last_result = f"The last run failed after {synced} of {total} ACS servers."
    stale = bool(
        enabled
        and interval > 0
        and (
            last_success is None or (now - last_success).total_seconds() > interval * 2
        )
    )
    if next_attempt_at:
        next_step = "The task owner scheduled a bounded automatic retry."
    elif not enabled:
        next_step = "No retry is expected while the schedule is disabled."
    elif status in {"partial", "timeout", "error", "failed"}:
        next_step = (
            "Automatic retries are exhausted; investigate the ACS server or worker."
        )
    else:
        next_step = "The scheduler will run the next inventory pass."
    expected = (
        f"Enabled; expected every {interval // 60 or 1} minute(s)."
        if enabled
        else "Disabled; no inventory refresh is expected."
    )
    freshness = (
        f"Last successful inventory refresh: {last_success.isoformat()}."
        if last_success
        else "No successful inventory refresh has been recorded."
    )
    return OperationalCheck(
        key="tr069_inventory",
        subject="TR-069 inventory refresh",
        expected=expected,
        last_result=last_result,
        observed_at=observed_at,
        evidence=f"{freshness} Consecutive failed runs: {int(result.get('consecutive_failures') or 0)}.",
        impact="ONT inventory and last-inform data may lag; this does not prove an ONT is offline.",
        next_step=next_step,
        next_attempt_at=next_attempt_at,
        action_url="/admin/network/tr069",
        needs_attention=bool(
            enabled and (stale or status not in {"", "ok", "success"})
        ),
    )


def _crm_binding_evidence(db: Session) -> tuple[str, bool]:
    bindings = (
        db.query(IntegrationCapabilityBinding)
        .join(IntegrationInstallation)
        .filter(
            IntegrationCapabilityBinding.capability_id
            == CRM_OPERATIONAL_OBSERVATION_CAPABILITY,
            IntegrationInstallation.connector_key == _CRM_CONNECTOR_KEY,
        )
        .order_by(IntegrationCapabilityBinding.created_at.asc())
        .all()
    )
    effective = [
        binding
        for binding in bindings
        if binding.state == IntegrationBindingState.enabled.value
        and binding.installation.state == IntegrationInstallationState.enabled.value
    ]
    if not bindings:
        return "No CRM operational-observation binding exists.", False
    if not effective:
        return "A binding exists, but it or its installation is disabled.", False
    defaults = [
        binding
        for binding in effective
        if (binding.policy_json or {}).get("default") is True
    ]
    if len(effective) > 1 and len(defaults) != 1:
        return (
            "Multiple enabled bindings exist without exactly one declared default.",
            False,
        )
    selected = defaults[0] if defaults else effective[0]
    installation = selected.installation
    revision = installation.current_config_revision
    if revision is None:
        return "The selected installation has no current configuration revision.", False
    if revision.validation_status != IntegrationValidationStatus.valid.value:
        return "The selected configuration revision has not passed validation.", False
    definition = integration_registry.require_connector_definition(_CRM_CONNECTOR_KEY)
    if (
        definition.version != installation.connector_version
        or definition.digest != installation.manifest_digest
    ):
        return (
            "The selected installation pin differs from the deployed connector.",
            False,
        )
    if definition.capability(CRM_OPERATIONAL_OBSERVATION_CAPABILITY) is None:
        return "The deployed CRM connector does not declare this capability.", False
    return (
        f"Binding {selected.id} is enabled with a validated pinned configuration.",
        True,
    )


def crm_operational_check(db: Session) -> OperationalCheck:
    from app.services.sales import selfserve as selfserve_service

    crm_required = not (
        selfserve_service.native_read_enabled(db)
        and selfserve_service.native_write_enabled(db)
    )
    binding_evidence, executable = _crm_binding_evidence(db)
    result, observed_at = _task_result(CRM_OPERATION_OBSERVATION)
    status = str(result.get("status") or "")
    next_attempt_at = _parse_datetime(result.get("next_attempt_at"))
    if not result:
        last_result = "No CRM capability call has been observed."
    elif status in {"ok", "success"}:
        last_result = "The last CRM operational-observation call completed."
    else:
        last_result = "The last CRM operational-observation call failed."
    if not crm_required:
        next_step = (
            "Native quote reads and writes are active; no CRM quote retry is required."
        )
    elif not executable:
        next_step = (
            "Repair or select the CRM capability binding before retrying portal reads."
        )
    elif status not in {"", "ok", "success"}:
        next_step = "The quote-mirror reconciler will retry; inspect its task evidence if failure continues."
    else:
        next_step = "No operator action is required; the mirror reconciler remains the backstop."
    return OperationalCheck(
        key="crm_operational_observation",
        subject="CRM portal observation capability",
        expected=(
            "Required while customer quote reads or writes still use the CRM mirror."
            if crm_required
            else "Not required for quotes after native read and write cutover."
        ),
        last_result=last_result,
        observed_at=observed_at,
        evidence=f"{binding_evidence} Consecutive failed calls: {int(result.get('consecutive_failures') or 0)}.",
        impact="When required and unavailable, portal quote data can be stale or unavailable; Sub customer and service state remain authoritative.",
        next_step=next_step,
        next_attempt_at=next_attempt_at,
        action_url="/admin/integrations/installed",
        needs_attention=bool(
            crm_required and (not executable or status not in {"", "ok", "success"})
        ),
    )


def _paystack_binding_evidence(db: Session) -> tuple[str, bool, bool]:
    installations = (
        db.query(IntegrationInstallation)
        .filter(IntegrationInstallation.connector_key == _PAYSTACK_CONNECTOR_KEY)
        .order_by(IntegrationInstallation.created_at.asc())
        .all()
    )
    if not installations:
        return "No Paystack installation exists.", False, False
    enabled_installations = [
        installation
        for installation in installations
        if installation.state == IntegrationInstallationState.enabled.value
    ]
    if len(enabled_installations) != 1:
        return (
            "Paystack does not have exactly one enabled installation.",
            False,
            True,
        )
    installation = enabled_installations[0]
    enabled_capabilities = {
        binding.capability_id
        for binding in installation.capability_bindings
        if binding.state == IntegrationBindingState.enabled.value
    }
    missing = sorted(_PAYSTACK_CAPABILITIES - enabled_capabilities)
    if missing:
        return (
            "Enabled Paystack installation is missing: " + ", ".join(missing) + ".",
            False,
            True,
        )
    revision = installation.current_config_revision
    if (
        revision is None
        or revision.validation_status != IntegrationValidationStatus.valid.value
    ):
        return (
            "Enabled Paystack installation has no validated current revision.",
            False,
            True,
        )
    definition = integration_registry.require_connector_definition(
        _PAYSTACK_CONNECTOR_KEY
    )
    if (
        definition.version != installation.connector_version
        or definition.digest != installation.manifest_digest
    ):
        return (
            "Enabled Paystack installation pin differs from the deployed connector.",
            False,
            True,
        )
    undeclared = sorted(
        capability_id
        for capability_id in _PAYSTACK_CAPABILITIES
        if definition.capability(capability_id) is None
    )
    if undeclared:
        return (
            "Deployed Paystack connector is missing: " + ", ".join(undeclared) + ".",
            False,
            True,
        )
    return (
        "Paystack webhook and reconciliation capabilities are enabled on a "
        "validated installation.",
        True,
        True,
    )


def _latest_paystack_webhook_at(db: Session) -> datetime | None:
    return (
        db.query(func.max(PaymentProviderEvent.received_at))
        .join(
            PaymentProvider,
            PaymentProvider.id == PaymentProviderEvent.provider_id,
        )
        .filter(
            PaymentProvider.provider_type == PaymentProviderType.paystack,
            PaymentProviderEvent.source == PaymentProviderEventSource.verified_webhook,
        )
        .scalar()
    )


def paystack_payment_check(
    db: Session,
    *,
    now: datetime | None = None,
) -> OperationalCheck:
    """Explain Paystack automatic-posting evidence without inventing health."""

    now = now or datetime.now(UTC)
    binding_evidence, executable, installed = _paystack_binding_evidence(db)
    schedule = _task_row(db, job_heartbeat.PAYMENT_RECONCILIATION_TASK)
    schedule_enabled = bool(schedule and schedule.enabled)
    interval = int(schedule.interval_seconds or 0) if schedule else 0
    result, result_at = _task_result(job_heartbeat.PAYMENT_RECONCILIATION_TASK)
    detail_value = result.get("detail")
    detail = dict(detail_value) if isinstance(detail_value, dict) else {}
    last_success = job_heartbeat.get_last_success(
        job_heartbeat.PAYMENT_RECONCILIATION_TASK
    )
    if last_success is not None and last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=UTC)
    runner_stale = bool(
        schedule_enabled
        and interval > 0
        and (
            last_success is None or (now - last_success).total_seconds() > interval * 2
        )
    )
    backlog = topup_reconciliation_backlog(db, observed_at=now)
    webhook_at = _latest_paystack_webhook_at(db)
    if webhook_at is not None and webhook_at.tzinfo is None:
        webhook_at = webhook_at.replace(tzinfo=UTC)
    errors = int(detail.get("errors") or 0)
    checked = int(detail.get("checked") or 0)
    recovered = int(detail.get("recovered") or 0)
    stale_backlog = backlog.eligible + backlog.outside_window
    webhook_missing_with_backlog = bool(
        installed and webhook_at is None and stale_backlog
    )
    needs_attention = bool(
        installed
        and (
            not executable
            or not schedule_enabled
            or runner_stale
            or errors
            or backlog.outside_window
            or webhook_missing_with_backlog
        )
    )

    if not installed:
        last_result = "Paystack is not installed; no automatic posting is expected."
    elif not result:
        last_result = "No reconciliation execution result has been recorded."
    else:
        last_result = (
            f"Reconciliation checked {checked}, recovered {recovered}, and "
            f"rejected {errors}; {stale_backlog} stale intent(s) remain."
        )
    webhook_evidence = (
        f"Last signature-verified Paystack webhook: {webhook_at.isoformat()}."
        if webhook_at
        else "No signature-verified Paystack webhook has been recorded."
    )
    runner_evidence = (
        f"Last successful reconciliation execution: {last_success.isoformat()}."
        if last_success
        else "No successful reconciliation execution has been recorded."
    )
    if not installed:
        next_step = "Install Paystack before configuring automatic posting."
    elif not executable:
        next_step = (
            "Repair the Paystack webhook and reconciliation capability bindings."
        )
    elif not schedule_enabled or runner_stale:
        next_step = "Repair the reconciliation schedule or billing worker before taking payments."
    elif webhook_missing_with_backlog:
        next_step = (
            f"Set the Paystack live webhook URL to POST {PAYSTACK_WEBHOOK_PATH}, "
            "then confirm a signature-verified receipt appears."
        )
    elif errors:
        next_step = (
            "Inspect and repair the rejected intents; the runner itself is executing."
        )
    elif backlog.outside_window:
        next_step = "Reconcile intents outside the automatic retry window explicitly."
    else:
        next_step = "No operator action is required."
    observations = [value for value in (result_at, webhook_at) if value is not None]
    observed_at = max(observations) if observations else None
    expected = (
        f"Paystack posts through POST {PAYSTACK_WEBHOOK_PATH}; "
        f"reconciliation runs every {interval // 60 or 1} minute(s)."
        if installed and schedule_enabled
        else f"Paystack posts through POST {PAYSTACK_WEBHOOK_PATH}; reconciliation is not enabled."
    )
    return OperationalCheck(
        key="paystack_payment_automation",
        subject="Paystack automatic payment posting",
        expected=expected,
        last_result=last_result,
        observed_at=observed_at,
        evidence=f"{binding_evidence} {webhook_evidence} {runner_evidence}",
        impact=(
            "Stale successful charges can remain absent from customer accounts until "
            "a verified webhook or successful reconciliation commits settlement."
            if needs_attention
            else "No unresolved automatic-posting evidence requires attention."
        ),
        next_step=next_step,
        next_attempt_at=None,
        action_url="/admin/integrations/installed",
        needs_attention=needs_attention,
    )


def operational_checks(
    db: Session,
    *,
    now: datetime | None = None,
    poller_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, object]]:
    now = now or datetime.now(UTC)
    poller = (
        poller_snapshot if poller_snapshot is not None else bandwidth_poller_snapshot()
    )
    checks = [
        _bandwidth_check(poller, now=now),
        _tr069_check(db, now=now),
        crm_operational_check(db),
        paystack_payment_check(db, now=now),
    ]
    checks.sort(key=lambda item: (not item.needs_attention, item.subject.lower()))
    return [check.to_dict() for check in checks]
