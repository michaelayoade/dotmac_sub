"""Detect a NEW writer that opens its own session and commits, undeclared.

ADR 0017 introduces ``TransactionMode.OUT_OF_BAND_EVIDENCE``: a service is
allowed to open its own ``db_session_adapter.create_session()`` unit of work
and commit it independently of the caller's transaction, instead of joining
it. ``tests/architecture/test_out_of_band_evidence_ratchet.py`` binds every
service that DECLARES this mode to its approving ADR -- but its own docstring
says the gap plainly: nothing there detects a new writer that does the same
``create_session()`` + ``commit()`` thing without declaring the mode at all.

This module closes that gap with a static AST scan over ``app/**/*.py`` for
any function whose OWN body (not a nested function's body) both opens a
session via ``create_session()`` -- directly, or through a module-level
alias such as ``SessionLocal = db_session_adapter.create_session`` (see
``app/services/enforcement_scheduled.py``) -- and calls ``.commit()`` on
anything. The result is compared, in both directions, against a fixed
APPROVED set (declared ``out_of_band_evidence`` writers, checked against the
SOT registry) union a BASELINE set (everything else the scan currently
finds, each classified by hand). A hit outside both sets fails the build: it
is either a genuinely new out-of-band writer that needs an ADR and a
registry declaration (ADR 0017), or it needs classifying into BASELINE with
a reason. A BASELINE entry that stops matching must be removed, so this
ratchet only shrinks.

Now covered, alongside the original ``<x>.create_session()`` (+ module-level
alias) + literal ``.commit()`` shape: ``from app.db import SessionLocal``
(optionally ``as X``) called as ``SessionLocal()`` and committed in the same
function; any module-level ``NAME = sessionmaker(...)`` factory called and
committed the same way; and the auto-committing ``with
db_session_adapter.session()`` / ``advisory_lock()`` blocks, which commit on
normal exit with no visible ``.commit()`` at the use site and so are flagged
by entering the context manager alone. ``owner_command_session()`` and
``read_session()`` are deliberately NOT flagged -- read
``app/services/db_session_adapter.py``: the former only rolls back
defensively (never commits) so the caller-registered command stays the sole
transaction owner, and the latter always rolls back; neither is an
out-of-band writer. Also covered: ``task_session()`` (``app/db.py``'s own
bare-name auto-committing generator), recognised whether ``from app.db
import task_session`` is imported at module level or -- the common case at
every real call site -- function-locally, inside the very function that then
calls ``task_session()``.

Stated limitations (an unmonitored region, not an exemption; follow-up owed):
the scan still does not recognise a FUNCTION-LOCAL alias of ``SessionLocal``
itself (only a function-local alias of ``task_session`` is handled; a
module-level ``SessionLocal``/``sessionmaker`` alias is handled; a
function-local ``SessionLocal`` import, e.g. ``app/services/enforcement.py``'s
``_coa_neg_ttl`` or ``app/web/admin/nas.py``'s ``_radius_secret_length``, is
not -- both are read-only today, not a live gap, but unmonitored), nor a
session opened in one function and committed in another (cross-function
commit) -- ADR 0017's own ratchet test docstring names this gap too.
Deliberately EXCLUDED, not missed: a ``Session(bind=db.connection(),
join_transaction_mode="create_savepoint")`` join (it explicitly joins the
caller's transaction rather than opening an independent one -- see
``app/services/auth_flow.py``, ``financial_imports.py``,
``events/dispatcher.py``), lock-only helpers such as
``app/tasks/_postgres_lock.py``'s ``postgres_session_advisory_lock`` (commits
only the advisory-lock acquisition, never business data), and
``engine.begin()`` against the external RADIUS accounting database (a
different engine entirely, e.g. ``radius_population.py``/``radius_reconciliation.py``/``usage.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Declared ``out_of_band_evidence`` writers (ADR 0017), each of which must
#: also be bound to its approving ADR by
#: ``test_out_of_band_evidence_ratchet.APPROVED_OUT_OF_BAND_EVIDENCE`` and be
#: registered in the SOT registry with that transaction mode (see test (c)
#: below).
APPROVED: frozenset[str] = frozenset(
    {
        "app/services/enforcement_evidence.py::record_enforcement_application",
    }
)

#: Everything else the scanner currently finds, classified by hand. Adding an
#: entry here without a genuine reason defeats the ratchet -- see (a)/(b)
#: below, which fail the build on both a new, unclassified hit and a stale
#: baseline entry that no longer matches.
BASELINE: dict[str, str] = {
    # -- Legacy out-of-band writers, already tracked by name in
    # -- test_out_of_band_evidence_ratchet.KNOWN_LEGACY_OUT_OF_BAND_WRITERS
    # -- (ADR 0017 follow-up debt, not yet migrated to the declared mode).
    "app/services/nas/_mikrotik.py::_record_mikrotik_auth_attempt": (
        "legacy out-of-band writer (listed in "
        "test_out_of_band_evidence_ratchet.KNOWN_LEGACY_OUT_OF_BAND_WRITERS)"
    ),
    "app/services/prepaid_service_renewals.py::_record_review_item_out_of_band": (
        "legacy out-of-band writer (listed in "
        "test_out_of_band_evidence_ratchet.KNOWN_LEGACY_OUT_OF_BAND_WRITERS)"
    ),
    # -- Same technique as the legacy writer above (a staff alert on a genuinely
    # -- independent connection). Surfaced by this scanner and now listed in
    # -- KNOWN_LEGACY_OUT_OF_BAND_WRITERS rather than silently baselined.
    "app/services/prepaid_service_renewals.py::_finalize_scheduled_renewal_summary": (
        "legacy out-of-band writer (listed in "
        "test_out_of_band_evidence_ratchet.KNOWN_LEGACY_OUT_OF_BAND_WRITERS)"
    ),
    # -- Scheduled-job / background-worker pattern: a Celery task or scheduled
    # -- runner opens its own session for the lifetime of ONE scheduled
    # -- invocation and commits its own work; there is no caller transaction
    # -- to join because the task itself is the top of the call stack. This
    # -- is the ordinary "adapter owns its own unit of work" shape, not an
    # -- evidence-writer exemption from ADR 0017's caller-transaction rule.
    "app/services/billing/scheduled.py::mark_invoices_overdue": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/billing/scheduled.py::run_billing_notifications": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/billing/scheduled.py::run_invoice_cycle": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/billing_invoice_pdf.py::process_export": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/collections/scheduled.py::run_billing_enforcement": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/collections/scheduled.py::run_bundle_reconcile": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/enforcement_scheduled.py::cleanup_subscription_block_sessions": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/network/ont_action_common.py::persist_data_model_root": (
        "legacy out-of-band writer (listed in "
        "test_out_of_band_evidence_ratchet.KNOWN_LEGACY_OUT_OF_BAND_WRITERS): a "
        "nested helper called mid-flow by ONT/CPE action helpers that commits "
        "OntUnit/CPEDevice.tr069_data_model on its own session"
    ),
    "app/services/task_idempotency.py::idempotent_task.decorator.wrapper": (
        "infrastructure: the idempotent_task decorator's own TaskExecution "
        "ledger writes, on its own session, independent of the wrapped task"
    ),
    "app/services/web_network_core_runtime.py::_refresh_device_health_worker": (
        "adapter-owned session lifecycle (task/runner): isolated background "
        "device-health refresh, own session per call"
    ),
    "app/tasks/forwarding_control_observations.py::run_forwarding_control_observation_poll": (
        "adapter-owned session lifecycle (task/runner): scheduled read-only "
        "collection task"
    ),
    "app/tasks/gis.py::run_batch_geocode_job": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/gis.py::sync_gis_sources": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/nas.py::check_nas_health": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/nas.py::run_scheduled_backups": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/nas.py::update_subscriber_counts": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/network_operations.py::cleanup_old_operations": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/nin_tasks.py::verify_nin_task": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/olt_config_backup.py::backup_all_olts": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/olt_firmware.py::rollback_firmware_task": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/olt_firmware.py::upgrade_firmware_task": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/ont_signal_observations.py::record_ont_observations": (
        "adapter-owned session lifecycle (task/runner): scheduled telemetry "
        "observation collector (freezes a status/Rx snapshot), own session"
    ),
    "app/tasks/radius.py::_run_enforcement_reconciler": (
        "false-positive coupling, not an evidence writer: the flagged "
        "``.commit()`` belongs to a separate raw psycopg connection "
        "(``rconn``) used for RADIUS drift bookkeeping, not the "
        "``create_session()`` ORM session opened in this same function, "
        "which is never committed here"
    ),
    "app/tasks/radius.py::reconcile_active_sessions": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/radius.py::run_radius_sync_job": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/router_sync.py::_recover_pending_readback": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/router_sync.py::audit_sot_drift": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/router_sync.py::capture_scheduled_snapshots": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/router_sync.py::execute_config_push": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/router_sync.py::reconcile_config_push_readback": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/router_sync.py::reconcile_nas_vlan_readback": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/router_sync.py::sync_all_system_info": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/tr069.py::apply_acs_config": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/tr069.py::apply_saved_ont_service_config": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/tr069.py::cleanup_tr069_records": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/tr069.py::wait_for_ont_bootstrap": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/uisp_control.py::_mark_pending_readback": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/uisp_ip_backfill.py::run_uisp_mgmt_ip_backfill": (
        "adapter-owned session lifecycle (task/runner): manual-trigger "
        "inventory backfill, own session per run"
    ),
    "app/tasks/unmatched_radio.py::run_unmatched_radio_review": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/usage.py::evaluate_fup_rules": "adapter-owned session lifecycle (task/runner)",
    # -- New hits from the SessionLocal()/sessionmaker and auto-committing
    # -- context-manager shapes (session_local_commit, auto_commit_session,
    # -- auto_commit_advisory_lock): ordinary Celery task / scheduled-job
    # -- entry points, each opening its own session for the lifetime of ONE
    # -- invocation. Same category as the pre-existing task/runner baseline
    # -- entries above, just reached via db_session_adapter.session()/
    # -- .advisory_lock() instead of a bare create_session()+commit().
    "app/tasks/admin_alerts.py::evaluate_infrastructure_alerts": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ai_operations.py::expire_stale_insights": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/alert_evaluation.py::evaluate_alert_rules": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/arrangements.py::check_overdue_arrangements": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/autopay.py::charge_due_invoices": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/bandwidth.py::cleanup_hot_data": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/bandwidth.py::process_bandwidth_stream": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/campaigns.py::process_due_campaign_steps": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/campaigns.py::process_due_campaigns": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/campaigns.py::send_campaign_batch": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/catalog.py::apply_due_subscription_changes": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/catalog.py::apply_due_subscription_status_commands": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/catalog.py::expire_subscriptions": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/catalog.py::send_expiry_reminders": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/channel_health.py::observe_channel_health": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/customer_impact_metrics.py::export_customer_impact_metrics": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/dotmac_erp_outbox.py::reconcile_erp_staff_access": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/events.py::cleanup_old_events": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/events.py::dispatch_pending_events": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/events.py::mark_stale_processing_events": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/events.py::retry_failed_events": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/exports.py::run_export_job": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/exports.py::run_scheduled_export": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/infrastructure_availability.py::prune_infrastructure_availability": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/infrastructure_availability.py::snapshot_infrastructure_availability": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/integration_delivery.py::deliver_integration_event": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/integration_delivery.py::deliver_meta_lead_conversion": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/integration_inbox.py::reclaim_stale_claims": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/integrations.py::run_integration_job": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ip_utilization.py::prune_ip_pool_utilization_snapshots": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ip_utilization.py::snapshot_ip_pool_utilization": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/monitoring_cleanup.py::cleanup_old_device_metrics": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/monitoring_cleanup.py::sync_inventory_to_monitoring": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/monitoring_cleanup.py::sync_nas_to_monitoring": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/mrr.py::snapshot_mrr": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/network_operation_dispatch.py::publish_network_operation_dispatches": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/notifications.py::deliver_notification": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/notifications.py::deliver_notification_queue": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/olt_health_retry.py::retry_failed_olt_connections": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/olt_health_retry.py::retry_single_olt": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/olt_mac_harvest.py::run_single_olt_mac_harvest": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_bulk.py::_queue_bulk_provisioning": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_bulk.py::execute_bulk_action": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_commissioning.py::cleanup_commissioned_ont": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_commissioning.py::verify_commissioned_ont": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_firmware.py::apply_huawei_ont_firmware": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_firmware.py::verify_huawei_ont_firmware": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_provisioning.py::authorize_ont": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_provisioning.py::provision_ont": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_provisioning.py::queue_bulk_provisioning": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_reconcile.py::_close_expired_remote_access": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_reconcile.py::_reconcile_dialer_credentials": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_reconcile.py::alert_overdue_reconcile_holds": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_reconcile.py::reconcile_huawei_ont": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_reconcile.py::run_ont_reconcile_sweep": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_runtime_status.py::dispatch_huawei_ont_status": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_runtime_status.py::refresh_huawei_olt_status": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/ont_runtime_status.py::refresh_single_ont_status": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/operational_escalations.py::dispatch_operational_escalation_deliveries": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/outage_auto_notify.py::auto_dispatch_outage_notifications": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/provisioning.py::reap_stale_provisioning_runs": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/provisioning.py::retry_pending_compensation_failures": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/provisioning.py::run_bulk_activation_job": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/provisioning.py::run_service_migration_job": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/radius_health.py::run_radius_health_check": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/team_inbox.py::auto_resolve_stale_conversations": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/team_inbox.py::backfill_conversation_participants": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/team_inbox.py::process_ai_intake_sessions": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/team_inbox.py::promote_message_media_assets": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/team_inbox.py::promote_queued_conversations": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/team_inbox.py::recover_stale_ai_intake": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/team_inbox.py::release_scheduled_replies": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/team_inbox.py::repair_whatsapp_locations": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/team_inbox.py::retry_failed_outbound_messages": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/team_inbox.py::send_queue_position_notifications": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/team_inbox.py::wake_due_snoozed_conversations": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/topology_outage.py::reconcile_detected_outages": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/topology_sync.py::warm_topology_status": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/topology_ufiber_link.py::run_ufiber_onu_link": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/topology_uisp.py::run_uisp_topology_sync": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/uisp_control.py::execute_uisp_apply": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/uisp_control.py::reconcile_uisp_config_readback": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/usage.py::_import_radius_accounting_locked": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/usage.py::meter_usage_into_quota": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/usage.py::notify_expiring_data_bundles": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/usage.py::reap_stale_radius_sessions": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/usage.py::run_usage_rating": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/vpn.py::run_vpn_control_job": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/vpn.py::run_vpn_health_scan": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/wireguard.py::cleanup_connection_logs": "adapter-owned session lifecycle (task/runner)",
    "app/tasks/wireguard.py::cleanup_expired_tokens": "adapter-owned session lifecycle (task/runner)",
    "app/services/billing/scheduled.py::refresh_billing_health_snapshot": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/credential_rotation_schedule.py::run_scheduled_credential_rotation": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/field/material_catalog_sync.py::run_erp_material_catalog_sync": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/syslog/handlers.py::_handle_autofind_event": (
        "adapter-owned session lifecycle (task/runner): syslog listener handling one ONTAUTOFIND event synchronously; own session per event, no caller transaction to join"
    ),
    # -- Infrastructure: the session-provider machinery itself (the
    # -- generator/method that IS create_session()+commit(), not a caller
    # -- of it) and a durable-execution-claim decorator with its own ledger
    # -- session, independent of the function it wraps -- same shape as
    # -- idempotent_task's own TaskExecution ledger writes above.
    "app/db.py::task_session": (
        "infrastructure: legacy task-owned transaction generator (app/db.py) -- this IS the SessionLocal()+commit() definition, not a caller of it; existing callers are tracked migration debt"
    ),
    "app/services/db_session_adapter.py::SqlAlchemySessionAdapter.session": (
        "infrastructure: the adapter's own auto-committing session() context-manager implementation, not a caller of it"
    ),
    "app/services/network_operation_dispatch.py::managed_network_operation_dispatch.decorator.wrapped": (
        "infrastructure: durable dispatch-claim decorator wraps a device task with its own execution-claim ledger session, independent of the wrapped task"
    ),
    # -- Notification delivery availability probes: each opens its own
    # -- session for one read-only capability check. Invoked from arbitrary
    # -- callers (API handlers, tasks), so the probe is deliberately isolated
    # -- from whatever transaction the caller happens to hold, not a caller
    # -- transaction it should join.
    "app/services/notification_adapter.py::EmailProvider.is_available": (
        "adapter-owned session lifecycle (notification delivery adapter): "
        "own session per send/availability probe, independent of caller"
    ),
    "app/services/notification_adapter.py::SmsProvider.is_available": (
        "adapter-owned session lifecycle (notification delivery adapter): "
        "own session per send/availability probe, independent of caller"
    ),
    # -- Legacy out-of-band writers, tracked by name in
    # -- test_out_of_band_evidence_ratchet.KNOWN_LEGACY_OUT_OF_BAND_WRITERS:
    # -- called from app/services/operational_escalation_delivery.py's
    # -- ``_send_to_target(db, ...)`` chain (db: Session held open by the
    # -- caller), each opens its OWN session via db_session_adapter.session()
    # -- and commits a delivery record mid-flow while that caller session may
    # -- still be open -- the same nested-commit-while-caller-holds-a-session
    # -- shape as persist_data_model_root.
    "app/services/notification_adapter.py::EmailProvider.send": (
        "legacy out-of-band writer (listed in "
        "test_out_of_band_evidence_ratchet.KNOWN_LEGACY_OUT_OF_BAND_WRITERS)"
    ),
    "app/services/notification_adapter.py::SmsProvider.send": (
        "legacy out-of-band writer (listed in "
        "test_out_of_band_evidence_ratchet.KNOWN_LEGACY_OUT_OF_BAND_WRITERS)"
    ),
    # -- New task_session_context hits: app/db.py's task_session() is a
    # -- bare-name auto-committing generator (SessionLocal()+yield+commit),
    # -- imported function-locally at every real call site here. Every one is
    # -- a top-level Celery task or a standalone run_*() beat entry point with
    # -- no caller holding an open session -- verified by grepping every
    # -- caller (support_tickets is the @celery_app.task
    # -- function itself; the dotmac_erp run_*() functions are each
    # -- called only from a top-level task in app/tasks/dotmac_erp_outbox.py,
    # -- or (run_repair_expense_claim_writebacks) not wired to a caller at
    # -- all yet, per its own docstring).
    "app/services/dotmac_erp/expense_sync.py::run_refresh_expense_claim_statuses": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/dotmac_erp/expense_sync.py::run_repair_expense_claim_writebacks": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/dotmac_erp/material_sync.py::run_refresh_material_request_statuses": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/dotmac_erp/outbox.py::run_deliver_pending": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/dotmac_erp/purchase_invoice_sync.py::run_refresh_purchase_invoice_statuses": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/dotmac_erp/purchase_invoice_sync.py::run_repair_purchase_invoice_sync": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/services/dotmac_erp/purchase_order_sync.py::run_repair_purchase_order_writebacks": (
        "adapter-owned session lifecycle (task/runner)"
    ),
    "app/tasks/support_tickets.py::auto_confirm_resolved_tickets": (
        "adapter-owned session lifecycle (task/runner)"
    ),
}


#: Shape labels a hit can be recorded under. A function can match more than
#: one (e.g. a ``create_session()``+commit AND a nested ``with ...session()``
#: block); ``find_out_of_band_writers_with_shapes`` reports every shape that
#: matched so classification (and the sensitivity proofs below) can name
#: exactly which recogniser fired.
SHAPE_CREATE_SESSION_COMMIT = "create_session_commit"
SHAPE_SESSION_LOCAL_COMMIT = "session_local_commit"
SHAPE_AUTO_COMMIT_SESSION = "auto_commit_session"
SHAPE_AUTO_COMMIT_ADVISORY_LOCK = "auto_commit_advisory_lock"
SHAPE_TASK_SESSION_CONTEXT = "task_session_context"

#: ``with``-block context managers that commit on normal exit with no visible
#: ``.commit()`` call at the use site (see app/services/db_session_adapter.py).
#: ``owner_command_session()`` and ``read_session()`` are deliberately absent:
#: neither ever calls ``.commit()`` (the former only rolls back defensively,
#: the latter always rolls back), so they are not out-of-band writers.
_AUTO_COMMIT_CONTEXT_MANAGERS: dict[str, str] = {
    "session": SHAPE_AUTO_COMMIT_SESSION,
    "advisory_lock": SHAPE_AUTO_COMMIT_ADVISORY_LOCK,
}


def _module_create_session_aliases(module: ast.Module) -> frozenset[str]:
    """Module-level ``NAME = <expr>.create_session`` aliases (top-level only).

    Mirrors the pattern in ``app/services/enforcement_scheduled.py``:
    ``SessionLocal = db_session_adapter.create_session``.
    """
    aliases: set[str] = set()
    for node in module.body:
        targets: list[ast.expr] | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        if targets is None or value is None:
            continue
        if isinstance(value, ast.Attribute) and value.attr == "create_session":
            for target in targets:
                if isinstance(target, ast.Name):
                    aliases.add(target.id)
    return frozenset(aliases)


def _module_session_local_aliases(module: ast.Module) -> frozenset[str]:
    """Module-level names bound to a raw session FACTORY (not a
    ``create_session()``-style method call): ``from app.db import
    SessionLocal`` (optionally ``as X``), or a module-level ``NAME =
    sessionmaker(...)`` result. Calling one of these names directly (e.g.
    ``SessionLocal()``) opens a session the same way ``create_session()``
    does, just through a different spelling (see app/db.py's
    ``SessionLocal = sessionmaker(...)``).
    """
    aliases: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.ImportFrom) and node.module == "app.db":
            for alias in node.names:
                if alias.name == "SessionLocal":
                    aliases.add(alias.asname or alias.name)
        targets: list[ast.expr] | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        if targets is None or value is None:
            continue
        if isinstance(value, ast.Call):
            func = value.func
            is_sessionmaker = (
                isinstance(func, ast.Name) and func.id == "sessionmaker"
            ) or (isinstance(func, ast.Attribute) and func.attr == "sessionmaker")
            if is_sessionmaker:
                for target in targets:
                    if isinstance(target, ast.Name):
                        aliases.add(target.id)
    return frozenset(aliases)


def _module_task_session_aliases(module: ast.Module) -> frozenset[str]:
    """Module-level names bound to ``from app.db import task_session`` (optionally
    ``as X``). ``task_session`` (app/db.py) is itself a ``@contextmanager``
    generator that opens ``SessionLocal()``, yields, and commits on normal
    exit -- the same auto-committing shape as
    ``db_session_adapter.session()``, just a bare-name call rather than an
    attribute call, and often imported function-locally (see
    ``_local_task_session_aliases`` below) rather than at module level.
    """
    aliases: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.ImportFrom) and node.module == "app.db":
            for alias in node.names:
                if alias.name == "task_session":
                    aliases.add(alias.asname or alias.name)
    return frozenset(aliases)


def _local_task_session_aliases(node: ast.AST) -> frozenset[str]:
    """Function-local ``from app.db import task_session`` aliases within
    ``node``'s OWN body (excluding nested defs) -- the common shape at every
    real call site (e.g. ``app/services/dotmac_erp/outbox.py``), where the
    import sits inside the function rather than at module level.
    """
    aliases: set[str] = set()

    def walk(current: ast.AST) -> None:
        for child in ast.iter_child_nodes(current):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
            ):
                continue
            if isinstance(child, ast.ImportFrom) and child.module == "app.db":
                for alias in child.names:
                    if alias.name == "task_session":
                        aliases.add(alias.asname or alias.name)
            walk(child)

    walk(node)
    return frozenset(aliases)


def _is_task_session_call(call: ast.Call, aliases: frozenset[str]) -> bool:
    """Whether ``call`` invokes a bare ``task_session``-style name."""
    func = call.func
    return isinstance(func, ast.Name) and func.id in aliases


def _is_create_session_call(call: ast.Call, aliases: frozenset[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "create_session":
        return True
    return isinstance(func, ast.Name) and func.id in aliases


def _is_session_local_call(call: ast.Call, aliases: frozenset[str]) -> bool:
    """Whether ``call`` invokes a bare session-factory name, e.g. ``SessionLocal()``."""
    func = call.func
    return isinstance(func, ast.Name) and func.id in aliases


def _is_commit_call(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr == "commit"


def _auto_commit_context_manager_shape(expr: ast.expr) -> str | None:
    """Whether a ``with`` item's context expression is one of the
    auto-committing ``db_session_adapter`` context managers, and if so which
    shape it is. Matches on attribute name alone (like ``.create_session()``/
    ``.commit()`` above) -- a repo-wide grep confirms ``.session()``/
    ``.advisory_lock()`` calls in ``app/`` are exclusively
    ``db_session_adapter.session()``/``db_session_adapter.advisory_lock()``.
    """
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
        return _AUTO_COMMIT_CONTEXT_MANAGERS.get(expr.func.attr)
    return None


def _own_body_shapes(
    node: ast.AST,
    cs_aliases: frozenset[str],
    sl_aliases: frozenset[str],
    ts_module_aliases: frozenset[str],
) -> frozenset[str]:
    """Every shape matched by ``node``'s OWN body (excluding nested defs).

    A nested function's/lambda's/class's body is a separate scope reported
    as its own hit, so descent stops there -- otherwise an outer function
    would be flagged purely because something it defines internally happens
    to touch both calls.
    """
    has_create = False
    has_commit = False
    has_session_local = False
    shapes: set[str] = set()
    ts_aliases = ts_module_aliases | _local_task_session_aliases(node)

    def walk(current: ast.AST) -> None:
        nonlocal has_create, has_commit, has_session_local
        for child in ast.iter_child_nodes(current):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
            ):
                continue
            if isinstance(child, (ast.With, ast.AsyncWith)):
                for item in child.items:
                    shape = _auto_commit_context_manager_shape(item.context_expr)
                    if shape is not None:
                        shapes.add(shape)
                    context_expr = item.context_expr
                    if isinstance(context_expr, ast.Call) and _is_task_session_call(
                        context_expr, ts_aliases
                    ):
                        shapes.add(SHAPE_TASK_SESSION_CONTEXT)
            if isinstance(child, ast.Call):
                if _is_create_session_call(child, cs_aliases):
                    has_create = True
                if _is_session_local_call(child, sl_aliases):
                    has_session_local = True
                if _is_commit_call(child):
                    has_commit = True
            walk(child)

    walk(node)
    if has_create and has_commit:
        shapes.add(SHAPE_CREATE_SESSION_COMMIT)
    if has_session_local and has_commit:
        shapes.add(SHAPE_SESSION_LOCAL_COMMIT)
    return frozenset(shapes)


def find_out_of_band_writers_with_shapes(root: Path) -> dict[str, frozenset[str]]:
    """Pure scanner: every ``app/**/*.py`` function that, in its OWN body
    (not a nested function's), either (a) opens its own session -- via
    ``create_session()``/a module-level alias of it, or via a bare
    ``SessionLocal()``-style factory call -- AND commits it, or (b) enters
    one of the auto-committing ``db_session_adapter`` context managers
    (``session()``/``advisory_lock()``), or (c) enters ``task_session()``
    (``app/db.py``'s bare-name auto-committing generator, imported either at
    module level or, more commonly, function-locally). Maps each hit's
    ``"path::qualname"`` (``qualname`` dotted through enclosing
    classes/functions) to the set of shape labels (``SHAPE_*`` above) that
    matched it.
    """
    hits: dict[str, frozenset[str]] = {}
    app_root = root / "app"
    for path in sorted(app_root.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        cs_aliases = _module_create_session_aliases(tree)
        sl_aliases = _module_session_local_aliases(tree)
        ts_aliases = _module_task_session_aliases(tree)
        rel = path.relative_to(root).as_posix()
        hits.update(
            _scan_module_functions(tree, cs_aliases, sl_aliases, ts_aliases, rel)
        )
    return hits


def find_out_of_band_writers(root: Path) -> list[str]:
    """Sorted ``"path::qualname"`` hits from
    :func:`find_out_of_band_writers_with_shapes`, dropping the per-hit shape
    detail -- this is the list every BASELINE/APPROVED comparison below
    checks against.
    """
    return sorted(find_out_of_band_writers_with_shapes(root))


def _scan_module_functions(
    node: ast.AST,
    cs_aliases: frozenset[str],
    sl_aliases: frozenset[str],
    ts_aliases: frozenset[str],
    rel: str,
    stack: list[str] | None = None,
) -> dict[str, frozenset[str]]:
    stack = stack if stack is not None else []
    found: dict[str, frozenset[str]] = {}
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            found.update(
                _scan_module_functions(
                    child, cs_aliases, sl_aliases, ts_aliases, rel, [*stack, child.name]
                )
            )
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = ".".join([*stack, child.name])
            shapes = _own_body_shapes(child, cs_aliases, sl_aliases, ts_aliases)
            if shapes:
                found[f"{rel}::{qualname}"] = shapes
            found.update(
                _scan_module_functions(
                    child, cs_aliases, sl_aliases, ts_aliases, rel, [*stack, child.name]
                )
            )
        else:
            found.update(
                _scan_module_functions(
                    child, cs_aliases, sl_aliases, ts_aliases, rel, stack
                )
            )
    return found


def test_hits_equal_approved_union_baseline_two_directional() -> None:
    hits = set(find_out_of_band_writers(ROOT))
    expected = APPROVED | set(BASELINE)

    new_hits = hits - expected
    assert not new_hits, (
        "New writer(s) open their own create_session() session and commit "
        "it without being declared: "
        + ", ".join(sorted(new_hits))
        + ". Either declare TransactionMode.OUT_OF_BAND_EVIDENCE for the "
        "service with an approving ADR (see ADR 0017 and "
        "tests/architecture/test_out_of_band_evidence_ratchet.py) and add it "
        "to APPROVED above, or classify it in BASELINE above with a reason."
    )

    stale_baseline = set(BASELINE) - hits
    assert not stale_baseline, (
        "BASELINE entry no longer matches the scanner and must be removed "
        "so this ratchet only shrinks: " + ", ".join(sorted(stale_baseline))
    )


def test_approved_and_baseline_do_not_overlap() -> None:
    overlap = APPROVED & set(BASELINE)
    assert not overlap, "Entries listed in both APPROVED and BASELINE: " + ", ".join(
        sorted(overlap)
    )


def test_every_approved_entry_is_registered_out_of_band_evidence() -> None:
    from app.services.sot_manifest import TransactionMode
    from app.services.sot_registry.registry import all_services

    mode = TransactionMode.OUT_OF_BAND_EVIDENCE.value
    registered_modules_by_mode = {
        service.module: (
            service.contract.transaction.mode.value if service.contract else None
        )
        for service in all_services()
    }

    for entry in APPROVED:
        module_path, _, _qualname = entry.partition("::")
        dotted_module = (
            module_path[: -len(".py")].replace("/", ".")
            if module_path.endswith(".py")
            else module_path.replace("/", ".")
        )
        actual_mode = registered_modules_by_mode.get(dotted_module)
        assert actual_mode == mode, (
            f"{entry}: registry module {dotted_module!r} has transaction mode "
            f"{actual_mode!r}, expected {mode!r} to justify APPROVED membership"
        )


class TestScannerSensitivity:
    """A guard that never fires proves nothing: plant a hit and a near-miss."""

    def test_a_planted_create_session_plus_commit_function_is_flagged(
        self, tmp_path: Path
    ) -> None:
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "sneaky_writer.py").write_text(
            "from app.services.db_session_adapter import db_session_adapter\n"
            "\n"
            "\n"
            "def write_something(x):\n"
            "    s = db_session_adapter.create_session()\n"
            "    s.add(x)\n"
            "    s.commit()\n",
            encoding="utf-8",
        )

        hits = find_out_of_band_writers(tmp_path)

        assert "app/services/sneaky_writer.py::write_something" in hits

    def test_create_session_without_commit_is_not_flagged(self, tmp_path: Path) -> None:
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "reader.py").write_text(
            "from app.services.db_session_adapter import db_session_adapter\n"
            "\n"
            "\n"
            "def read_something():\n"
            "    s = db_session_adapter.create_session()\n"
            "    return s.query(object).first()\n",
            encoding="utf-8",
        )

        hits = find_out_of_band_writers(tmp_path)

        assert "app/services/reader.py::read_something" not in hits

    def test_commit_without_create_session_is_not_flagged(self, tmp_path: Path) -> None:
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "caller_owned.py").write_text(
            "def write_with_caller_session(db, x):\n    db.add(x)\n    db.commit()\n",
            encoding="utf-8",
        )

        hits = find_out_of_band_writers(tmp_path)

        assert "app/services/caller_owned.py::write_with_caller_session" not in hits

    def test_a_planted_sessionlocal_import_plus_commit_function_is_flagged(
        self, tmp_path: Path
    ) -> None:
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "sneaky_sessionlocal.py").write_text(
            "from app.db import SessionLocal\n"
            "\n"
            "\n"
            "def write_something(x):\n"
            "    s = SessionLocal()\n"
            "    s.add(x)\n"
            "    s.commit()\n",
            encoding="utf-8",
        )

        hits_with_shapes = find_out_of_band_writers_with_shapes(tmp_path)

        key = "app/services/sneaky_sessionlocal.py::write_something"
        assert key in hits_with_shapes
        assert hits_with_shapes[key] == frozenset({SHAPE_SESSION_LOCAL_COMMIT})

    def test_sessionlocal_import_without_commit_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "sessionlocal_reader.py").write_text(
            "from app.db import SessionLocal\n"
            "\n"
            "\n"
            "def read_something():\n"
            "    s = SessionLocal()\n"
            "    return s.query(object).first()\n",
            encoding="utf-8",
        )

        hits = find_out_of_band_writers(tmp_path)

        assert "app/services/sessionlocal_reader.py::read_something" not in hits

    def test_a_planted_sessionmaker_bound_factory_plus_commit_function_is_flagged(
        self, tmp_path: Path
    ) -> None:
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "own_factory.py").write_text(
            "from sqlalchemy.orm import sessionmaker\n"
            "\n"
            "_engine = None\n"
            "OwnSessionFactory = sessionmaker(bind=_engine)\n"
            "\n"
            "\n"
            "def write_something(x):\n"
            "    s = OwnSessionFactory()\n"
            "    s.add(x)\n"
            "    s.commit()\n",
            encoding="utf-8",
        )

        hits_with_shapes = find_out_of_band_writers_with_shapes(tmp_path)

        key = "app/services/own_factory.py::write_something"
        assert key in hits_with_shapes
        assert hits_with_shapes[key] == frozenset({SHAPE_SESSION_LOCAL_COMMIT})

    def test_a_planted_with_session_block_is_flagged_with_no_visible_commit(
        self, tmp_path: Path
    ) -> None:
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "sneaky_with_session.py").write_text(
            "from app.services.db_session_adapter import db_session_adapter\n"
            "\n"
            "\n"
            "def write_something(x):\n"
            "    with db_session_adapter.session() as s:\n"
            "        s.add(x)\n",
            encoding="utf-8",
        )

        hits_with_shapes = find_out_of_band_writers_with_shapes(tmp_path)

        key = "app/services/sneaky_with_session.py::write_something"
        assert key in hits_with_shapes
        assert hits_with_shapes[key] == frozenset({SHAPE_AUTO_COMMIT_SESSION})

    def test_with_read_session_block_is_not_flagged(self, tmp_path: Path) -> None:
        """``read_session()`` always rolls back -- the near-miss for ``.session()``."""
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "with_read_session.py").write_text(
            "from app.services.db_session_adapter import db_session_adapter\n"
            "\n"
            "\n"
            "def read_something():\n"
            "    with db_session_adapter.read_session() as s:\n"
            "        return s.query(object).first()\n",
            encoding="utf-8",
        )

        hits = find_out_of_band_writers(tmp_path)

        assert "app/services/with_read_session.py::read_something" not in hits

    def test_with_owner_command_session_block_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """``owner_command_session()`` never commits -- see
        ``app/services/db_session_adapter.py``: it only rolls back
        defensively, so the caller-registered command stays the sole
        transaction owner. The near-miss for ``.advisory_lock()``/``.session()``
        despite the superficially similar "session" name."""
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "with_owner_command_session.py").write_text(
            "from app.services.db_session_adapter import db_session_adapter\n"
            "\n"
            "\n"
            "def write_something(x):\n"
            "    with db_session_adapter.owner_command_session() as s:\n"
            "        s.add(x)\n",
            encoding="utf-8",
        )

        hits = find_out_of_band_writers(tmp_path)

        assert "app/services/with_owner_command_session.py::write_something" not in hits

    def test_a_planted_with_advisory_lock_block_is_flagged(
        self, tmp_path: Path
    ) -> None:
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "sneaky_with_advisory_lock.py").write_text(
            "from app.services.db_session_adapter import db_session_adapter\n"
            "\n"
            "\n"
            "def write_something(x):\n"
            "    with db_session_adapter.advisory_lock(1) as (s, acquired):\n"
            "        if acquired:\n"
            "            s.add(x)\n",
            encoding="utf-8",
        )

        hits_with_shapes = find_out_of_band_writers_with_shapes(tmp_path)

        key = "app/services/sneaky_with_advisory_lock.py::write_something"
        assert key in hits_with_shapes
        assert hits_with_shapes[key] == frozenset({SHAPE_AUTO_COMMIT_ADVISORY_LOCK})

    def test_a_planted_function_local_task_session_import_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """Mirrors the real call sites (e.g.
        ``app/services/dotmac_erp/outbox.py``): the ``task_session`` import
        sits INSIDE the function, not at module level."""
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "sneaky_task_session.py").write_text(
            "def write_something(x):\n"
            "    from app.db import task_session\n"
            "\n"
            "    with task_session() as db:\n"
            "        db.add(x)\n",
            encoding="utf-8",
        )

        hits_with_shapes = find_out_of_band_writers_with_shapes(tmp_path)

        key = "app/services/sneaky_task_session.py::write_something"
        assert key in hits_with_shapes
        assert hits_with_shapes[key] == frozenset({SHAPE_TASK_SESSION_CONTEXT})

    def test_a_planted_module_level_task_session_alias_is_flagged(
        self, tmp_path: Path
    ) -> None:
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "sneaky_task_session_alias.py").write_text(
            "from app.db import task_session as owned_session\n"
            "\n"
            "\n"
            "def write_something(x):\n"
            "    with owned_session() as db:\n"
            "        db.add(x)\n",
            encoding="utf-8",
        )

        hits_with_shapes = find_out_of_band_writers_with_shapes(tmp_path)

        key = "app/services/sneaky_task_session_alias.py::write_something"
        assert key in hits_with_shapes
        assert hits_with_shapes[key] == frozenset({SHAPE_TASK_SESSION_CONTEXT})

    def test_a_planted_sessionlocal_import_alias_plus_commit_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """``from app.db import SessionLocal as X`` -- the aliased-import case."""
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "sneaky_sessionlocal_alias.py").write_text(
            "from app.db import SessionLocal as OwnSession\n"
            "\n"
            "\n"
            "def write_something(x):\n"
            "    s = OwnSession()\n"
            "    s.add(x)\n"
            "    s.commit()\n",
            encoding="utf-8",
        )

        hits_with_shapes = find_out_of_band_writers_with_shapes(tmp_path)

        key = "app/services/sneaky_sessionlocal_alias.py::write_something"
        assert key in hits_with_shapes
        assert hits_with_shapes[key] == frozenset({SHAPE_SESSION_LOCAL_COMMIT})

    def test_a_planted_annotated_sessionmaker_assignment_plus_commit_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """A module-level ANNOTATED assignment (``NAME: T = sessionmaker(...)``),
        not a plain ``Assign`` -- the ``AnnAssign`` branch of
        ``_module_session_local_aliases``."""
        app_dir = tmp_path / "app" / "services"
        app_dir.mkdir(parents=True)
        (app_dir / "sneaky_annotated_factory.py").write_text(
            "from sqlalchemy.orm import sessionmaker, Session\n"
            "\n"
            "_engine = None\n"
            "OwnSessionFactory: type[Session] = sessionmaker(bind=_engine)\n"
            "\n"
            "\n"
            "def write_something(x):\n"
            "    s = OwnSessionFactory()\n"
            "    s.add(x)\n"
            "    s.commit()\n",
            encoding="utf-8",
        )

        hits_with_shapes = find_out_of_band_writers_with_shapes(tmp_path)

        key = "app/services/sneaky_annotated_factory.py::write_something"
        assert key in hits_with_shapes
        assert hits_with_shapes[key] == frozenset({SHAPE_SESSION_LOCAL_COMMIT})


def test_every_known_legacy_out_of_band_writer_is_in_the_baseline() -> None:
    """The ratchet's legacy list and this detector's baseline must agree, so a
    legacy writer can neither be listed without being detected nor detected
    without being listed as legacy."""
    from tests.architecture.test_out_of_band_evidence_ratchet import (
        KNOWN_LEGACY_OUT_OF_BAND_WRITERS,
    )

    missing = sorted(set(KNOWN_LEGACY_OUT_OF_BAND_WRITERS) - set(BASELINE))
    assert not missing, f"legacy writers absent from BASELINE: {missing}"
    legacy_in_baseline = sorted(
        key for key, why in BASELINE.items() if why.startswith("legacy out-of-band")
    )
    assert legacy_in_baseline == sorted(KNOWN_LEGACY_OUT_OF_BAND_WRITERS), (
        "BASELINE entries classified as legacy out-of-band writers must match "
        "KNOWN_LEGACY_OUT_OF_BAND_WRITERS exactly"
    )
