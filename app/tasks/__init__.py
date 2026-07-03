from app.tasks.admin_alerts import evaluate_infrastructure_alerts
from app.tasks.alert_evaluation import evaluate_alert_rules
from app.tasks.app_cache import (
    refresh_dashboard_stats_cache_task,
    refresh_ont_zabbix_snapshot_cache_task,
)
from app.tasks.arrangements import check_overdue_arrangements
from app.tasks.autopay import charge_due_invoices
from app.tasks.bandwidth import (
    aggregate_to_metrics as aggregate_bandwidth_to_metrics,
)
from app.tasks.bandwidth import (
    cleanup_hot_data as cleanup_bandwidth_hot_data,
)
from app.tasks.bandwidth import (
    process_bandwidth_stream,
)
from app.tasks.bandwidth import (
    trim_redis_stream as trim_bandwidth_stream,
)
from app.tasks.billing import (
    audit_cutover_balance_invariant_task,
    check_billing_switch_task,
    run_invoice_cycle,
)
from app.tasks.catalog import expire_subscriptions
from app.tasks.collections import run_dunning
from app.tasks.crm_billing_push import push_crm_billing_snapshots
from app.tasks.crm_sync import push_subscriber_change as push_crm_subscriber_change
from app.tasks.crm_sync import redrive_crm_dead_letters
from app.tasks.crm_ticket_pull import (
    pull_crm_tickets,
    sync_crm_ticket,
)
from app.tasks.crm_ticket_push import (
    push_comment_to_crm,
    push_ticket_to_crm,
)
from app.tasks.enforcement import cleanup_subscription_block_sessions
from app.tasks.events import (
    cleanup_old_events,
    mark_stale_processing_events,
    retry_failed_events,
)
from app.tasks.exports import run_export_job, run_scheduled_export
from app.tasks.gis import run_batch_geocode_job, sync_gis_sources
from app.tasks.imports import run_import_job
from app.tasks.infrastructure_availability import (
    prune_infrastructure_availability,
    snapshot_infrastructure_availability,
)
from app.tasks.integrations import run_integration_job
from app.tasks.invoice_pdf import generate_invoice_pdf_export
from app.tasks.ip_utilization import (
    prune_ip_pool_utilization_snapshots,
    snapshot_ip_pool_utilization,
)
from app.tasks.monitoring_cleanup import (
    cleanup_old_device_metrics as cleanup_device_metrics,
)
from app.tasks.monitoring_cleanup import (
    sync_inventory_to_monitoring as sync_inventory_devices_to_monitoring,
)
from app.tasks.monitoring_cleanup import (
    sync_nas_to_monitoring as sync_nas_devices_to_monitoring,
)
from app.tasks.monitoring_coverage import refresh_monitoring_coverage
from app.tasks.monitoring_warm import warm_monitoring_caches
from app.tasks.mrr import snapshot_mrr
from app.tasks.nas import (
    check_nas_health,
    cleanup_nas_backups,
    run_scheduled_backups,
    update_subscriber_counts,
)
from app.tasks.network_operations import cleanup_old_operations
from app.tasks.notifications import deliver_notification_queue
from app.tasks.oauth import check_token_health, refresh_expiring_tokens
from app.tasks.olt_config_backup import backup_all_olts
from app.tasks.olt_health_retry import (
    retry_failed_olt_connections,
    retry_single_olt,
    trigger_immediate_retry,
)
from app.tasks.ont_bulk import execute_bulk_action as execute_ont_bulk_action
from app.tasks.ont_provisioning import (
    authorize_ont as authorize_ont_task,
)
from app.tasks.ont_provisioning import (
    provision_ont,
    queue_bulk_provisioning,
)
from app.tasks.payment_reconciliation import reconcile_topups
from app.tasks.profile_sync import (
    execute_due_profile_sync_tasks,
)
from app.tasks.projects import reconcile_project_mirror
from app.tasks.provisioning import (
    reap_stale_provisioning_runs,
    retry_pending_compensation_failures,
    run_bulk_activation_job,
    run_service_migration_job,
)
from app.tasks.quotes import reconcile_quote_mirror
from app.tasks.radius import run_radius_sync_job
from app.tasks.radius_population import refresh_radius_from_subs, sync_device_login
from app.tasks.referrals import reconcile_referral_mirror
from app.tasks.router_sync import (
    capture_scheduled_snapshots,
    cleanup_idle_tunnels,
    execute_config_push,
    sync_all_interfaces,
    sync_all_system_info,
)
from app.tasks.topology_lldp import run_lldp_topology_poll
from app.tasks.topology_sync import run_topology_reconcile, warm_topology_status
from app.tasks.tr069 import (
    apply_acs_config as tr069_apply_acs_config,
)
from app.tasks.tr069 import (
    check_device_health as tr069_check_device_health,
)
from app.tasks.tr069 import (
    cleanup_tr069_records,
)
from app.tasks.tr069 import (
    execute_bulk_action as tr069_execute_bulk_action,
)
from app.tasks.tr069 import (
    execute_pending_jobs as tr069_execute_pending_jobs,
)
from app.tasks.tr069 import (
    refresh_ont_runtime_data as tr069_refresh_ont_runtime,
)
from app.tasks.tr069 import (
    refresh_single_ont_runtime as tr069_refresh_single_ont,
)
from app.tasks.tr069 import (
    sync_all_acs_devices as tr069_sync_all_acs_devices,
)
from app.tasks.usage import (
    import_radius_accounting,
    notify_expiring_data_bundles,
    reap_stale_radius_sessions,
    run_usage_rating,
)
from app.tasks.vacation_holds import resume_expired_holds
from app.tasks.vas import (
    run_vas_requery,
    run_vas_review_requery,
    run_wallet_auto_deduct,
    sync_vas_catalog,
)
from app.tasks.vpn import run_vpn_control_job, run_vpn_health_scan
from app.tasks.webhooks import (
    deliver_webhook,
    retry_failed_deliveries,
)
from app.tasks.wireguard import (
    cleanup_connection_logs as cleanup_wireguard_logs,
)
from app.tasks.wireguard import (
    cleanup_expired_tokens as cleanup_wireguard_tokens,
)
from app.tasks.wireguard import (
    generate_connection_log_report as wireguard_connection_report,
)
from app.tasks.work_orders import reconcile_work_order_mirror
from app.tasks.workflow import detect_sla_breaches as retired_detect_sla_breaches
from app.tasks.zabbix_ingestion import (
    dispatch_portal_usage_ingestion,
    ingest_olt_signals_from_zabbix,
    ingest_portal_usage,
    ingest_portal_usage_chunk,
    repair_stale_olt_signal_ingest,
)
from app.tasks.zabbix_sync import (
    remove_device_from_zabbix_task,
    sync_devices_to_zabbix,
    sync_single_nas_to_zabbix,
    sync_single_olt_to_zabbix,
)

