import json
import logging
import os

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.domain_settings import (
    DomainSettings,
    audit_settings,
    auth_settings,
    billing_settings,
    catalog_settings,
    collections_settings,
    comms_settings,
    geocoding_settings,
    gis_settings,
    imports_settings,
    lifecycle_settings,
    network_monitoring_settings,
    network_settings,
    notification_settings,
    provisioning_settings,
    radius_settings,
    scheduler_settings,
    subscriber_settings,
    tr069_settings,
    usage_settings,
)
from app.services.secrets import is_openbao_ref
from app.timezone import APP_TIMEZONE_NAME

logger = logging.getLogger(__name__)


def seed_auth_settings(db: Session) -> None:
    auth_settings.ensure_by_key(
        db,
        key="jwt_algorithm",
        value_type=SettingValueType.string,
        value_text=os.getenv("JWT_ALGORITHM", "HS256"),
    )
    auth_settings.ensure_by_key(
        db,
        key="jwt_access_ttl_minutes",
        value_type=SettingValueType.integer,
        value_text=os.getenv("JWT_ACCESS_TTL_MINUTES", "15"),
    )
    auth_settings.ensure_by_key(
        db,
        key="jwt_refresh_ttl_days",
        value_type=SettingValueType.integer,
        value_text=os.getenv("JWT_REFRESH_TTL_DAYS", "30"),
    )
    auth_settings.ensure_by_key(
        db,
        key="refresh_cookie_name",
        value_type=SettingValueType.string,
        value_text=os.getenv("REFRESH_COOKIE_NAME", "refresh_token"),
    )
    auth_settings.ensure_by_key(
        db,
        key="refresh_cookie_secure",
        value_type=SettingValueType.boolean,
        value_text=os.getenv("REFRESH_COOKIE_SECURE", "false"),
        value_json=os.getenv("REFRESH_COOKIE_SECURE", "false").lower()
        in {"1", "true", "yes", "on"},
    )
    auth_settings.ensure_by_key(
        db,
        key="refresh_cookie_samesite",
        value_type=SettingValueType.string,
        value_text=os.getenv("REFRESH_COOKIE_SAMESITE", "lax"),
    )
    auth_settings.ensure_by_key(
        db,
        key="refresh_cookie_domain",
        value_type=SettingValueType.string,
        value_text=os.getenv("REFRESH_COOKIE_DOMAIN", ""),
    )
    auth_settings.ensure_by_key(
        db,
        key="refresh_cookie_path",
        value_type=SettingValueType.string,
        value_text=os.getenv("REFRESH_COOKIE_PATH", "/auth"),
    )
    auth_settings.ensure_by_key(
        db,
        key="totp_issuer",
        value_type=SettingValueType.string,
        value_text=os.getenv("TOTP_ISSUER", "dotmac_sm"),
    )
    auth_settings.ensure_by_key(
        db,
        key="api_key_rate_window_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("API_KEY_RATE_WINDOW_SECONDS", "60"),
    )
    auth_settings.ensure_by_key(
        db,
        key="api_key_rate_max",
        value_type=SettingValueType.integer,
        value_text=os.getenv("API_KEY_RATE_MAX", "5"),
    )
    jwt_secret = os.getenv("JWT_SECRET")
    if jwt_secret and is_openbao_ref(jwt_secret):
        auth_settings.ensure_by_key(
            db,
            key="jwt_secret",
            value_type=SettingValueType.string,
            value_text=jwt_secret,
            is_secret=True,
        )
    totp_key = os.getenv("TOTP_ENCRYPTION_KEY")
    if totp_key and is_openbao_ref(totp_key):
        auth_settings.ensure_by_key(
            db,
            key="totp_encryption_key",
            value_type=SettingValueType.string,
            value_text=totp_key,
            is_secret=True,
        )


def seed_audit_settings(db: Session) -> None:
    audit_settings.ensure_by_key(
        db,
        key="enabled",
        value_type=SettingValueType.boolean,
        value_text="true",
        value_json=True,
    )
    audit_settings.ensure_by_key(
        db,
        key="methods",
        value_type=SettingValueType.json,
        value_json=["POST", "PUT", "PATCH", "DELETE"],
    )
    audit_settings.ensure_by_key(
        db,
        key="skip_paths",
        value_type=SettingValueType.json,
        value_json=["/static", "/web", "/health"],
    )
    audit_settings.ensure_by_key(
        db,
        key="read_trigger_header",
        value_type=SettingValueType.string,
        value_text="x-audit-read",
    )
    audit_settings.ensure_by_key(
        db,
        key="read_trigger_query",
        value_type=SettingValueType.string,
        value_text="audit",
    )


def seed_imports_settings(db: Session) -> None:
    imports_settings.ensure_by_key(
        db,
        key="max_file_bytes",
        value_type=SettingValueType.integer,
        value_text=str(5 * 1024 * 1024),
    )
    imports_settings.ensure_by_key(
        db,
        key="max_rows",
        value_type=SettingValueType.integer,
        value_text="5000",
    )
    imports_settings.ensure_by_key(
        db,
        key="import_history_log",
        value_type=SettingValueType.json,
        value_json=[],
    )
    imports_settings.ensure_by_key(
        db,
        key="import_rollback_window_hours",
        value_type=SettingValueType.integer,
        value_text=os.getenv("IMPORT_ROLLBACK_WINDOW_HOURS", "24"),
    )
    imports_settings.ensure_by_key(
        db,
        key="import_background_threshold_rows",
        value_type=SettingValueType.integer,
        value_text=os.getenv("IMPORT_BACKGROUND_THRESHOLD_ROWS", "1000"),
    )
    imports_settings.ensure_by_key(
        db,
        key="import_jobs_log",
        value_type=SettingValueType.json,
        value_json=[],
    )


def seed_gis_settings(db: Session) -> None:
    gis_settings.ensure_by_key(
        db,
        key="sync_enabled",
        value_type=SettingValueType.boolean,
        value_text="true",
        value_json=True,
    )
    gis_settings.ensure_by_key(
        db,
        key="sync_interval_minutes",
        value_type=SettingValueType.integer,
        value_text="60",
    )
    gis_settings.ensure_by_key(
        db,
        key="sync_pop_sites",
        value_type=SettingValueType.boolean,
        value_text="true",
        value_json=True,
    )
    gis_settings.ensure_by_key(
        db,
        key="sync_addresses",
        value_type=SettingValueType.boolean,
        value_text="true",
        value_json=True,
    )
    gis_settings.ensure_by_key(
        db,
        key="sync_deactivate_missing",
        value_type=SettingValueType.boolean,
        value_text="false",
        value_json=False,
    )
    gis_settings.ensure_by_key(
        db,
        key="map_customer_limit",
        value_type=SettingValueType.integer,
        value_text="2000",
    )
    gis_settings.ensure_by_key(
        db,
        key="map_nearest_search_max_km",
        value_type=SettingValueType.integer,
        value_text="50",
    )
    gis_settings.ensure_by_key(
        db,
        key="map_snap_max_m",
        value_type=SettingValueType.integer,
        value_text="250",
    )
    gis_settings.ensure_by_key(
        db,
        key="map_allow_straightline_fallback",
        value_type=SettingValueType.boolean,
        value_text="false",
    )


