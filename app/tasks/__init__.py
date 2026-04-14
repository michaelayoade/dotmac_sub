from app.tasks.alert_evaluation import evaluate_alert_rules
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
from app.tasks.billing import run_invoice_cycle
from app.tasks.collections import run_dunning, run_prepaid_enforcement
from app.tasks.exports import run_export_job, run_scheduled_export
from app.tasks.gis import run_batch_geocode_job, sync_gis_sources
from app.tasks.imports import run_import_job
from app.tasks.integrations import run_integration_job
from app.tasks.invoice_pdf import generate_invoice_pdf_export
from app.tasks.monitoring_cleanup import (
    cleanup_old_device_metrics as cleanup_device_metrics,
)
from app.tasks.monitoring_cleanup import (
    sync_nas_to_monitoring as sync_nas_devices_to_monitoring,
)
from app.tasks.mrr import snapshot_mrr
from app.tasks.nas import (
    check_nas_health,
    cleanup_nas_backups,
    run_scheduled_backups,
    update_subscriber_counts,
)
from app.tasks.network_monitoring import (
    refresh_core_device_ping,
    refresh_core_device_snmp,
)
from app.tasks.network_operations import cleanup_old_operations
from app.tasks.notifications import deliver_notification_queue
from app.tasks.oauth import check_token_health, refresh_expiring_tokens
from app.tasks.olt_config_backup import backup_all_olts
from app.tasks.olt_polling import (
    finalize_olt_polling,
    poll_all_olt_signals,
    poll_single_olt,
)
from app.tasks.ont_authorization import (
    run_authorize_autofind_ont_task,
    run_post_authorization_follow_up_task,
)
from app.tasks.ont_autofind import discover_all_olt_autofind
from app.tasks.ont_bulk import execute_bulk_action as execute_ont_bulk_action
from app.tasks.ont_discovery import discover_all_olt_onts
from app.tasks.ont_provisioning import (
    auto_link_profiles,
    detect_profile_drift,
)
from app.tasks.provisioning import run_bulk_activation_job, run_service_migration_job
from app.tasks.provisioning_enforcement import run_enforcement
from app.tasks.radius import run_radius_sync_job
from app.tasks.snmp import discover_interfaces as discover_snmp_interfaces
from app.tasks.snmp import walk_interfaces as walk_snmp_interfaces
from app.tasks.splynx_sync import run_incremental_sync
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
    sync_all_acs_devices as tr069_sync_all_acs_devices,
)
from app.tasks.usage import import_radius_accounting, run_usage_rating
from app.tasks.vacation_holds import resume_expired_holds
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
from app.tasks.workflow import detect_sla_breaches as retired_detect_sla_breaches

__all__ = [
    "cleanup_old_operations",
    "sync_gis_sources",
    "run_batch_geocode_job",
    "run_import_job",
    "run_integration_job",
    "generate_invoice_pdf_export",
    "run_radius_sync_job",
    "run_invoice_cycle",
    "run_dunning",
    "run_prepaid_enforcement",
    "run_scheduled_export",
    "run_export_job",
    "run_usage_rating",
    "import_radius_accounting",
    "cleanup_nas_backups",
    "refresh_core_device_ping",
    "refresh_core_device_snmp",
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
    "poll_all_olt_signals",
    "poll_single_olt",
    "finalize_olt_polling",
    "discover_all_olt_onts",
    "run_bulk_activation_job",
    "run_service_migration_job",
    "run_incremental_sync",
    "discover_snmp_interfaces",
    "walk_snmp_interfaces",
    "run_vpn_control_job",
    "run_vpn_health_scan",
    "deliver_webhook",
    "retry_failed_deliveries",
    "deliver_notification_queue",
    "detect_profile_drift",
    "auto_link_profiles",
    "snapshot_mrr",
    "tr069_sync_all_acs_devices",
    "tr069_execute_pending_jobs",
    "tr069_execute_bulk_action",
    "tr069_check_device_health",
    "tr069_refresh_ont_runtime",
    "cleanup_tr069_records",
    "run_scheduled_backups",
    "update_subscriber_counts",
    "check_nas_health",
    "execute_ont_bulk_action",
    "discover_all_olt_autofind",
    "run_authorize_autofind_ont_task",
    "run_post_authorization_follow_up_task",
    "run_enforcement",
    "evaluate_alert_rules",
    "cleanup_device_metrics",
    "sync_nas_devices_to_monitoring",
    "retired_detect_sla_breaches",
    "resume_expired_holds",
]