__all__ = [
    "cleanup_old_operations",
    "sync_gis_sources",
    "run_batch_geocode_job",
    "run_vas_requery",
    "run_vas_review_requery",
    "run_wallet_auto_deduct",
    "sync_vas_catalog",
    "run_import_job",
    "run_integration_job",
    "generate_invoice_pdf_export",
    "run_radius_sync_job",
    "provision_ont",
    "queue_bulk_provisioning",
    "run_invoice_cycle",
    "charge_due_invoices",
    "check_overdue_arrangements",
    "reconcile_topups",
    "expire_subscriptions",
    "run_dunning",
    "check_billing_switch_task",
    "push_crm_subscriber_change",
    "redrive_crm_dead_letters",
    "pull_crm_tickets",
    "sync_crm_ticket",
    "push_ticket_to_crm",
    "push_comment_to_crm",
    "push_crm_billing_snapshots",
    "run_scheduled_export",
    "run_export_job",
    "retry_failed_events",
    "mark_stale_processing_events",
    "cleanup_old_events",
    "cleanup_subscription_block_sessions",
    "run_usage_rating",
    "import_radius_accounting",
    "reap_stale_radius_sessions",
    "reap_stale_provisioning_runs",
    "notify_expiring_data_bundles",
    "cleanup_nas_backups",
    "refresh_expiring_tokens",
    "check_token_health",
    "cleanup_wireguard_logs",
    "cleanup_wireguard_tokens",
    "wireguard_connection_report",
    "process_bandwidth_stream",
    "cleanup_bandwidth_hot_data",
    "aggregate_bandwidth_to_metrics",
    "trim_bandwidth_stream",
    "backup_all_olts",
    "retry_failed_olt_connections",
    "retry_single_olt",
    "trigger_immediate_retry",
    "run_bulk_activation_job",
    "run_service_migration_job",
    "retry_pending_compensation_failures",
    "refresh_radius_from_subs",
    "sync_device_login",
    "run_vpn_control_job",
    "run_vpn_health_scan",
    "deliver_webhook",
    "retry_failed_deliveries",
    "deliver_notification_queue",
    "snapshot_mrr",
    "snapshot_ip_pool_utilization",
    "snapshot_infrastructure_availability",
    "prune_infrastructure_availability",
    "prune_ip_pool_utilization_snapshots",
    "run_topology_reconcile",
    "warm_topology_status",
    "run_lldp_topology_poll",
    "tr069_sync_all_acs_devices",
    "tr069_execute_pending_jobs",
    "tr069_execute_bulk_action",
    "tr069_apply_acs_config",
    "tr069_check_device_health",
    "tr069_refresh_ont_runtime",
    "tr069_refresh_single_ont",
    "cleanup_tr069_records",
    "run_scheduled_backups",
    "update_subscriber_counts",
    "check_nas_health",
    "execute_ont_bulk_action",
    "authorize_ont_task",
    "evaluate_alert_rules",
    "evaluate_infrastructure_alerts",
    "refresh_dashboard_stats_cache_task",
    "refresh_ont_zabbix_snapshot_cache_task",
    "cleanup_device_metrics",
    "sync_nas_devices_to_monitoring",
    "sync_inventory_devices_to_monitoring",
    "retired_detect_sla_breaches",
    "resume_expired_holds",
    "dispatch_portal_usage_ingestion",
    "ingest_olt_signals_from_zabbix",
    "ingest_portal_usage",
    "ingest_portal_usage_chunk",
    "repair_stale_olt_signal_ingest",
    "sync_devices_to_zabbix",
    "sync_single_olt_to_zabbix",
    "sync_single_nas_to_zabbix",
    "remove_device_from_zabbix_task",
    # OLT queue processing (Phase 4)
    "execute_due_profile_sync_tasks",
    "warm_monitoring_caches",
    "refresh_monitoring_coverage",
    # Router config sync/snapshot (keystone) — previously unregistered, so the
    # scheduled capture never ran. Importing here registers them with the worker.
    "capture_scheduled_snapshots",
    "cleanup_idle_tunnels",
    "execute_config_push",
    "sync_all_interfaces",
    "sync_all_system_info",
    "reconcile_project_mirror",
    "reconcile_quote_mirror",
    "reconcile_referral_mirror",
    "reconcile_work_order_mirror",
]