def seed_usage_settings(db: Session) -> None:
    enabled_raw = os.getenv("USAGE_RATING_ENABLED", "true")
    usage_settings.ensure_by_key(
        db,
        key="usage_rating_enabled",
        value_type=SettingValueType.boolean,
        value_text=enabled_raw,
        value_json=enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    usage_settings.ensure_by_key(
        db,
        key="usage_rating_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("USAGE_RATING_INTERVAL_SECONDS", "86400"),
    )
    accounting_enabled_raw = os.getenv("RADIUS_ACCOUNTING_IMPORT_ENABLED", "true")
    usage_settings.ensure_by_key(
        db,
        key="radius_accounting_import_enabled",
        value_type=SettingValueType.boolean,
        value_text=accounting_enabled_raw,
        value_json=accounting_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    usage_settings.ensure_by_key(
        db,
        key="radius_accounting_import_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("RADIUS_ACCOUNTING_IMPORT_INTERVAL_SECONDS", "60"),
    )
    warning_enabled_raw = os.getenv("USAGE_WARNING_ENABLED", "true")
    usage_settings.ensure_by_key(
        db,
        key="usage_warning_enabled",
        value_type=SettingValueType.boolean,
        value_text=warning_enabled_raw,
        value_json=warning_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    usage_settings.ensure_by_key(
        db,
        key="usage_warning_thresholds",
        value_type=SettingValueType.string,
        value_text=os.getenv("USAGE_WARNING_THRESHOLDS", "0.8,0.9"),
    )
    usage_settings.ensure_by_key(
        db,
        key="fup_throttle_radius_profile_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("USAGE_FUP_THROTTLE_RADIUS_PROFILE_ID", ""),
    )
    usage_settings.ensure_by_key(
        db,
        key="fup_action",
        value_type=SettingValueType.string,
        value_text=os.getenv("USAGE_FUP_ACTION", "throttle"),
    )


def seed_notification_settings(db: Session) -> None:
    enabled_raw = os.getenv("ALERT_NOTIFICATIONS_ENABLED", "true")
    notification_settings.ensure_by_key(
        db,
        key="alert_notifications_enabled",
        value_type=SettingValueType.boolean,
        value_text=enabled_raw,
        value_json=enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    notification_settings.ensure_by_key(
        db,
        key="alert_notifications_default_channel",
        value_type=SettingValueType.string,
        value_text=os.getenv("ALERT_NOTIFICATIONS_DEFAULT_CHANNEL", "email"),
    )
    notification_settings.ensure_by_key(
        db,
        key="alert_notifications_default_recipient",
        value_type=SettingValueType.string,
        value_text=os.getenv("ALERT_NOTIFICATIONS_DEFAULT_RECIPIENT", ""),
    )
    notification_settings.ensure_by_key(
        db,
        key="alert_notifications_default_template_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("ALERT_NOTIFICATIONS_DEFAULT_TEMPLATE_ID", ""),
    )
    notification_settings.ensure_by_key(
        db,
        key="alert_notifications_default_rotation_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("ALERT_NOTIFICATIONS_DEFAULT_ROTATION_ID", ""),
    )
    notification_settings.ensure_by_key(
        db,
        key="alert_notifications_default_delay_minutes",
        value_type=SettingValueType.integer,
        value_text=os.getenv("ALERT_NOTIFICATIONS_DEFAULT_DELAY_MINUTES", "0"),
    )
    queue_enabled_raw = os.getenv("NOTIFICATION_QUEUE_ENABLED", "true")
    notification_settings.ensure_by_key(
        db,
        key="notification_queue_enabled",
        value_type=SettingValueType.boolean,
        value_text=queue_enabled_raw,
        value_json=queue_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    notification_settings.ensure_by_key(
        db,
        key="notification_queue_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("NOTIFICATION_QUEUE_INTERVAL_SECONDS", "60"),
    )


def _seed_missing_notification_templates(db: Session) -> int:
    """Insert any missing default notification templates without committing."""
    from app.models.notification import NotificationChannel, NotificationTemplate

    templates = [
        {
            "code": "subscription_created",
            "name": "Subscription Created",
            "channel": NotificationChannel.email,
            "subject": "Your new service subscription",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your subscription to {offer_name} has been created. "
                "A service order will be created for installation.\n\n"
                "If you have questions, contact our support team.\n\n"
                "Thank you for choosing us."
            ),
        },
        {
            "code": "subscription_created",
            "name": "Subscription Created SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Hi {subscriber_name}, your {offer_name} subscription has been created. "
                "We will contact you shortly with the next steps."
            ),
        },
        {
            "code": "subscription_activated",
            "name": "Subscription Activated",
            "channel": NotificationChannel.email,
            "subject": "Your service is now active",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your {offer_name} subscription is now active. "
                "You can start using your service immediately.\n\n"
                "Your service credentials have been sent separately.\n\n"
                "Welcome aboard!"
            ),
        },
        {
            "code": "subscription_activated",
            "name": "Subscription Activated SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Hi {subscriber_name}, your {offer_name} service is now active. "
                "If you need help, contact support."
            ),
        },
        {
            "code": "subscription_suspended",
            "name": "Subscription Suspended",
            "channel": NotificationChannel.email,
            "subject": "Service suspended — action required",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your {offer_name} subscription has been suspended. "
                "This may be due to an outstanding balance or a policy violation.\n\n"
                "Please contact our support team or make a payment to restore your service.\n\n"
                "Thank you."
            ),
        },
        {
            "code": "subscription_suspended",
            "name": "Subscription Suspended SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Hi {subscriber_name}, your {offer_name} service is suspended. "
                "Please pay outstanding invoices or contact support."
            ),
        },
        {
            "code": "suspension_warning",
            "name": "Suspension Warning",
            "channel": NotificationChannel.email,
            "subject": "Payment reminder — suspension in {grace_hours} hours",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Invoice #{invoice_number} for {amount} is overdue. "
                "Your service may be suspended if payment is not received within "
                "{grace_hours} hours.\n\n"
                "Pay now: {portal_url}/billing\n\n"
                "If you have already paid, please disregard this message."
            ),
        },
        {
            "code": "suspension_warning",
            "name": "Suspension Warning SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Reminder: invoice #{invoice_number} for {amount} is overdue. "
                "Pay within {grace_hours} hours to avoid suspension. {portal_url}/billing"
            ),
        },
        {
            "code": "subscription_canceled",
            "name": "Subscription Canceled",
            "channel": NotificationChannel.email,
            "subject": "Subscription canceled",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your {offer_name} subscription has been canceled. "
                "If this was not expected, please contact our support team.\n\n"
                "We hope to serve you again in the future."
            ),
        },
        {
            "code": "subscription_expiring",
            "name": "Subscription Expiring",
            "channel": NotificationChannel.email,
            "subject": "Your subscription is expiring soon",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your {offer_name} subscription will expire soon. "
                "Please renew to avoid service interruption.\n\n"
                "You can renew by making a payment through our customer portal "
                "or by contacting our support team.\n\n"
                "Thank you."
            ),
        },
        {
            "code": "subscription_expiring",
            "name": "Subscription Expiring SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Hi {subscriber_name}, your {offer_name} subscription expires soon. "
                "Please renew to avoid interruption."
            ),
        },
        {
            "code": "invoice_created",
            "name": "Invoice Created",
            "channel": NotificationChannel.email,
            "subject": "New invoice #{invoice_number}",
            "body": (
                "Dear {subscriber_name},\n\n"
                "A new invoice #{invoice_number} for {amount} has been generated "
                "for your {offer_name} subscription.\n\n"
                "Due date: {due_date}\n\n"
                "You can view and pay this invoice through our customer portal.\n\n"
                "Thank you."
            ),
        },
        {
            "code": "invoice_sent",
            "name": "Invoice Sent",
            "channel": NotificationChannel.email,
            "subject": "Invoice #{invoice_number} — payment due {due_date}",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Invoice #{invoice_number} for {amount} is due on {due_date}.\n\n"
                "Please make your payment before the due date to avoid "
                "service interruption.\n\n"
                "Pay online: {portal_url}/billing\n\n"
                "Thank you."
            ),
        },
        {
            "code": "invoice_sent",
            "name": "Invoice Sent SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Invoice #{invoice_number} for {amount} is due on {due_date}. "
                "Pay online: {portal_url}/billing"
            ),
        },
        {
            "code": "invoice_overdue",
            "name": "Invoice Overdue",
            "channel": NotificationChannel.email,
            "subject": "Overdue invoice #{invoice_number} — immediate payment required",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Invoice #{invoice_number} for {amount} is now overdue. "
                "Your service may be suspended if payment is not received promptly.\n\n"
                "Please make your payment immediately to avoid disruption.\n\n"
                "Pay online: {portal_url}/billing\n\n"
                "If you have already paid, please disregard this notice."
            ),
        },
        {
            "code": "invoice_overdue",
            "name": "Invoice Overdue SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Invoice #{invoice_number} for {amount} is overdue. "
                "Pay now to avoid service disruption: {portal_url}/billing"
            ),
        },
        {
            "code": "payment_received",
            "name": "Payment Received",
            "channel": NotificationChannel.email,
            "subject": "Payment received — thank you",
            "body": (
                "Dear {subscriber_name},\n\n"
                "We have received your payment of {amount}. Thank you!\n\n"
                "Your account balance has been updated accordingly.\n\n"
                "If you have questions about your billing, please contact support."
            ),
        },
        {
            "code": "payment_received",
            "name": "Payment Received SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": ("We received your payment of {amount}. Thank you."),
        },
        {
            "code": "payment_failed",
            "name": "Payment Failed",
            "channel": NotificationChannel.email,
            "subject": "Payment failed — please retry",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your recent payment attempt of {amount} was not successful. "
                "Please try again or use a different payment method.\n\n"
                "If you continue to experience issues, contact our support team.\n\n"
                "Thank you."
            ),
        },
        {
            "code": "usage_warning",
            "name": "Usage Warning",
            "channel": NotificationChannel.email,
            "subject": "Data usage warning — {usage_percent}% used",
            "body": (
                "Dear {subscriber_name},\n\n"
                "You have used {usage_percent}% of your monthly data allowance "
                "on your {offer_name} plan.\n\n"
                "Consider upgrading your plan if you need more data.\n\n"
                "Thank you."
            ),
        },
        {
            "code": "usage_exhausted",
            "name": "Usage Exhausted",
            "channel": NotificationChannel.email,
            "subject": "Data allowance exhausted",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your monthly data allowance on the {offer_name} plan has been exhausted. "
                "Your speed may be reduced until the next billing cycle.\n\n"
                "To restore full speed, consider upgrading your plan or purchasing a top-up.\n\n"
                "Thank you."
            ),
        },
        {
            "code": "provisioning_completed",
            "name": "Provisioning Completed",
            "channel": NotificationChannel.email,
            "subject": "Service installation complete",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your service installation has been completed successfully. "
                "Your {offer_name} subscription is now ready to use.\n\n"
                "If you experience any issues, please contact our support team.\n\n"
                "Enjoy your service!"
            ),
        },
        {
            "code": "provisioning_completed",
            "name": "Provisioning Completed SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Your {offer_name} installation is complete and the service is ready."
            ),
        },
        {
            "code": "provisioning_failed",
            "name": "Provisioning Failed",
            "channel": NotificationChannel.email,
            "subject": "Service installation issue",
            "body": (
                "Dear {subscriber_name},\n\n"
                "We encountered an issue while setting up your {offer_name} subscription. "
                "Our technical team has been notified and will follow up shortly.\n\n"
                "We apologize for the inconvenience."
            ),
        },
        {
            "code": "ont_offline",
            "name": "ONT Offline Alert",
            "channel": NotificationChannel.email,
            "subject": "Network device offline — {device_serial}",
            "body": (
                "ONT {device_serial} has gone offline.\n\n"
                "Subscriber: {subscriber_name}\n"
                "Location: {location}\n\n"
                "Please investigate the connectivity issue."
            ),
        },
        {
            "code": "ont_online",
            "name": "ONT Online Alert",
            "channel": NotificationChannel.email,
            "subject": "Network device back online — {device_serial}",
            "body": (
                "ONT {device_serial} is back online.\n\n"
                "Subscriber: {subscriber_name}\n"
                "Downtime resolved."
            ),
        },
        {
            "code": "ont_signal_degraded",
            "name": "ONT Signal Degraded",
            "channel": NotificationChannel.email,
            "subject": "Fiber signal degraded — {device_serial}",
            "body": (
                "ONT {device_serial} is reporting degraded optical signal levels.\n\n"
                "Subscriber: {subscriber_name}\n"
                "Signal level: {signal_level} dBm\n\n"
                "Please check the fiber path for potential issues."
            ),
        },
        {
            "code": "ont_discovered",
            "name": "ONT Discovered",
            "channel": NotificationChannel.email,
            "subject": "New ONT discovered — {device_serial}",
            "body": (
                "A new ONT has been discovered on the network.\n\n"
                "Serial: {device_serial}\n"
                "OLT: {olt_name}\n"
                "PON Port: {pon_port}\n\n"
                "This device is awaiting assignment to a subscriber."
            ),
        },
    ]

    created = 0
    for tmpl_data in templates:
        from sqlalchemy import select as sa_select

        existing = db.scalars(
            sa_select(NotificationTemplate).where(
                NotificationTemplate.code == tmpl_data["code"],
                NotificationTemplate.channel == tmpl_data["channel"],
            )
        ).first()
        if not existing:
            tmpl = NotificationTemplate(**tmpl_data)
            db.add(tmpl)
            created += 1
            logger.info("Seeded notification template: %s", tmpl_data["code"])

    db.flush()
    return created


