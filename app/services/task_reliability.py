"""Reliability contracts for Celery tasks.

Celery is the transport layer. This registry documents the retry and failure
handling contract for every first-party task so new tasks cannot be added
without an explicit reliability decision.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class RetryPolicy(StrEnum):
    """Primary retry strategy for a task."""

    BEAT_RERUN = "beat_rerun"
    CELERY_AUTORETRY = "celery_autoretry"
    DB_STATE_MACHINE = "db_state_machine"
    DEAD_LETTER_REDRIVE = "dead_letter_redrive"
    ITEM_LEVEL = "item_level"
    MANUAL_REDRIVE = "manual_redrive"
    NO_RETRY = "no_retry"


class Idempotency(StrEnum):
    """How safe a repeat execution is expected to be."""

    IDEMPOTENT = "idempotent"
    GUARDED = "guarded"
    STATE_MACHINE = "state_machine"
    PER_ITEM_GUARDED = "per_item_guarded"
    NON_IDEMPOTENT = "non_idempotent"


class FailureVisibility(StrEnum):
    """Where operators can see or recover failures."""

    LOG_ONLY = "log_only"
    HEALTH_HEARTBEAT = "health_heartbeat"
    DOMAIN_STATUS = "domain_status"
    DEAD_LETTER = "dead_letter"
    ADMIN_REDRIVE = "admin_redrive"


@dataclass(frozen=True)
class TaskReliabilityContract:
    domain: str
    retry_policy: RetryPolicy
    idempotency: Idempotency
    failure_visibility: FailureVisibility
    notes: str = ""


def _c(
    domain: str,
    retry_policy: RetryPolicy,
    idempotency: Idempotency,
    failure_visibility: FailureVisibility,
    notes: str = "",
) -> TaskReliabilityContract:
    return TaskReliabilityContract(
        domain=domain,
        retry_policy=retry_policy,
        idempotency=idempotency,
        failure_visibility=failure_visibility,
        notes=notes,
    )


SWEEP = RetryPolicy.BEAT_RERUN
AUTORETRY = RetryPolicy.CELERY_AUTORETRY
STATE = RetryPolicy.DB_STATE_MACHINE
DLQ = RetryPolicy.DEAD_LETTER_REDRIVE
ITEMS = RetryPolicy.ITEM_LEVEL
MANUAL = RetryPolicy.MANUAL_REDRIVE
NONE = RetryPolicy.NO_RETRY

IDEMP = Idempotency.IDEMPOTENT
GUARDED = Idempotency.GUARDED
STATEFUL = Idempotency.STATE_MACHINE
PER_ITEM = Idempotency.PER_ITEM_GUARDED
NON_IDEMP = Idempotency.NON_IDEMPOTENT

LOG = FailureVisibility.LOG_ONLY
HEALTH = FailureVisibility.HEALTH_HEARTBEAT
STATUS = FailureVisibility.DOMAIN_STATUS
DEAD = FailureVisibility.DEAD_LETTER
REDRIVE = FailureVisibility.ADMIN_REDRIVE


TASK_RELIABILITY_CONTRACTS: dict[str, TaskReliabilityContract] = {
    "app.tasks.admin_alerts.evaluate_infrastructure_alerts": _c(
        "monitoring", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.alert_evaluation.evaluate_alert_rules": _c(
        "monitoring", SWEEP, IDEMP, STATUS
    ),
    "app.tasks.app_cache.refresh_dashboard_stats_cache": _c(
        "cache", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.app_cache.refresh_ont_zabbix_snapshot_cache": _c(
        "cache", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.arrangements.check_overdue_arrangements": _c(
        "billing", SWEEP, GUARDED, HEALTH
    ),
    "app.tasks.autopay.charge_due_invoices": _c(
        "billing", STATE, GUARDED, STATUS, "Money-moving; retry through payment state."
    ),
    "app.tasks.bandwidth.aggregate_to_metrics": _c("bandwidth", SWEEP, IDEMP, HEALTH),
    "app.tasks.bandwidth.cleanup_hot_data": _c("bandwidth", SWEEP, IDEMP, LOG),
    "app.tasks.bandwidth.process_bandwidth_stream": _c(
        "bandwidth", SWEEP, PER_ITEM, HEALTH
    ),
    "app.tasks.bandwidth.trim_redis_stream": _c("bandwidth", SWEEP, IDEMP, LOG),
    "app.tasks.billing.audit_cutover_balance_invariant": _c(
        "billing", SWEEP, IDEMP, HEALTH, "Read-only drift audit; safe to re-run."
    ),
    "app.tasks.billing.audit_funded_inactive_exposure": _c(
        "billing",
        SWEEP,
        IDEMP,
        HEALTH,
        "Read-only funded inactive liability report; safe to re-run.",
    ),
    "app.tasks.billing.check_billing_switch": _c("billing", SWEEP, IDEMP, HEALTH),
    "app.tasks.billing.mark_invoices_overdue": _c("billing", SWEEP, IDEMP, HEALTH),
    "app.tasks.billing.run_billing_notifications": _c(
        "billing", STATE, GUARDED, STATUS
    ),
    "app.tasks.billing.run_invoice_cycle": _c(
        "billing", STATE, GUARDED, HEALTH, "Invoice creation must remain idempotent."
    ),
    "app.tasks.catalog.expire_subscriptions": _c("catalog", SWEEP, GUARDED, HEALTH),
    "app.tasks.catalog.send_expiry_reminders": _c("catalog", SWEEP, GUARDED, STATUS),
    "app.tasks.catalog.apply_due_subscription_changes": _c(
        "catalog",
        SWEEP,
        GUARDED,
        HEALTH,
        "Applying a scheduled plan change must be idempotent (apply() status guard).",
    ),
    "app.tasks.collections.prepaid_balance_sweep": _c(
        "collections",
        SWEEP,
        GUARDED,
        HEALTH,
        "Daily balance sweep; per-account commit, idempotent arm/warn/suspend.",
    ),
    "app.tasks.collections.run_billing_enforcement": _c(
        "collections", STATE, GUARDED, HEALTH
    ),
    "app.tasks.collections.run_dunning": _c("collections", STATE, GUARDED, HEALTH),
    "app.tasks.collections.run_bundle_reconcile": _c(
        "collections", STATE, GUARDED, HEALTH
    ),
    "app.tasks.crm_billing_push.push_crm_billing_snapshots": _c(
        "crm", DLQ, PER_ITEM, DEAD
    ),
    "app.tasks.crm_sync.push_subscriber_change": _c("crm", AUTORETRY, GUARDED, DEAD),
    "app.tasks.crm_sync.redrive_crm_dead_letters": _c("crm", DLQ, IDEMP, DEAD),
    "app.tasks.crm_ticket_pull.pull_crm_tickets": _c("crm", SWEEP, IDEMP, HEALTH),
    "app.tasks.crm_ticket_pull.sync_crm_ticket": _c("crm", SWEEP, IDEMP, STATUS),
    "app.tasks.crm_ticket_push.push_comment_to_crm": _c(
        "crm", AUTORETRY, GUARDED, STATUS
    ),
    "app.tasks.crm_ticket_push.push_ticket_to_crm": _c(
        "crm", AUTORETRY, GUARDED, STATUS
    ),
    "app.tasks.enforcement.cleanup_subscription_block_sessions": _c(
        "enforcement", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.events.cleanup_old_events": _c("events", SWEEP, IDEMP, LOG),
    "app.tasks.events.mark_stale_processing_events": _c("events", SWEEP, IDEMP, STATUS),
    "app.tasks.events.retry_failed_events": _c("events", STATE, STATEFUL, STATUS),
    "app.tasks.exports.run_export_job": _c("exports", MANUAL, GUARDED, STATUS),
    "app.tasks.exports.run_scheduled_export": _c("exports", SWEEP, GUARDED, STATUS),
    "app.tasks.gis.run_batch_geocode_job": _c("gis", ITEMS, PER_ITEM, STATUS),
    "app.tasks.gis.sync_gis_sources": _c("gis", SWEEP, IDEMP, HEALTH),
    "app.tasks.imports.process_import_run": _c("imports", ITEMS, PER_ITEM, STATUS),
    "app.tasks.imports.run_import_job": _c("imports", ITEMS, PER_ITEM, STATUS),
    "app.tasks.infrastructure_availability.prune_infrastructure_availability": _c(
        "monitoring", SWEEP, IDEMP, LOG
    ),
    "app.tasks.infrastructure_availability.snapshot_infrastructure_availability": _c(
        "monitoring", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.integrations.run_integration_job": _c(
        "integrations", STATE, GUARDED, STATUS
    ),
    "app.tasks.invoice_pdf.generate_invoice_pdf_export": _c(
        "billing", MANUAL, IDEMP, STATUS
    ),
    "app.tasks.ip_utilization.prune_ip_pool_utilization_snapshots": _c(
        "ipam", SWEEP, IDEMP, LOG
    ),
    "app.tasks.ip_utilization.snapshot_ip_pool_utilization": _c(
        "ipam", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.monitoring_cleanup.check_stale_infrastructure": _c(
        "monitoring", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.monitoring_cleanup.cleanup_old_device_metrics": _c(
        "monitoring", SWEEP, IDEMP, LOG
    ),
    "app.tasks.monitoring_cleanup.sync_inventory_to_monitoring": _c(
        "monitoring", SWEEP, IDEMP, STATUS
    ),
    "app.tasks.monitoring_cleanup.sync_nas_to_monitoring": _c(
        "monitoring", SWEEP, IDEMP, STATUS
    ),
    "app.tasks.monitoring_coverage.refresh_monitoring_coverage": _c(
        "monitoring", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.monitoring_warm.warm_monitoring_caches": _c(
        "monitoring", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.mrr.snapshot_mrr": _c("billing", SWEEP, IDEMP, HEALTH),
    "app.tasks.nas.check_nas_health": _c("network", SWEEP, IDEMP, HEALTH),
    "app.tasks.nas.cleanup_nas_backups": _c("network", SWEEP, IDEMP, LOG),
    "app.tasks.nas.run_scheduled_backups": _c("network", STATE, GUARDED, STATUS),
    "app.tasks.nas.update_subscriber_counts": _c("network", SWEEP, IDEMP, HEALTH),
    "app.tasks.network_operations.cleanup_old_operations": _c(
        "network", SWEEP, IDEMP, LOG
    ),
    "app.tasks.nin_tasks.verify_nin_task": _c("identity", AUTORETRY, GUARDED, STATUS),
    "app.tasks.notifications.deliver_notification_queue": _c(
        "notifications", STATE, GUARDED, STATUS
    ),
    "app.tasks.oauth.check_token_health": _c("integrations", SWEEP, IDEMP, HEALTH),
    "app.tasks.oauth.refresh_expiring_tokens": _c(
        "integrations", STATE, GUARDED, STATUS
    ),
    "app.tasks.olt_config_backup.backup_all_olts": _c("network", SWEEP, IDEMP, STATUS),
    "app.tasks.olt_health_retry.retry_failed_olt_connections": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.olt_health_retry.retry_single_olt": _c(
        "network", AUTORETRY, IDEMP, STATUS
    ),
    "app.tasks.olt_mac_harvest.run_olt_mac_harvest": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.ont_bulk.execute_bulk_action": _c("network", ITEMS, PER_ITEM, STATUS),
    "app.tasks.ont_provisioning.authorize_ont": _c(
        "provisioning", STATE, STATEFUL, STATUS
    ),
    "app.tasks.ont_provisioning.provision_ont": _c(
        "provisioning", STATE, STATEFUL, STATUS
    ),
    "app.tasks.ont_provisioning.queue_bulk_provisioning": _c(
        "provisioning", ITEMS, PER_ITEM, STATUS
    ),
    "app.tasks.payment_reconciliation.reconcile_topups": _c(
        "billing", STATE, GUARDED, HEALTH
    ),
    "app.tasks.profile_sync.execute_due_profile_sync_tasks": _c(
        "network", STATE, STATEFUL, STATUS
    ),
    "app.tasks.projects.reconcile_project_mirror": _c("crm", SWEEP, IDEMP, HEALTH),
    "app.tasks.projects.refresh_project_mirror_for_subscriber": _c(
        "crm",
        NONE,
        IDEMP,
        LOG,
        "Best-effort on-view refresh; periodic reconcile backs it.",
    ),
    "app.tasks.provisioning.reap_stale_provisioning_runs": _c(
        "provisioning", SWEEP, IDEMP, STATUS
    ),
    "app.tasks.provisioning.retry_pending_compensation_failures": _c(
        "provisioning", STATE, STATEFUL, REDRIVE
    ),
    "app.tasks.provisioning.run_bulk_activation_job": _c(
        "provisioning", ITEMS, PER_ITEM, STATUS
    ),
    "app.tasks.provisioning.run_service_migration_job": _c(
        "provisioning", ITEMS, PER_ITEM, STATUS
    ),
    "app.tasks.quotes.reconcile_quote_mirror": _c("crm", SWEEP, IDEMP, HEALTH),
    "app.tasks.quotes.refresh_quote_mirror_for_subscriber": _c(
        "crm",
        NONE,
        IDEMP,
        LOG,
        "Best-effort on-view refresh; periodic reconcile backs it.",
    ),
    "app.tasks.radius.audit_ip_consistency": _c("radius", SWEEP, IDEMP, HEALTH),
    "app.tasks.radius.audit_suspension_enforcement": _c("radius", SWEEP, IDEMP, HEALTH),
    "app.tasks.radius.connectivity_shadow_audit": _c("radius", SWEEP, IDEMP, HEALTH),
    "app.tasks.radius.reap_radacct_ghosts": _c("radius", SWEEP, IDEMP, HEALTH),
    "app.tasks.radius.reconcile_active_sessions": _c("radius", SWEEP, IDEMP, HEALTH),
    "app.tasks.radius.run_enforcement_reconciler": _c("radius", STATE, GUARDED, STATUS),
    "app.tasks.radius.run_radius_sync_job": _c("radius", SWEEP, IDEMP, STATUS),
    "app.tasks.radius_population.refresh_radius_from_subs": _c(
        "radius", SWEEP, IDEMP, STATUS
    ),
    "app.tasks.radius_population.sync_device_login": _c("radius", SWEEP, IDEMP, STATUS),
    "app.tasks.referrals.reconcile_referral_mirror": _c(
        "billing", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.referrals.refresh_referral_mirror_for_subscriber": _c(
        "crm",
        NONE,
        IDEMP,
        LOG,
        "Best-effort on-view refresh; periodic reconcile backs it.",
    ),
    "app.tasks.topology_lldp.run_lldp_topology_poll": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.topology_metrics.export_topology_metrics": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.topology_sync.run_topology_reconcile": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.topology_sync.warm_topology_status": _c("network", SWEEP, IDEMP, HEALTH),
    "app.tasks.topology_ufiber_link.run_ufiber_onu_link": _c(
        "network", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.topology_uisp.run_uisp_topology_sync": _c(
        "network",
        SWEEP,
        IDEMP,
        HEALTH,
        "Scheduled UISP relationship sync; advisory lock prevents overlap.",
    ),
    "app.tasks.unmatched_radio.run_unmatched_radio_review": _c(
        "network",
        SWEEP,
        IDEMP,
        HEALTH,
        "Scheduled unmatched-radio review; re-links radios whose MAC now "
        "matches a subscriber and refreshes the residual ops queue.",
    ),
    "app.tasks.tr069.apply_acs_config": _c("tr069", STATE, STATEFUL, STATUS),
    "app.tasks.tr069.apply_saved_ont_service_config": _c(
        "tr069", STATE, STATEFUL, STATUS
    ),
    "app.tasks.tr069.check_device_health": _c("tr069", SWEEP, IDEMP, HEALTH),
    "app.tasks.tr069.cleanup_stale_genieacs_tasks": _c("tr069", SWEEP, IDEMP, LOG),
    "app.tasks.tr069.cleanup_tr069_records": _c("tr069", SWEEP, IDEMP, LOG),
    "app.tasks.tr069.execute_bulk_action": _c("tr069", ITEMS, PER_ITEM, STATUS),
    "app.tasks.tr069.execute_pending_jobs": _c("tr069", STATE, STATEFUL, STATUS),
    "app.tasks.tr069.refresh_ont_runtime_data": _c("tr069", SWEEP, IDEMP, HEALTH),
    "app.tasks.tr069.refresh_single_ont_runtime": _c("tr069", MANUAL, IDEMP, STATUS),
    "app.tasks.tr069.scrape_genieacs_metrics": _c("tr069", SWEEP, IDEMP, HEALTH),
    "app.tasks.tr069.setup_genieacs": _c("tr069", MANUAL, GUARDED, STATUS),
    "app.tasks.tr069.sync_all_acs_devices": _c("tr069", SWEEP, IDEMP, HEALTH),
    "app.tasks.tr069.wait_for_ont_bootstrap": _c("tr069", STATE, STATEFUL, STATUS),
    "app.tasks.usage.evaluate_fup_rules": _c("usage", STATE, GUARDED, HEALTH),
    "app.tasks.usage.import_radius_accounting": _c("usage", SWEEP, PER_ITEM, HEALTH),
    "app.tasks.usage.lift_expired_fup_enforcement": _c("usage", SWEEP, GUARDED, HEALTH),
    "app.tasks.usage.meter_usage_into_quota": _c("usage", STATE, GUARDED, HEALTH),
    "app.tasks.usage.notify_expiring_data_bundles": _c("usage", STATE, GUARDED, STATUS),
    "app.tasks.usage.reap_stale_radius_sessions": _c("usage", SWEEP, IDEMP, HEALTH),
    "app.tasks.usage.run_usage_rating": _c("usage", STATE, GUARDED, HEALTH),
    "app.tasks.vacation_holds.resume_expired_holds": _c(
        "customer", SWEEP, GUARDED, HEALTH
    ),
    "app.tasks.vas.run_vas_requery": _c("vas", STATE, GUARDED, STATUS),
    "app.tasks.vas.run_vas_review_requery": _c("vas", STATE, GUARDED, STATUS),
    "app.tasks.vas.run_wallet_auto_deduct": _c(
        "vas", STATE, GUARDED, STATUS, "Money-moving; provider state must gate retry."
    ),
    "app.tasks.vas.sync_vas_catalog": _c("vas", SWEEP, IDEMP, HEALTH),
    "app.tasks.vpn.run_vpn_control_job": _c("network", STATE, STATEFUL, STATUS),
    "app.tasks.vpn.run_vpn_health_scan": _c("network", SWEEP, IDEMP, HEALTH),
    "app.tasks.webhooks.deliver_webhook": _c("webhooks", AUTORETRY, GUARDED, STATUS),
    "app.tasks.webhooks.retry_failed_deliveries": _c("webhooks", DLQ, GUARDED, STATUS),
    "app.tasks.wireguard.cleanup_connection_logs": _c("network", SWEEP, IDEMP, LOG),
    "app.tasks.wireguard.cleanup_expired_tokens": _c("network", SWEEP, IDEMP, LOG),
    "app.tasks.wireguard.generate_connection_log_report": _c(
        "network", MANUAL, IDEMP, STATUS
    ),
    "app.tasks.work_orders.reconcile_work_order_mirror": _c(
        "crm", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.work_orders.refresh_work_order_mirror_for_subscriber": _c(
        "crm",
        NONE,
        IDEMP,
        LOG,
        "Best-effort on-view refresh; periodic reconcile backs it.",
    ),
    "app.tasks.workflow.detect_sla_breaches": _c("workflow", SWEEP, IDEMP, STATUS),
    "app.tasks.zabbix_ingestion.dispatch_portal_usage_ingestion": _c(
        "zabbix", SWEEP, IDEMP, HEALTH
    ),
    "app.tasks.zabbix_ingestion.ingest_olt_signals_from_zabbix": _c(
        "zabbix", ITEMS, PER_ITEM, STATUS
    ),
    "app.tasks.zabbix_ingestion.ingest_portal_usage": _c(
        "zabbix", ITEMS, PER_ITEM, STATUS
    ),
    "app.tasks.zabbix_ingestion.ingest_portal_usage_chunk": _c(
        "zabbix", ITEMS, PER_ITEM, STATUS
    ),
    "app.tasks.zabbix_ingestion.repair_stale_olt_signal_ingest": _c(
        "zabbix", SWEEP, IDEMP, STATUS
    ),
    "app.tasks.zabbix_sync.remove_device_from_zabbix": _c(
        "zabbix", MANUAL, GUARDED, STATUS
    ),
    "app.tasks.zabbix_sync.sync_devices_to_zabbix": _c("zabbix", SWEEP, IDEMP, HEALTH),
    "app.tasks.zabbix_sync.sync_single_nas_to_zabbix": _c(
        "zabbix", MANUAL, IDEMP, STATUS
    ),
    "app.tasks.zabbix_sync.sync_single_olt_to_zabbix": _c(
        "zabbix", MANUAL, IDEMP, STATUS
    ),
    "router_sync.capture_scheduled_snapshots": _c("router", SWEEP, IDEMP, HEALTH),
    "router_sync.cleanup_idle_tunnels": _c("router", SWEEP, IDEMP, LOG),
    "router_sync.execute_config_push": _c("router", STATE, STATEFUL, STATUS),
    "router_sync.sync_all_interfaces": _c("router", SWEEP, IDEMP, HEALTH),
    "router_sync.sync_all_system_info": _c("router", SWEEP, IDEMP, HEALTH),
}


def is_first_party_task(task_name: str) -> bool:
    return task_name.startswith("app.tasks.") or task_name.startswith("router_sync.")


def find_missing_task_reliability_contracts(
    registered_task_names: Iterable[str],
) -> list[str]:
    registered = {
        task_name
        for task_name in registered_task_names
        if is_first_party_task(task_name)
    }
    return sorted(registered - set(TASK_RELIABILITY_CONTRACTS))


def find_stale_task_reliability_contracts(
    registered_task_names: Iterable[str],
) -> list[str]:
    registered = {
        task_name
        for task_name in registered_task_names
        if is_first_party_task(task_name)
    }
    return sorted(set(TASK_RELIABILITY_CONTRACTS) - registered)
