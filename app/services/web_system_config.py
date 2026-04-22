"""Service helpers for all configuration/settings pages (08_config features)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.billing_settings import resolve_payment_due_days

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Generic setting read / write helpers
# ---------------------------------------------------------------------------


def _read_settings(
    db: Session, domain: SettingDomain, keys: list[str]
) -> dict[str, str]:
    """Read a batch of DomainSettings, returning {key: value_text}."""
    stmt = (
        select(DomainSetting)
        .where(DomainSetting.domain == domain)
        .where(DomainSetting.key.in_(keys))
    )
    rows = db.scalars(stmt).all()
    result: dict[str, str] = dict.fromkeys(keys, "")
    for row in rows:
        result[row.key] = row.value_text or ""
    return result


def _save_settings(
    db: Session,
    domain: SettingDomain,
    data: Mapping[str, str],
    keys: list[str],
    *,
    secret_keys: set[str] | None = None,
) -> None:
    """Upsert a batch of DomainSettings."""
    secret_keys = secret_keys or set()
    for key in keys:
        value = (data.get(key) or "").strip()
        stmt = select(DomainSetting).where(
            DomainSetting.domain == domain,
            DomainSetting.key == key,
        )
        setting = db.scalars(stmt).first()
        if setting:
            setting.value_text = value
            setting.is_secret = key in secret_keys
        else:
            setting = DomainSetting(
                domain=domain,
                key=key,
                value_text=value,
                value_type=SettingValueType.string,
                is_secret=key in secret_keys,
            )
            db.add(setting)
    db.flush()
    db.commit()


# ---------------------------------------------------------------------------
# 8.5 System Preferences & Security
# ---------------------------------------------------------------------------
PREFERENCE_KEYS = [
    "default_landing_page",
    "admin_portal_title",
    "force_2fa",
    "search_debounce_ms",
]


def get_preferences_context(db: Session) -> dict:
    return {"preferences": _read_settings(db, SettingDomain.auth, PREFERENCE_KEYS)}


def save_preferences(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.auth, data, PREFERENCE_KEYS)


# ---------------------------------------------------------------------------
# 8.7 Subscriber Settings
# ---------------------------------------------------------------------------
SUBSCRIBER_KEYS = [
    "login_format",
    "password_length",
    "password_charset",
    "welcome_enabled",
    "welcome_channel",
    "welcome_delay",
    "default_billing_type",
    "max_search_results",
    "stats_format",
    "portal_2fa_enabled",
    "portal_2fa_method",
]


def get_subscriber_config_context(db: Session) -> dict:
    return {
        "subscriber_settings": _read_settings(
            db, SettingDomain.subscriber, SUBSCRIBER_KEYS
        )
    }


def save_subscriber_config(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.subscriber, data, SUBSCRIBER_KEYS)


# ---------------------------------------------------------------------------
# 8.8 Customer Portal Configuration
# ---------------------------------------------------------------------------
PORTAL_KEYS = [
    # Domain routing
    "selfcare_domain",
    "selfcare_redirect_root",
    "admin_domain",
    "reseller_domain",
    # Portal behaviour
    "portal_language",
    "portal_auth_field",
    "portal_password_reset",
    "portal_help_url",
    "show_payment_due",
    "show_bandwidth_usage",
    "show_fup_rules",
    "show_session_stats",
    "mobile_app_google_play_id",
    "mobile_app_appstore_id",
]


def get_portal_config_context(db: Session) -> dict:
    return {"portal_settings": _read_settings(db, SettingDomain.auth, PORTAL_KEYS)}


def save_portal_config(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.auth, data, PORTAL_KEYS)


# ---------------------------------------------------------------------------
# 8.10 Data Retention
# ---------------------------------------------------------------------------
RETENTION_KEYS = [
    "admin_logs_months",
    "api_logs_months",
    "internal_logs_months",
    "portal_logs_months",
    "usage_stats_months",
    "task_logs_months",
    "task_results_months",
]
RETENTION_DEFAULTS = {
    "admin_logs_months": "6",
    "api_logs_months": "1",
    "internal_logs_months": "24",
    "portal_logs_months": "12",
    "usage_stats_months": "36",
    "task_logs_months": "3",
    "task_results_months": "24",
}


def get_retention_context(db: Session) -> dict:
    settings = _read_settings(db, SettingDomain.audit, RETENTION_KEYS)
    for k, v in RETENTION_DEFAULTS.items():
        if not settings.get(k):
            settings[k] = v
    return {"retention": settings, "retention_defaults": RETENTION_DEFAULTS}


def save_retention(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.audit, data, RETENTION_KEYS)


# ---------------------------------------------------------------------------
# 8.11 Finance Automation
# ---------------------------------------------------------------------------
FINANCE_AUTO_KEYS = [
    "auto_invoice_enabled",
    "confirmation_period_days",
    "confirmation_time",
    "preview_days",
    "date_mode",
    "dashboard_billing_notification",
    "auto_blocking_enabled",
    "blocking_time",
    "block_on_weekends",
    "block_on_holidays",
    "deactivation_enabled",
    "prepaid_deactivation",
    "ip_reclamation_enabled",
    "ip_reclamation_months",
]


def get_finance_automation_context(db: Session) -> dict:
    return {
        "finance_auto": _read_settings(db, SettingDomain.billing, FINANCE_AUTO_KEYS)
    }


def save_finance_automation(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.billing, data, FINANCE_AUTO_KEYS)


# ---------------------------------------------------------------------------
# 8.12 Billing & Invoice Settings
# ---------------------------------------------------------------------------
BILLING_KEYS = [
    "billing_enabled",
    "payment_period",
    "billing_day",
    "use_creation_date",
    "payment_due_days",
    "auto_suspend_on_overdue",
    "suspension_grace_hours",
    "expiry_reminder_days",
    "invoice_reminder_days",
    "dunning_escalation_days",
    "blocking_period_days",
    "deactivation_period_days",
    "minimum_balance",
    "send_billing_notifications",
    "invoice_number_format",
    "receipt_number_format",
    "credit_note_format",
    "proforma_enabled",
    "proforma_generation_day",
    "proforma_payment_period",
    "zero_total_invoices",
    "invoice_caching",
    # Prepaid customer defaults
    "prepaid_default_billing_day",
    "prepaid_default_payment_due_days",
    "prepaid_default_grace_period_days",
    "prepaid_default_min_balance",
    # Postpaid customer defaults
    "postpaid_default_billing_day",
    "postpaid_default_payment_due_days",
    "postpaid_default_grace_period_days",
    "postpaid_default_min_balance",
]


def get_billing_config_context(db: Session) -> dict:
    billing = _read_settings(db, SettingDomain.billing, BILLING_KEYS)
    if not billing.get("payment_due_days"):
        billing["payment_due_days"] = str(resolve_payment_due_days(db))
    defaults = {
        "suspension_grace_hours": "48",
        "expiry_reminder_days": "7",
        "invoice_reminder_days": "7,1",
        "dunning_escalation_days": "3,7,14,30",
    }
    for key, value in defaults.items():
        if not billing.get(key):
            billing[key] = value
    return {"billing": billing}


def save_billing_config(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.billing, data, BILLING_KEYS)


# ---------------------------------------------------------------------------
# 8.13 Payment Methods (read-only list from models)
# ---------------------------------------------------------------------------


def get_payment_methods_context(db: Session) -> dict:
    """List payment methods/providers."""
    try:
        from app.models.billing import PaymentProvider

        stmt = select(PaymentProvider).order_by(PaymentProvider.name)
        methods = list(db.scalars(stmt).all())
    except Exception:
        logger.debug("PaymentProvider model not available")
        methods = []
    return {"payment_methods": methods}


# ---------------------------------------------------------------------------
# 8.18 Tax Configuration
# ---------------------------------------------------------------------------


def get_tax_config_context(db: Session) -> dict:
    """List tax rates."""
    try:
        from app.models.billing import TaxRate

        stmt = select(TaxRate).order_by(TaxRate.name)
        rates = list(db.scalars(stmt).all())
    except Exception:
        logger.debug("TaxRate model not available")
        rates = []
    return {"tax_rates": rates}


# ---------------------------------------------------------------------------
# 8.16 Billing Reminders
# ---------------------------------------------------------------------------
REMINDER_KEYS = [
    "reminders_enabled",
    "reminder_channel",
    "reminder_send_time",
    "wave1_days",
    "wave1_subject",
    "wave1_email_template",
    "wave1_sms_template",
    "wave2_days",
    "wave2_subject",
    "wave2_email_template",
    "wave2_sms_template",
    "wave3_days",
    "wave3_subject",
    "wave3_email_template",
    "wave3_sms_template",
    "attach_invoices",
    "payment_method_filter",
]


def get_reminders_context(db: Session) -> dict:
    return {"reminders": _read_settings(db, SettingDomain.collections, REMINDER_KEYS)}


def save_reminders(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.collections, data, REMINDER_KEYS)


# ---------------------------------------------------------------------------
# 8.17 Billing Notifications
# ---------------------------------------------------------------------------
BILLING_NOTIF_KEYS = [
    "billing_notif_send_hour",
    "blocking_wave_enabled",
    "blocking_wave_channel",
    "blocking_wave_email_template",
    "blocking_wave_sms_template",
    "blocking_wave_bcc",
    "pre_block_wave1_enabled",
    "pre_block_wave1_days",
    "pre_block_wave1_channel",
    "pre_block_wave1_email_template",
    "pre_block_wave1_sms_template",
    "pre_block_wave2_enabled",
    "pre_block_wave2_days",
    "pre_block_wave2_channel",
    "pre_block_wave2_email_template",
    "pre_block_wave2_sms_template",
]


def get_billing_notifications_context(db: Session) -> dict:
    return {
        "billing_notif": _read_settings(
            db, SettingDomain.collections, BILLING_NOTIF_KEYS
        )
    }


def save_billing_notifications(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.collections, data, BILLING_NOTIF_KEYS)


# ---------------------------------------------------------------------------
# 8.19 Plan Change Configuration
# ---------------------------------------------------------------------------
PLAN_CHANGE_KEYS = [
    "refund_policy",
    "upgrade_fee",
    "downgrade_fee",
    "fee_tax_rate",
    "invoice_timing",
    "prepaid_rollover",
    "discount_transfer",
    "minimum_invoice_amount",
]


def get_plan_change_context(db: Session) -> dict:
    return {"plan_change": _read_settings(db, SettingDomain.billing, PLAN_CHANGE_KEYS)}


def save_plan_change(db: Session, data: Mapping[str, Any]) -> None:
    normalized = dict(data)
    refund_policy = str(normalized.get("refund_policy") or "").strip().lower()
    invoice_timing = str(normalized.get("invoice_timing") or "").strip().lower()
    prepaid_rollover = str(normalized.get("prepaid_rollover") or "").strip().lower()
    discount_transfer = str(normalized.get("discount_transfer") or "").strip().lower()

    allowed_refund_policies = {"none", "prorated", "full_within_days"}
    allowed_invoice_timing = {"immediate", "next_invoice"}
    allowed_booleanish = {"true", "false"}

    if refund_policy not in allowed_refund_policies:
        raise ValueError(
            "Refund Policy must be one of: none, prorated, full_within_days."
        )
    if invoice_timing not in allowed_invoice_timing:
        raise ValueError("Invoice Timing must be either immediate or next_invoice.")
    if prepaid_rollover not in allowed_booleanish:
        raise ValueError("Prepaid Rollover must be true or false.")
    if discount_transfer not in allowed_booleanish:
        raise ValueError("Discount Transfer must be true or false.")

    for key in (
        "upgrade_fee",
        "downgrade_fee",
        "fee_tax_rate",
        "minimum_invoice_amount",
    ):
        value = str(normalized.get(key) or "").strip()
        if not value:
            continue
        try:
            Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(
                f"{key.replace('_', ' ').title()} must be a valid decimal value."
            ) from exc

    normalized["refund_policy"] = refund_policy
    normalized["invoice_timing"] = invoice_timing
    normalized["prepaid_rollover"] = prepaid_rollover
    normalized["discount_transfer"] = discount_transfer
    _save_settings(db, SettingDomain.billing, normalized, PLAN_CHANGE_KEYS)


# ---------------------------------------------------------------------------
# 8.20 RADIUS Configuration
# ---------------------------------------------------------------------------
RADIUS_KEYS = [
    "reject_ip_not_found",
    "reject_ip_blocked",
    "reject_ip_negative",
    "reject_ip_bad_mac",
    "reject_ip_bad_password",
    "nas_type_default",
    "debug_enabled",
    "debug_level",
    "debug_auto_off_minutes",
    "mac_binding_enabled",
    "max_mac_addresses",
    "ip_pool_location_linking",
    "periodic_restart_enabled",
    "restart_frequency",
    "restart_time",
    "captive_redirect_enabled",
    "captive_portal_ip",
    "captive_portal_url",
    "allow_unknown_nas",
    "default_nas_id",
    # PPPoE auto-generation
    "pppoe_username_prefix",
    "pppoe_username_padding",
    "pppoe_username_start",
    "pppoe_default_password_length",
]


def get_radius_config_context(db: Session) -> dict:
    return {"radius": _read_settings(db, SettingDomain.radius, RADIUS_KEYS)}


def save_radius_config(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.radius, data, RADIUS_KEYS)


# ---------------------------------------------------------------------------
# 8.22 CPE Configuration
# ---------------------------------------------------------------------------
CPE_KEYS = [
    "kb_base",
    "api_debug",
    "api_timeout",
    "api_attempts",
    "qos_reverse",
    "qos_download_queue_type",
    "qos_upload_queue_type",
    "blocking_enabled",
    "redirect_ip",
    "redirect_port",
    "dhcp_enabled",
    "dhcp_server_name",
    "dhcp_interface",
    "dhcp_lease_time",
    "dhcp_dns_servers",
    "wlan_management",
]


def get_cpe_config_context(db: Session) -> dict:
    return {"cpe": _read_settings(db, SettingDomain.network, CPE_KEYS)}


def save_cpe_config(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.network, data, CPE_KEYS)


# ---------------------------------------------------------------------------
# 8.23 Monitoring Configuration
# ---------------------------------------------------------------------------
MONITORING_KEYS = [
    "monitoring_vendors",
    "monitoring_device_types",
    "monitoring_groups",
    "cpu_warn_pct",
    "mem_warn_pct",
    "interface_warn_pct",
    "device_metrics_retention_days",
    "alert_evaluation_interval_seconds",
    "ont_signal_warning_dbm",
    "ont_signal_critical_dbm",
    "ont_signal_alert_cooldown_minutes",
    "interface_walk_interval_seconds",
    "interface_discovery_interval_seconds",
    "hot_retention_hours",
    "cleanup_interval_seconds",
]


def get_monitoring_config_context(db: Session) -> dict:
    settings = _read_settings(
        db,
        SettingDomain.network_monitoring,
        [
            "monitoring_vendors",
            "monitoring_device_types",
            "monitoring_groups",
            "cpu_warn_pct",
            "mem_warn_pct",
            "interface_warn_pct",
            "device_metrics_retention_days",
            "alert_evaluation_interval_seconds",
            "ont_signal_warning_dbm",
            "ont_signal_critical_dbm",
            "ont_signal_alert_cooldown_minutes",
        ],
    )
    settings.update(
        _read_settings(
            db,
            SettingDomain.snmp,
            [
                "interface_walk_interval_seconds",
                "interface_discovery_interval_seconds",
            ],
        )
    )
    settings.update(
        _read_settings(
            db,
            SettingDomain.bandwidth,
            [
                "hot_retention_hours",
                "cleanup_interval_seconds",
            ],
        )
    )
    # Provide defaults for list-type settings
    if not settings.get("monitoring_vendors"):
        settings["monitoring_vendors"] = (
            "MikroTik,Cisco,Ubiquiti,Huawei,TP-Link,Juniper,D-Link,Ericsson"
        )
    if not settings.get("monitoring_device_types"):
        settings["monitoring_device_types"] = (
            "Router,Switch,Server,Access Point,CPE,OLT,ONT,Firewall"
        )
    if not settings.get("monitoring_groups"):
        settings["monitoring_groups"] = "Core Infrastructure,Access Layer,Customer CPE"
    defaults = {
        "device_metrics_retention_days": "90",
        "alert_evaluation_interval_seconds": "60",
        "ont_signal_warning_dbm": "-25",
        "ont_signal_critical_dbm": "-28",
        "ont_signal_alert_cooldown_minutes": "30",
        "interface_walk_interval_seconds": "300",
        "interface_discovery_interval_seconds": "3600",
        "hot_retention_hours": "24",
        "cleanup_interval_seconds": "3600",
    }
    for key, value in defaults.items():
        if not settings.get(key):
            settings[key] = value
    return {"monitoring": settings}


def save_monitoring_config(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(
        db,
        SettingDomain.network_monitoring,
        data,
        [
            "monitoring_vendors",
            "monitoring_device_types",
            "monitoring_groups",
            "cpu_warn_pct",
            "mem_warn_pct",
            "interface_warn_pct",
            "device_metrics_retention_days",
            "alert_evaluation_interval_seconds",
            "ont_signal_warning_dbm",
            "ont_signal_critical_dbm",
            "ont_signal_alert_cooldown_minutes",
        ],
    )
    _save_settings(
        db,
        SettingDomain.snmp,
        data,
        [
            "interface_walk_interval_seconds",
            "interface_discovery_interval_seconds",
        ],
    )
    _save_settings(
        db,
        SettingDomain.bandwidth,
        data,
        [
            "hot_retention_hours",
            "cleanup_interval_seconds",
        ],
    )


# ---------------------------------------------------------------------------
# 8.25 Fair Usage Policy
# ---------------------------------------------------------------------------
FUP_KEYS = [
    "fup_custom_reset_field",
    "fup_monthly_reset_schedule",
    "fup_weekly_reset_day",
    "fup_send_notifications",
    "fup_threshold_warn_pct",
    "fup_threshold_critical_pct",
]


def get_fup_config_context(db: Session) -> dict:
    return {"fup": _read_settings(db, SettingDomain.usage, FUP_KEYS)}


def save_fup_config(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.usage, data, FUP_KEYS)


# ---------------------------------------------------------------------------
# 8.26 NAS Types
# ---------------------------------------------------------------------------
NAS_TYPE_KEYS = ["nas_types_list"]


def get_nas_types_context(db: Session) -> dict:
    settings = _read_settings(db, SettingDomain.network, NAS_TYPE_KEYS)
    if not settings.get("nas_types_list"):
        settings["nas_types_list"] = (
            "MikroTik (API: Yes)|Cisco|Ericsson|Linux PPPD|Ubiquiti|D-Link|Juniper|"
            "Cisco IOS|Cisco IOS XE|netElastic"
        )
    types = []
    for entry in settings["nas_types_list"].split("|"):
        entry = entry.strip()
        if entry:
            has_api = "(API: Yes)" in entry
            name = entry.replace("(API: Yes)", "").replace("(API: No)", "").strip()
            types.append({"name": name, "has_api": has_api})
    return {"nas_types": types, "nas_types_raw": settings["nas_types_list"]}


# ---------------------------------------------------------------------------
# 8.27 IPv6 Configuration
# ---------------------------------------------------------------------------
IPV6_KEYS = [
    "ipv6_auto_assign_enabled",
    "ipv6_auto_assign_network",
    "ipv6_default_prefix",
    "ipv6_dual_stack_default",
]


def get_ipv6_config_context(db: Session) -> dict:
    return {"ipv6": _read_settings(db, SettingDomain.network, IPV6_KEYS)}


def save_ipv6_config(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.network, data, IPV6_KEYS)


# ---------------------------------------------------------------------------
# 8.4 Notification Templates (placeholder)
# ---------------------------------------------------------------------------

DEFAULT_TEMPLATES = [
    {
        "name": "Welcome Email",
        "category": "Onboarding",
        "channel": "Email",
        "description": "Sent when a new subscriber account is activated",
    },
    {
        "name": "Welcome SMS",
        "category": "Onboarding",
        "channel": "SMS",
        "description": "Short welcome message for new subscribers",
    },
    {
        "name": "Payment Received",
        "category": "Billing",
        "channel": "Email",
        "description": "Confirmation when payment is recorded",
    },
    {
        "name": "Invoice Generated",
        "category": "Billing",
        "channel": "Email",
        "description": "Sent when a new invoice is created",
    },
    {
        "name": "Payment Overdue",
        "category": "Billing",
        "channel": "Both",
        "description": "Reminder for overdue invoices",
    },
    {
        "name": "Service Suspension Warning",
        "category": "Billing",
        "channel": "Both",
        "description": "Warning before service is suspended",
    },
    {
        "name": "Service Restored",
        "category": "Service",
        "channel": "Email",
        "description": "Notification when service is restored after payment",
    },
    {
        "name": "Maintenance Scheduled",
        "category": "Network",
        "channel": "Email",
        "description": "Advance notice of planned maintenance",
    },
    {
        "name": "Loss of Signal Alert",
        "category": "Network",
        "channel": "Email",
        "description": "Alert when ONT loses signal",
    },
    {
        "name": "Ticket Acknowledged",
        "category": "Support",
        "channel": "Email",
        "description": "Confirmation that a support ticket was received",
    },
    {
        "name": "Ticket Resolved",
        "category": "Support",
        "channel": "Email",
        "description": "Notification that a support ticket was resolved",
    },
    {
        "name": "Installation Progress",
        "category": "Provisioning",
        "channel": "Both",
        "description": "Update on fiber installation status",
    },
    {
        "name": "Password Reset",
        "category": "Security",
        "channel": "Email",
        "description": "Password reset link for portal access",
    },
    {
        "name": "Two-Factor Code",
        "category": "Security",
        "channel": "SMS",
        "description": "2FA verification code",
    },
]


def get_templates_context(db: Session) -> dict:
    """Return notification template library context (placeholder data)."""
    return {
        "templates_list": DEFAULT_TEMPLATES,
        "categories": sorted({t["category"] for t in DEFAULT_TEMPLATES}),
        "channels": ["Email", "SMS", "Both"],
    }