def seed_notification_templates(db: Session) -> None:
    """Seed default notification templates for key ISP events.

    Uses upsert-by-code-and-channel: creates if missing, skips if already exists
    (admin may have customized the content).
    """
    _seed_missing_notification_templates(db)
    db.commit()


def seed_collections_settings(db: Session) -> None:
    enabled_raw = os.getenv("DUNNING_ENABLED", "true")
    collections_settings.ensure_by_key(
        db,
        key="dunning_enabled",
        value_type=SettingValueType.boolean,
        value_text=enabled_raw,
        value_json=enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    collections_settings.ensure_by_key(
        db,
        key="dunning_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("DUNNING_INTERVAL_SECONDS", "86400"),
    )
    prepaid_enabled_raw = os.getenv("PREPAID_ENFORCEMENT_ENABLED", "true")
    collections_settings.ensure_by_key(
        db,
        key="prepaid_enforcement_enabled",
        value_type=SettingValueType.boolean,
        value_text=prepaid_enabled_raw,
        value_json=prepaid_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_enforcement_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("PREPAID_ENFORCEMENT_INTERVAL_SECONDS", "3600"),
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_blocking_time",
        value_type=SettingValueType.string,
        value_text=os.getenv("PREPAID_BLOCKING_TIME", "08:00"),
    )
    prepaid_skip_weekends_raw = os.getenv("PREPAID_SKIP_WEEKENDS", "false")
    collections_settings.ensure_by_key(
        db,
        key="prepaid_skip_weekends",
        value_type=SettingValueType.boolean,
        value_text=prepaid_skip_weekends_raw,
        value_json=prepaid_skip_weekends_raw.lower() in {"1", "true", "yes", "on"},
    )
    prepaid_skip_holidays_raw = os.getenv("PREPAID_SKIP_HOLIDAYS", "[]")
    try:
        prepaid_skip_holidays_value = json.loads(prepaid_skip_holidays_raw)
    except json.JSONDecodeError:
        prepaid_skip_holidays_value = []
    collections_settings.ensure_by_key(
        db,
        key="prepaid_skip_holidays",
        value_type=SettingValueType.json,
        value_json=prepaid_skip_holidays_value,
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_grace_days",
        value_type=SettingValueType.integer,
        value_text=os.getenv("PREPAID_GRACE_DAYS", "0"),
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_deactivation_days",
        value_type=SettingValueType.integer,
        value_text=os.getenv("PREPAID_DEACTIVATION_DAYS", "0"),
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_default_min_balance",
        value_type=SettingValueType.string,
        value_text=os.getenv("PREPAID_DEFAULT_MIN_BALANCE", "0.00"),
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_warning_subject",
        value_type=SettingValueType.string,
        value_text=os.getenv("PREPAID_WARNING_SUBJECT", "Low Balance Warning"),
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_warning_body",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "PREPAID_WARNING_BODY",
            "Your prepaid balance is below the minimum threshold ({threshold}). "
            "Current balance: {balance}. Please top up to avoid suspension.",
        ),
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_deactivation_subject",
        value_type=SettingValueType.string,
        value_text=os.getenv("PREPAID_DEACTIVATION_SUBJECT", "Service Deactivated"),
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_deactivation_body",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "PREPAID_DEACTIVATION_BODY",
            "Your prepaid balance has been exhausted and service has been deactivated. "
            "Please contact support to restore service.",
        ),
    )


def seed_geocoding_settings(db: Session) -> None:
    geocoding_settings.ensure_by_key(
        db,
        key="enabled",
        value_type=SettingValueType.boolean,
        value_text="true",
        value_json=True,
    )
    geocoding_settings.ensure_by_key(
        db,
        key="provider",
        value_type=SettingValueType.string,
        value_text=os.getenv("GEOCODING_PROVIDER", "nominatim"),
    )
    geocoding_settings.ensure_by_key(
        db,
        key="base_url",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "GEOCODING_BASE_URL", "https://nominatim.openstreetmap.org"
        ),
    )
    geocoding_settings.ensure_by_key(
        db,
        key="user_agent",
        value_type=SettingValueType.string,
        value_text=os.getenv("GEOCODING_USER_AGENT", "dotmac_sm"),
    )
    geocoding_settings.ensure_by_key(
        db,
        key="email",
        value_type=SettingValueType.string,
        value_text=os.getenv("GEOCODING_EMAIL", ""),
    )
    geocoding_settings.ensure_by_key(
        db,
        key="timeout_sec",
        value_type=SettingValueType.integer,
        value_text=os.getenv("GEOCODING_TIMEOUT_SEC", "5"),
    )
    geocoding_settings.ensure_by_key(
        db,
        key="batch_geocode_jobs_log",
        value_type=SettingValueType.json,
        value_json=[],
    )
    geocoding_settings.ensure_by_key(
        db,
        key="batch_geocode_log_rows",
        value_type=SettingValueType.json,
        value_json=[],
    )


def seed_scheduler_settings(db: Session) -> None:
    broker = (
        os.getenv("CELERY_BROKER_URL")
        or os.getenv("REDIS_URL")
        or "redis://localhost:6379/0"
    )
    backend = (
        os.getenv("CELERY_RESULT_BACKEND")
        or os.getenv("REDIS_URL")
        or "redis://localhost:6379/1"
    )
    scheduler_settings.ensure_by_key(
        db,
        key="broker_url",
        value_type=SettingValueType.string,
        value_text=broker,
    )
    scheduler_settings.ensure_by_key(
        db,
        key="result_backend",
        value_type=SettingValueType.string,
        value_text=backend,
    )
    scheduler_settings.ensure_by_key(
        db,
        key="timezone",
        value_type=SettingValueType.string,
        value_text=os.getenv("CELERY_TIMEZONE", APP_TIMEZONE_NAME),
    )
    scheduler_settings.ensure_by_key(
        db,
        key="beat_max_loop_interval",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CELERY_BEAT_MAX_LOOP_INTERVAL", "5"),
    )
    scheduler_settings.ensure_by_key(
        db,
        key="beat_refresh_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CELERY_BEAT_REFRESH_SECONDS", "30"),
    )
    scheduler_settings.ensure_by_key(
        db,
        key="refresh_minutes",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CELERY_BEAT_REFRESH_MINUTES", "5"),
    )


def seed_radius_settings(db: Session) -> None:
    radius_settings.ensure_by_key(
        db,
        key="auth_server_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_AUTH_SERVER_ID", ""),
    )
    radius_settings.ensure_by_key(
        db,
        key="auth_shared_secret",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_AUTH_SHARED_SECRET", ""),
        is_secret=True,
    )
    radius_settings.ensure_by_key(
        db,
        key="auth_dictionary_path",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_AUTH_DICTIONARY", "/etc/raddb/dictionary"),
    )
    radius_settings.ensure_by_key(
        db,
        key="auth_timeout_sec",
        value_type=SettingValueType.integer,
        value_text=os.getenv("RADIUS_AUTH_TIMEOUT_SEC", "3"),
    )
    coa_enabled_raw = os.getenv("RADIUS_COA_ENABLED", "true")
    radius_settings.ensure_by_key(
        db,
        key="coa_enabled",
        value_type=SettingValueType.boolean,
        value_text=coa_enabled_raw,
        value_json=coa_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    radius_settings.ensure_by_key(
        db,
        key="coa_dictionary_path",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_COA_DICTIONARY", "/etc/raddb/dictionary"),
    )
    radius_settings.ensure_by_key(
        db,
        key="coa_timeout_sec",
        value_type=SettingValueType.integer,
        value_text=os.getenv("RADIUS_COA_TIMEOUT_SEC", "3"),
    )
    radius_settings.ensure_by_key(
        db,
        key="coa_retries",
        value_type=SettingValueType.integer,
        value_text=os.getenv("RADIUS_COA_RETRIES", "1"),
    )
    refresh_raw = os.getenv("RADIUS_REFRESH_SESSIONS_ON_PROFILE_CHANGE", "true")
    radius_settings.ensure_by_key(
        db,
        key="refresh_sessions_on_profile_change",
        value_type=SettingValueType.boolean,
        value_text=refresh_raw,
        value_json=refresh_raw.lower() in {"1", "true", "yes", "on"},
    )
    # PPPoE auto-generation settings (disabled by default for dual-run)
    # pppoe_auto_generate_enabled removed — PPPoE generation is now mandatory
    radius_settings.ensure_by_key(
        db,
        key="pppoe_username_prefix",
        value_type=SettingValueType.string,
        value_text=os.getenv("PPPOE_USERNAME_PREFIX", "1050"),
    )
    radius_settings.ensure_by_key(
        db,
        key="pppoe_username_padding",
        value_type=SettingValueType.integer,
        value_text=os.getenv("PPPOE_USERNAME_PADDING", "5"),
    )
    radius_settings.ensure_by_key(
        db,
        key="pppoe_username_start",
        value_type=SettingValueType.integer,
        value_text=os.getenv("PPPOE_USERNAME_START", "1"),
    )
    radius_settings.ensure_by_key(
        db,
        key="pppoe_default_password_length",
        value_type=SettingValueType.integer,
        value_text=os.getenv("PPPOE_DEFAULT_PASSWORD_LENGTH", "12"),
    )
    # Captive portal redirect settings
    captive_enabled_raw = os.getenv("RADIUS_CAPTIVE_REDIRECT_ENABLED", "false")
    radius_settings.ensure_by_key(
        db,
        key="captive_redirect_enabled",
        value_type=SettingValueType.boolean,
        value_text=captive_enabled_raw,
        value_json=captive_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    radius_settings.ensure_by_key(
        db,
        key="captive_portal_ip",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_CAPTIVE_PORTAL_IP", ""),
    )
    radius_settings.ensure_by_key(
        db,
        key="captive_portal_url",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_CAPTIVE_PORTAL_URL", ""),
    )


def seed_billing_settings(db: Session) -> None:
    billing_settings.ensure_by_key(
        db,
        key="default_currency",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DEFAULT_CURRENCY", "NGN"),
    )
    billing_settings.ensure_by_key(
        db,
        key="default_invoice_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DEFAULT_INVOICE_STATUS", "draft"),
    )
    billing_settings.ensure_by_key(
        db,
        key="default_tax_application",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DEFAULT_TAX_APPLICATION", "exclusive"),
    )
    billing_settings.ensure_by_key(
        db,
        key="default_payment_method_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DEFAULT_PAYMENT_METHOD_TYPE", "card"),
    )
    billing_settings.ensure_by_key(
        db,
        key="default_payment_provider_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DEFAULT_PAYMENT_PROVIDER_TYPE", "paystack"),
    )
    payment_failover_enabled_raw = os.getenv(
        "BILLING_PAYMENT_GATEWAY_FAILOVER_ENABLED", "true"
    )
    billing_settings.ensure_by_key(
        db,
        key="payment_gateway_failover_enabled",
        value_type=SettingValueType.boolean,
        value_text=payment_failover_enabled_raw,
        value_json=payment_failover_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    billing_settings.ensure_by_key(
        db,
        key="payment_gateway_primary_provider",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_PAYMENT_GATEWAY_PRIMARY_PROVIDER", "paystack"),
    )
    billing_settings.ensure_by_key(
        db,
        key="payment_gateway_secondary_provider",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "BILLING_PAYMENT_GATEWAY_SECONDARY_PROVIDER", "flutterwave"
        ),
    )
    billing_settings.ensure_by_key(
        db,
        key="default_bank_account_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DEFAULT_BANK_ACCOUNT_TYPE", "checking"),
    )
    billing_settings.ensure_by_key(
        db,
        key="default_payment_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DEFAULT_PAYMENT_STATUS", "pending"),
    )
    billing_enabled_raw = os.getenv("BILLING_ENABLED", "true")
    billing_settings.ensure_by_key(
        db,
        key="billing_enabled",
        value_type=SettingValueType.boolean,
        value_text=billing_enabled_raw,
    )
    billing_settings.ensure_by_key(
        db,
        key="billing_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_INTERVAL_SECONDS", "86400"),
    )
    billing_settings.ensure_by_key(
        db,
        key="payment_due_days",
        value_type=SettingValueType.integer,
        value_text=os.getenv(
            "BILLING_PAYMENT_DUE_DAYS",
            os.getenv("BILLING_INVOICE_DUE_DAYS", "14"),
        ),
    )
    auto_suspend_raw = os.getenv("BILLING_AUTO_SUSPEND_ON_OVERDUE", "true")
    billing_settings.ensure_by_key(
        db,
        key="auto_suspend_on_overdue",
        value_type=SettingValueType.boolean,
        value_text=auto_suspend_raw,
        value_json=auto_suspend_raw.lower() in {"1", "true", "yes", "on"},
    )
    billing_settings.ensure_by_key(
        db,
        key="suspension_grace_hours",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_SUSPENSION_GRACE_HOURS", "48"),
    )
    billing_settings.ensure_by_key(
        db,
        key="expiry_reminder_days",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_EXPIRY_REMINDER_DAYS", "7"),
    )
    billing_settings.ensure_by_key(
        db,
        key="invoice_reminder_days",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_INVOICE_REMINDER_DAYS", "7,1"),
    )
    billing_settings.ensure_by_key(
        db,
        key="dunning_escalation_days",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DUNNING_ESCALATION_DAYS", "3,7,14,30"),
    )
    billing_settings.ensure_by_key(
        db,
        key="refund_policy",
        value_type=SettingValueType.string,
        value_text=os.getenv("PLAN_CHANGE_REFUND_POLICY", "none"),
    )
    billing_settings.ensure_by_key(
        db,
        key="upgrade_fee",
        value_type=SettingValueType.string,
        value_text=os.getenv("PLAN_CHANGE_UPGRADE_FEE", "0.00"),
    )
    billing_settings.ensure_by_key(
        db,
        key="downgrade_fee",
        value_type=SettingValueType.string,
        value_text=os.getenv("PLAN_CHANGE_DOWNGRADE_FEE", "0.00"),
    )
    billing_settings.ensure_by_key(
        db,
        key="fee_tax_rate",
        value_type=SettingValueType.string,
        value_text=os.getenv("PLAN_CHANGE_FEE_TAX_RATE", "0.00"),
    )
    billing_settings.ensure_by_key(
        db,
        key="invoice_timing",
        value_type=SettingValueType.string,
        value_text=os.getenv("PLAN_CHANGE_INVOICE_TIMING", "immediate"),
    )
    prepaid_rollover_raw = os.getenv("PLAN_CHANGE_PREPAID_ROLLOVER", "false")
    billing_settings.ensure_by_key(
        db,
        key="prepaid_rollover",
        value_type=SettingValueType.boolean,
        value_text=prepaid_rollover_raw,
        value_json=prepaid_rollover_raw.lower() in {"1", "true", "yes", "on"},
    )
    discount_transfer_raw = os.getenv("PLAN_CHANGE_DISCOUNT_TRANSFER", "false")
    billing_settings.ensure_by_key(
        db,
        key="discount_transfer",
        value_type=SettingValueType.boolean,
        value_text=discount_transfer_raw,
        value_json=discount_transfer_raw.lower() in {"1", "true", "yes", "on"},
    )
    billing_settings.ensure_by_key(
        db,
        key="minimum_invoice_amount",
        value_type=SettingValueType.string,
        value_text=os.getenv("PLAN_CHANGE_MINIMUM_INVOICE_AMOUNT", "0.00"),
    )
    invoice_enabled_raw = os.getenv("BILLING_INVOICE_NUMBER_ENABLED", "true")
    billing_settings.ensure_by_key(
        db,
        key="invoice_number_enabled",
        value_type=SettingValueType.boolean,
        value_text=invoice_enabled_raw,
        value_json=invoice_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    billing_settings.ensure_by_key(
        db,
        key="invoice_number_prefix",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_INVOICE_NUMBER_PREFIX", "INV-"),
    )
    billing_settings.ensure_by_key(
        db,
        key="invoice_number_padding",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_INVOICE_NUMBER_PADDING", "6"),
    )
    billing_settings.ensure_by_key(
        db,
        key="invoice_number_start",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_INVOICE_NUMBER_START", "1"),
    )
    credit_note_enabled_raw = os.getenv("BILLING_CREDIT_NOTE_NUMBER_ENABLED", "true")
    billing_settings.ensure_by_key(
        db,
        key="credit_note_number_enabled",
        value_type=SettingValueType.boolean,
        value_text=credit_note_enabled_raw,
        value_json=credit_note_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    billing_settings.ensure_by_key(
        db,
        key="credit_note_number_prefix",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_CREDIT_NOTE_NUMBER_PREFIX", "CR-"),
    )
    billing_settings.ensure_by_key(
        db,
        key="credit_note_number_padding",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_CREDIT_NOTE_NUMBER_PADDING", "6"),
    )
    billing_settings.ensure_by_key(
        db,
        key="credit_note_number_start",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_CREDIT_NOTE_NUMBER_START", "1"),
    )
    billing_settings.ensure_by_key(
        db,
        key="paystack_secret_key",
        value_type=SettingValueType.string,
        value_text=os.getenv("PAYSTACK_SECRET_KEY", ""),
        is_secret=True,
    )
    billing_settings.ensure_by_key(
        db,
        key="paystack_public_key",
        value_type=SettingValueType.string,
        value_text=os.getenv("PAYSTACK_PUBLIC_KEY", ""),
    )
    # Flutterwave settings
    billing_settings.ensure_by_key(
        db,
        key="flutterwave_secret_key",
        value_type=SettingValueType.string,
        value_text=os.getenv("FLUTTERWAVE_SECRET_KEY", ""),
        is_secret=True,
    )
    billing_settings.ensure_by_key(
        db,
        key="flutterwave_public_key",
        value_type=SettingValueType.string,
        value_text=os.getenv("FLUTTERWAVE_PUBLIC_KEY", ""),
    )
    billing_settings.ensure_by_key(
        db,
        key="flutterwave_secret_hash",
        value_type=SettingValueType.string,
        value_text=os.getenv("FLUTTERWAVE_SECRET_HASH", ""),
        is_secret=True,
    )


def seed_catalog_settings(db: Session) -> None:
    catalog_settings.ensure_by_key(
        db,
        key="default_proration_policy",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_PRORATION_POLICY", "immediate"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_downgrade_policy",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_DOWNGRADE_POLICY", "next_cycle"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_suspension_action",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_SUSPENSION_ACTION", "suspend"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_refund_policy",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_REFUND_POLICY", "none"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_billing_cycle",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_BILLING_CYCLE", "monthly"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_contract_term",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_CONTRACT_TERM", "month_to_month"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_offer_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_OFFER_STATUS", "active"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_subscription_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_SUBSCRIPTION_STATUS", "pending"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_billing_mode",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_BILLING_MODE", "prepaid"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="billing_mode_help_text",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "CATALOG_BILLING_MODE_HELP_TEXT", "Overrides tariff default."
        ),
    )
    catalog_settings.ensure_by_key(
        db,
        key="billing_mode_prepaid_notice",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "CATALOG_BILLING_MODE_PREPAID_NOTICE", "Balance enforcement applies."
        ),
    )
    catalog_settings.ensure_by_key(
        db,
        key="billing_mode_postpaid_notice",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "CATALOG_BILLING_MODE_POSTPAID_NOTICE",
            "This subscription follows dunning steps.",
        ),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_price_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_PRICE_TYPE", "recurring"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_addon_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_ADDON_TYPE", "custom"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_addon_quantity",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CATALOG_DEFAULT_ADDON_QUANTITY", "1"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_nas_vendor",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_NAS_VENDOR", "other"),
    )


def seed_subscriber_settings(db: Session) -> None:
    subscriber_settings.ensure_by_key(
        db,
        key="default_account_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("SUBSCRIBER_DEFAULT_ACCOUNT_STATUS", "active"),
    )
    subscriber_settings.ensure_by_key(
        db,
        key="default_address_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("SUBSCRIBER_DEFAULT_ADDRESS_TYPE", "service"),
    )
    subscriber_settings.ensure_by_key(
        db,
        key="default_contact_role",
        value_type=SettingValueType.string,
        value_text=os.getenv("SUBSCRIBER_DEFAULT_CONTACT_ROLE", "primary"),
    )
    subscriber_number_enabled_raw = os.getenv("SUBSCRIBER_NUMBER_ENABLED", "true")
    subscriber_settings.ensure_by_key(
        db,
        key="subscriber_number_enabled",
        value_type=SettingValueType.boolean,
        value_text=subscriber_number_enabled_raw,
        value_json=subscriber_number_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    subscriber_settings.ensure_by_key(
        db,
        key="subscriber_number_prefix",
        value_type=SettingValueType.string,
        value_text=os.getenv("SUBSCRIBER_NUMBER_PREFIX", "SUB-"),
    )
    subscriber_settings.ensure_by_key(
        db,
        key="subscriber_number_padding",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SUBSCRIBER_NUMBER_PADDING", "6"),
    )
    subscriber_settings.ensure_by_key(
        db,
        key="subscriber_number_start",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SUBSCRIBER_NUMBER_START", "1"),
    )
    account_number_enabled_raw = os.getenv("SUBSCRIBER_ACCOUNT_NUMBER_ENABLED", "true")
    subscriber_settings.ensure_by_key(
        db,
        key="account_number_enabled",
        value_type=SettingValueType.boolean,
        value_text=account_number_enabled_raw,
        value_json=account_number_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    subscriber_settings.ensure_by_key(
        db,
        key="account_number_prefix",
        value_type=SettingValueType.string,
        value_text=os.getenv("SUBSCRIBER_ACCOUNT_NUMBER_PREFIX", "ACC-"),
    )
    subscriber_settings.ensure_by_key(
        db,
        key="account_number_padding",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SUBSCRIBER_ACCOUNT_NUMBER_PADDING", "6"),
    )
    subscriber_settings.ensure_by_key(
        db,
        key="account_number_start",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SUBSCRIBER_ACCOUNT_NUMBER_START", "1"),
    )


def seed_usage_policy_settings(db: Session) -> None:
    usage_settings.ensure_by_key(
        db,
        key="default_charge_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("USAGE_DEFAULT_CHARGE_STATUS", "staged"),
    )
    usage_settings.ensure_by_key(
        db,
        key="default_rating_run_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("USAGE_DEFAULT_RATING_RUN_STATUS", "running"),
    )


def seed_collections_policy_settings(db: Session) -> None:
    collections_settings.ensure_by_key(
        db,
        key="default_dunning_case_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("COLLECTIONS_DEFAULT_DUNNING_CASE_STATUS", "open"),
    )


def seed_auth_policy_settings(db: Session) -> None:
    auth_settings.ensure_by_key(
        db,
        key="default_auth_provider",
        value_type=SettingValueType.string,
        value_text=os.getenv("AUTH_DEFAULT_AUTH_PROVIDER", "local"),
    )
    auth_settings.ensure_by_key(
        db,
        key="default_session_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("AUTH_DEFAULT_SESSION_STATUS", "active"),
    )


def seed_provisioning_settings(db: Session) -> None:
    provisioning_settings.ensure_by_key(
        db,
        key="default_service_order_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROVISIONING_DEFAULT_SERVICE_ORDER_STATUS", "draft"),
    )
    provisioning_settings.ensure_by_key(
        db,
        key="service_migration_jobs_log",
        value_type=SettingValueType.json,
        value_json=[],
    )
    provisioning_settings.ensure_by_key(
        db,
        key="bulk_activation_jobs_log",
        value_type=SettingValueType.json,
        value_json=[],
    )


def seed_provisioning_workflows(db: Session) -> None:
    """Seed default provisioning workflows for fiber subscriber onboarding.

    Creates a "Fiber PPPoE Full Provisioning" workflow with the standard
    5-step automated sequence. Skips if a workflow with this name already exists.
    """
    from app.models.provisioning import (
        ProvisioningStep,
        ProvisioningStepType,
        ProvisioningVendor,
        ProvisioningWorkflow,
    )

    workflow_name = "Fiber PPPoE Full Provisioning"
    from sqlalchemy import select as sa_select

    existing = db.scalars(
        sa_select(ProvisioningWorkflow).where(
            ProvisioningWorkflow.name == workflow_name
        )
    ).first()
    if existing:
        return

    workflow = ProvisioningWorkflow(
        name=workflow_name,
        vendor=ProvisioningVendor.huawei,
        description=(
            "End-to-end automated provisioning for Huawei FTTH with MikroTik NAS. "
            "Creates OLT service-port, NAS VLAN/IP/PPPoE, pushes WAN config and "
            "PPPoE credentials to ONT via TR-069, then verifies subscriber comes online."
        ),
        is_active=True,
    )
    db.add(workflow)
    db.flush()

    steps = [
        {
            "name": "Create OLT Service Port",
            "step_type": ProvisioningStepType.create_olt_service_port,
            "order_index": 10,
            "config": {
                "gem_index": 1,
                "description": "Map ONT GEM port to service VLAN on OLT",
            },
        },
        {
            "name": "Ensure NAS VLAN",
            "step_type": ProvisioningStepType.ensure_nas_vlan,
            "order_index": 20,
            "config": {
                "parent_interface": "ether3",
                "pppoe_default_profile": "default",
                "description": "Create VLAN interface, IP, and PPPoE server on MikroTik NAS",
            },
        },
        {
            "name": "Push WAN Config via TR-069",
            "step_type": ProvisioningStepType.push_tr069_wan_config,
            "order_index": 30,
            "config": {
                "wan_mode": "pppoe",
                "description": "Configure ONT WAN mode to PPPoE via GenieACS",
            },
        },
        {
            "name": "Push PPPoE Credentials via TR-069",
            "step_type": ProvisioningStepType.push_tr069_pppoe_credentials,
            "order_index": 40,
            "config": {
                "description": "Push subscriber PPPoE username/password to ONT via GenieACS",
            },
        },
        {
            "name": "Confirm Subscriber Online",
            "step_type": ProvisioningStepType.confirm_up,
            "order_index": 50,
            "config": {
                "description": "Verify subscriber has connected and is online via RADIUS",
            },
        },
    ]

    for step_data in steps:
        step = ProvisioningStep(
            workflow_id=workflow.id,
            **step_data,
        )
        db.add(step)

    db.commit()
    logger.info(
        "Seeded provisioning workflow: %s (%d steps)", workflow_name, len(steps)
    )


def seed_projects_settings(db: Session) -> None:
    """Seed minimal projects settings required by services/tests."""
    projects_settings = DomainSettings(SettingDomain.projects)
    projects_settings.ensure_by_key(
        db,
        key="default_project_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROJECTS_DEFAULT_PROJECT_STATUS", "active"),
    )


def seed_inventory_settings(db: Session) -> None:
    """Seed minimal inventory settings required by services/tests."""
    inventory_settings = DomainSettings(SettingDomain.inventory)
    inventory_settings.ensure_by_key(
        db,
        key="default_reservation_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("INVENTORY_DEFAULT_RESERVATION_STATUS", "pending"),
    )
    provisioning_settings.ensure_by_key(
        db,
        key="default_appointment_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROVISIONING_DEFAULT_APPOINTMENT_STATUS", "proposed"),
    )
    provisioning_settings.ensure_by_key(
        db,
        key="default_task_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROVISIONING_DEFAULT_TASK_STATUS", "pending"),
    )
    provisioning_settings.ensure_by_key(
        db,
        key="default_vendor",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROVISIONING_DEFAULT_VENDOR", "other"),
    )
    provisioning_settings.ensure_by_key(
        db,
        key="default_workflow_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROVISIONING_DEFAULT_WORKFLOW_ID", ""),
    )


def seed_tr069_settings(db: Session) -> None:
    tr069_settings.ensure_by_key(
        db,
        key="default_acs_server_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("TR069_DEFAULT_ACS_SERVER_ID", ""),
    )


def seed_network_policy_settings(db: Session) -> None:
    from app.services.domain_settings import network_settings

    network_settings.ensure_by_key(
        db,
        key="default_device_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_DEVICE_TYPE", "other"),
    )
    network_settings.ensure_by_key(
        db,
        key="default_device_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_DEVICE_STATUS", "active"),
    )
    network_settings.ensure_by_key(
        db,
        key="default_port_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_PORT_TYPE", "ethernet"),
    )
    network_settings.ensure_by_key(
        db,
        key="default_port_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_PORT_STATUS", "down"),
    )
    network_settings.ensure_by_key(
        db,
        key="default_ip_version",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_IP_VERSION", "ipv4"),
    )
    network_settings.ensure_by_key(
        db,
        key="default_olt_port_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_OLT_PORT_TYPE", "pon"),
    )
    network_settings.ensure_by_key(
        db,
        key="default_fiber_strand_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_FIBER_STRAND_STATUS", "available"),
    )
    network_settings.ensure_by_key(
        db,
        key="default_splitter_input_ports",
        value_type=SettingValueType.integer,
        value_text=os.getenv("NETWORK_DEFAULT_SPLITTER_INPUT_PORTS", "1"),
    )
    network_settings.ensure_by_key(
        db,
        key="default_splitter_output_ports",
        value_type=SettingValueType.integer,
        value_text=os.getenv("NETWORK_DEFAULT_SPLITTER_OUTPUT_PORTS", "8"),
    )
    # Fiber installation planning cost rates
    network_settings.ensure_by_key(
        db,
        key="fiber_drop_cable_cost_per_meter",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_FIBER_DROP_CABLE_COST_PER_METER", "2.50"),
    )
    network_settings.ensure_by_key(
        db,
        key="fiber_labor_cost_per_meter",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_FIBER_LABOR_COST_PER_METER", "1.50"),
    )
    network_settings.ensure_by_key(
        db,
        key="fiber_ont_device_cost",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_FIBER_ONT_DEVICE_COST", "85.00"),
    )
    network_settings.ensure_by_key(
        db,
        key="fiber_installation_base_fee",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_FIBER_INSTALLATION_BASE_FEE", "50.00"),
    )


def seed_network_settings(db: Session) -> None:
    kill_enabled_raw = os.getenv("NETWORK_MIKROTIK_SESSION_KILL_ENABLED", "true")
    network_settings.ensure_by_key(
        db,
        key="mikrotik_session_kill_enabled",
        value_type=SettingValueType.boolean,
        value_text=kill_enabled_raw,
        value_json=kill_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    block_enabled_raw = os.getenv("NETWORK_ADDRESS_LIST_BLOCK_ENABLED", "true")
    network_settings.ensure_by_key(
        db,
        key="address_list_block_enabled",
        value_type=SettingValueType.boolean,
        value_text=block_enabled_raw,
        value_json=block_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    network_settings.ensure_by_key(
        db,
        key="default_mikrotik_address_list",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_MIKROTIK_ADDRESS_LIST", ""),
    )
    network_settings.ensure_by_key(
        db,
        key="vpn_control_jobs_log",
        value_type=SettingValueType.json,
        value_json=[],
    )


def seed_radius_policy_settings(db: Session) -> None:
    radius_settings.ensure_by_key(
        db,
        key="default_auth_port",
        value_type=SettingValueType.integer,
        value_text=os.getenv("RADIUS_DEFAULT_AUTH_PORT", "1812"),
    )


def seed_network_monitoring_settings(db: Session) -> None:
    network_monitoring_settings.ensure_by_key(
        db,
        key="server_health_disk_warn_pct",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SERVER_HEALTH_DISK_WARN_PCT", "80"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="server_health_disk_crit_pct",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SERVER_HEALTH_DISK_CRIT_PCT", "90"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="server_health_mem_warn_pct",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SERVER_HEALTH_MEM_WARN_PCT", "80"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="server_health_mem_crit_pct",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SERVER_HEALTH_MEM_CRIT_PCT", "90"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="server_health_load_warn",
        value_type=SettingValueType.string,
        value_text=os.getenv("SERVER_HEALTH_LOAD_WARN", "1.0"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="server_health_load_crit",
        value_type=SettingValueType.string,
        value_text=os.getenv("SERVER_HEALTH_LOAD_CRIT", "1.5"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="network_health_warn_pct",
        value_type=SettingValueType.integer,
        value_text=os.getenv("NETWORK_HEALTH_WARN_PCT", "90"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="network_health_crit_pct",
        value_type=SettingValueType.integer,
        value_text=os.getenv("NETWORK_HEALTH_CRIT_PCT", "70"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="core_device_ping_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CORE_DEVICE_PING_INTERVAL_SECONDS", "120"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="core_device_snmp_walk_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CORE_DEVICE_SNMP_WALK_INTERVAL_SECONDS", "300"),
    )
    radius_settings.ensure_by_key(
        db,
        key="default_acct_port",
        value_type=SettingValueType.integer,
        value_text=os.getenv("RADIUS_DEFAULT_ACCT_PORT", "1813"),
    )
    default_sync_users = os.getenv("RADIUS_DEFAULT_SYNC_USERS", "true")
    radius_settings.ensure_by_key(
        db,
        key="default_sync_users",
        value_type=SettingValueType.boolean,
        value_text=default_sync_users,
        value_json=default_sync_users.lower() in {"1", "true", "yes", "on"},
    )
    default_sync_clients = os.getenv("RADIUS_DEFAULT_SYNC_NAS_CLIENTS", "true")
    radius_settings.ensure_by_key(
        db,
        key="default_sync_nas_clients",
        value_type=SettingValueType.boolean,
        value_text=default_sync_clients,
        value_json=default_sync_clients.lower() in {"1", "true", "yes", "on"},
    )
    radius_settings.ensure_by_key(
        db,
        key="default_sync_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_DEFAULT_SYNC_STATUS", "running"),
    )


def seed_lifecycle_settings(db: Session) -> None:
    lifecycle_settings.ensure_by_key(
        db,
        key="default_event_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("LIFECYCLE_DEFAULT_EVENT_TYPE", "other"),
    )


def seed_comms_settings(db: Session) -> None:
    comms_settings.ensure_by_key(
        db,
        key="default_notification_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("COMMS_DEFAULT_NOTIFICATION_STATUS", "pending"),
    )
    # Meta (Facebook/Instagram) Integration Settings
    comms_settings.ensure_by_key(
        db,
        key="meta_app_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("META_APP_ID", ""),
    )
    comms_settings.ensure_by_key(
        db,
        key="meta_app_secret",
        value_type=SettingValueType.string,
        value_text=os.getenv("META_APP_SECRET", ""),
        is_secret=True,
    )
    comms_settings.ensure_by_key(
        db,
        key="meta_webhook_verify_token",
        value_type=SettingValueType.string,
        value_text=os.getenv("META_WEBHOOK_VERIFY_TOKEN", ""),
        is_secret=True,
    )
    comms_settings.ensure_by_key(
        db,
        key="meta_oauth_redirect_uri",
        value_type=SettingValueType.string,
        value_text=os.getenv("META_OAUTH_REDIRECT_URI", ""),
    )
    comms_settings.ensure_by_key(
        db,
        key="meta_graph_api_version",
        value_type=SettingValueType.string,
        value_text=os.getenv("META_GRAPH_API_VERSION", "v19.0"),
    )
    comms_settings.ensure_by_key(
        db,
        key="meta_access_token_override",
        value_type=SettingValueType.string,
        value_text=os.getenv("META_ACCESS_TOKEN_OVERRIDE", ""),
        is_secret=True,
    )
    comms_settings.ensure_by_key(
        db,
        key="whatsapp_provider",
        value_type=SettingValueType.string,
        value_text=os.getenv("WHATSAPP_PROVIDER", "meta_cloud_api"),
    )
    comms_settings.ensure_by_key(
        db,
        key="whatsapp_api_key",
        value_type=SettingValueType.string,
        value_text=os.getenv("WHATSAPP_API_KEY", ""),
        is_secret=True,
    )
    comms_settings.ensure_by_key(
        db,
        key="whatsapp_api_secret",
        value_type=SettingValueType.string,
        value_text=os.getenv("WHATSAPP_API_SECRET", ""),
        is_secret=True,
    )
    comms_settings.ensure_by_key(
        db,
        key="whatsapp_phone_number",
        value_type=SettingValueType.string,
        value_text=os.getenv("WHATSAPP_PHONE_NUMBER", ""),
    )
    comms_settings.ensure_by_key(
        db,
        key="whatsapp_webhook_url",
        value_type=SettingValueType.string,
        value_text=os.getenv("WHATSAPP_WEBHOOK_URL", ""),
    )
    templates_json_raw = os.getenv("WHATSAPP_MESSAGE_TEMPLATES_JSON", "[]")
    try:
        templates_json_value = json.loads(templates_json_raw)
        if not isinstance(templates_json_value, list):
            templates_json_value = []
    except json.JSONDecodeError:
        templates_json_value = []
    comms_settings.ensure_by_key(
        db,
        key="whatsapp_message_templates",
        value_type=SettingValueType.json,
        value_json=templates_json_value,
    )


def seed_wireguard_settings(db: Session) -> None:
    """Seed WireGuard VPN settings from environment variables."""
    # Encryption key for storing WireGuard private keys at rest
    wg_key = os.getenv("WIREGUARD_KEY_ENCRYPTION_KEY")
    if wg_key and is_openbao_ref(wg_key):
        network_settings.ensure_by_key(
            db,
            key="wireguard_key_encryption_key",
            value_type=SettingValueType.string,
            value_text=wg_key,
            is_secret=True,
        )
    # Log retention
    network_settings.ensure_by_key(
        db,
        key="wireguard_log_retention_days",
        value_type=SettingValueType.integer,
        value_text=os.getenv("WIREGUARD_LOG_RETENTION_DAYS", "90"),
    )
    # Log cleanup task settings
    log_cleanup_enabled = os.getenv("WIREGUARD_LOG_CLEANUP_ENABLED", "true")
    network_settings.ensure_by_key(
        db,
        key="wireguard_log_cleanup_enabled",
        value_type=SettingValueType.boolean,
        value_text=log_cleanup_enabled,
        value_json=log_cleanup_enabled.lower() in {"1", "true", "yes", "on"},
    )
    network_settings.ensure_by_key(
        db,
        key="wireguard_log_cleanup_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("WIREGUARD_LOG_CLEANUP_INTERVAL_SECONDS", "86400"),
    )
    # Token cleanup settings
    token_cleanup_enabled = os.getenv("WIREGUARD_TOKEN_CLEANUP_ENABLED", "true")
    network_settings.ensure_by_key(
        db,
        key="wireguard_token_cleanup_enabled",
        value_type=SettingValueType.boolean,
        value_text=token_cleanup_enabled,
        value_json=token_cleanup_enabled.lower() in {"1", "true", "yes", "on"},
    )
    network_settings.ensure_by_key(
        db,
        key="wireguard_token_cleanup_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("WIREGUARD_TOKEN_CLEANUP_INTERVAL_SECONDS", "3600"),
    )
    # Peer stats sync settings
    stats_sync_enabled = os.getenv("WIREGUARD_PEER_STATS_SYNC_ENABLED", "true")
    network_settings.ensure_by_key(
        db,
        key="wireguard_peer_stats_sync_enabled",
        value_type=SettingValueType.boolean,
        value_text=stats_sync_enabled,
        value_json=stats_sync_enabled.lower() in {"1", "true", "yes", "on"},
    )
    network_settings.ensure_by_key(
        db,
        key="wireguard_peer_stats_sync_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("WIREGUARD_PEER_STATS_SYNC_INTERVAL_SECONDS", "300"),
    )

    # VPN Server Defaults
    network_settings.ensure_by_key(
        db,
        key="wireguard_default_listen_port",
        value_type=SettingValueType.integer,
        value_text=os.getenv("WIREGUARD_DEFAULT_LISTEN_PORT", "51820"),
    )
    network_settings.ensure_by_key(
        db,
        key="wireguard_default_vpn_address",
        value_type=SettingValueType.string,
        value_text=os.getenv("WIREGUARD_DEFAULT_VPN_ADDRESS", "10.10.0.1/24"),
    )
    network_settings.ensure_by_key(
        db,
        key="wireguard_default_vpn_address_v6",
        value_type=SettingValueType.string,
        value_text=os.getenv("WIREGUARD_DEFAULT_VPN_ADDRESS_V6", ""),
    )
    network_settings.ensure_by_key(
        db,
        key="wireguard_default_mtu",
        value_type=SettingValueType.integer,
        value_text=os.getenv("WIREGUARD_DEFAULT_MTU", "1420"),
    )
    network_settings.ensure_by_key(
        db,
        key="wireguard_default_interface_name",
        value_type=SettingValueType.string,
        value_text=os.getenv("WIREGUARD_DEFAULT_INTERFACE_NAME", "wg0"),
    )
