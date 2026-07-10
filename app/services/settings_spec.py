import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import domain_settings as settings_service
from app.services.response import ListResponseMixin
from app.services.settings_cache import SettingsCache
from app.services.settings_specs.provisioning import build_provisioning_specs

if TYPE_CHECKING:
    from app.models.domain_settings import DomainSetting


logger = logging.getLogger(__name__)


def _coerce_int_value(value: object) -> int | None:
    # Domain settings values are stored as text/json and flow through `object` types here.
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class SettingSpec(ListResponseMixin):
    domain: SettingDomain
    key: str
    env_var: str | None
    value_type: SettingValueType
    default: object | None
    label: str | None = None
    required: bool = False
    allowed: set[str] | None = None
    min_value: int | None = None
    max_value: int | None = None
    is_secret: bool = False


SETTINGS_SPECS: list[SettingSpec] = [
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_autodetect_min_affected",
        label="Outage Wireless-Cluster Min Down Radios",
        env_var="OUTAGE_AUTODETECT_MIN_AFFECTED",
        value_type=SettingValueType.integer,
        default=3,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_autodetect_min_fraction_pct",
        label="Outage Wireless-Cluster Min Down Fraction (%)",
        env_var="OUTAGE_AUTODETECT_MIN_FRACTION_PCT",
        value_type=SettingValueType.integer,
        default=40,
        min_value=1,
        max_value=100,
    ),
    # --- Detected-outage incident reconcile (design §7.6) ------------------
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_reconcile_interval_seconds",
        label="Detected-Outage Reconcile Interval (seconds)",
        env_var="OUTAGE_RECONCILE_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=180,
        min_value=120,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_confirm_seconds_large",
        label="Outage Confirm Window — Large Impact (seconds)",
        env_var="OUTAGE_CONFIRM_SECONDS_LARGE",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_confirm_seconds_med",
        label="Outage Confirm Window — Medium Impact (seconds)",
        env_var="OUTAGE_CONFIRM_SECONDS_MED",
        value_type=SettingValueType.integer,
        default=360,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_confirm_seconds_small",
        label="Outage Confirm Window — Small Impact (seconds)",
        env_var="OUTAGE_CONFIRM_SECONDS_SMALL",
        value_type=SettingValueType.integer,
        default=600,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_confirm_threshold_med",
        label="Outage Confirm — Medium-Impact Threshold (affected)",
        env_var="OUTAGE_CONFIRM_THRESHOLD_MED",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_confirm_threshold_large",
        label="Outage Confirm — Large-Impact Threshold (affected)",
        env_var="OUTAGE_CONFIRM_THRESHOLD_LARGE",
        value_type=SettingValueType.integer,
        default=20,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_resolve_seconds",
        label="Outage Resolve Window — Sustained Recovery (seconds)",
        env_var="OUTAGE_RESOLVE_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="celery_long_running_task_minutes",
        label="Celery Long-running Task Threshold (minutes)",
        env_var="CELERY_LONG_RUNNING_TASK_MINUTES",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
        max_value=1440,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="celery_reserved_backlog_threshold",
        label="Celery Reserved Backlog Threshold",
        env_var="CELERY_RESERVED_BACKLOG_THRESHOLD",
        value_type=SettingValueType.integer,
        default=100,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="celery_queue_backlog_threshold",
        label="Celery Queue Backlog Threshold",
        env_var="CELERY_QUEUE_BACKLOG_THRESHOLD",
        value_type=SettingValueType.integer,
        default=500,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="dashboard_sync_healthy_age_seconds",
        label="Dashboard Sync Healthy Age (seconds)",
        env_var="DASHBOARD_SYNC_HEALTHY_AGE_SECONDS",
        value_type=SettingValueType.integer,
        default=7200,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="ont_signal_warning_dbm",
        label="ONT Signal Warning Threshold (dBm)",
        env_var="ONT_SIGNAL_WARNING_DBM",
        value_type=SettingValueType.integer,
        default=-25,
        min_value=-40,
        max_value=-5,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="ont_signal_critical_dbm",
        label="ONT Signal Critical Threshold (dBm)",
        env_var="ONT_SIGNAL_CRITICAL_DBM",
        value_type=SettingValueType.integer,
        default=-28,
        min_value=-40,
        max_value=-5,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="ont_signal_alert_cooldown_minutes",
        label="ONT Signal Alert Cooldown (minutes)",
        env_var="ONT_SIGNAL_ALERT_COOLDOWN_MINUTES",
        value_type=SettingValueType.integer,
        default=30,
        min_value=5,
        max_value=1440,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="olt_polling_interval_minutes",
        label="OLT Signal Polling Interval (minutes)",
        env_var="OLT_POLLING_INTERVAL_MINUTES",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
        max_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="core_device_ping_interval_seconds",
        label="Core Device Ping Refresh Interval (seconds)",
        env_var="CORE_DEVICE_PING_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=120,
        min_value=10,
        max_value=3600,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="core_device_snmp_walk_interval_seconds",
        label="Core Device SNMP Walk Interval (seconds)",
        env_var="CORE_DEVICE_SNMP_WALK_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=30,
        max_value=3600,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="pon_outage_min_offline_onus",
        label="PON Outage Min Offline ONUs",
        env_var="PON_OUTAGE_MIN_OFFLINE_ONUS",
        value_type=SettingValueType.integer,
        default=2,
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="ont_offline_poll_threshold",
        label="ONT Offline Poll Threshold",
        env_var="ONT_OFFLINE_POLL_THRESHOLD",
        value_type=SettingValueType.integer,
        default=2,
        min_value=1,
        max_value=10,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="topology_metrics_interval_seconds",
        label="Topology Metrics Export Interval (seconds)",
        env_var="TOPOLOGY_METRICS_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=900,
        min_value=300,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="default_auth_port",
        env_var="RADIUS_DEFAULT_AUTH_PORT",
        value_type=SettingValueType.integer,
        default=1812,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="default_acct_port",
        env_var="RADIUS_DEFAULT_ACCT_PORT",
        value_type=SettingValueType.integer,
        default=1813,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="default_sync_users",
        env_var="RADIUS_DEFAULT_SYNC_USERS",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="default_sync_nas_clients",
        env_var="RADIUS_DEFAULT_SYNC_NAS_CLIENTS",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="default_sync_status",
        env_var="RADIUS_DEFAULT_SYNC_STATUS",
        value_type=SettingValueType.string,
        default="running",
        allowed={"running", "success", "failed"},
    ),
    SettingSpec(
        domain=SettingDomain.inventory,
        key="default_reservation_status",
        env_var="INVENTORY_DEFAULT_RESERVATION_STATUS",
        value_type=SettingValueType.string,
        default="active",
        allowed={"active", "released", "consumed"},
    ),
    SettingSpec(
        domain=SettingDomain.inventory,
        key="default_material_status",
        env_var="INVENTORY_DEFAULT_MATERIAL_STATUS",
        value_type=SettingValueType.string,
        default="required",
        allowed={"required", "reserved", "used"},
    ),
    SettingSpec(
        domain=SettingDomain.lifecycle,
        key="default_event_type",
        env_var="LIFECYCLE_DEFAULT_EVENT_TYPE",
        value_type=SettingValueType.string,
        default="other",
        allowed={
            "activate",
            "suspend",
            "resume",
            "cancel",
            "upgrade",
            "downgrade",
            "change_address",
            "other",
        },
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="default_notification_status",
        env_var="COMMS_DEFAULT_NOTIFICATION_STATUS",
        value_type=SettingValueType.string,
        default="pending",
        allowed={"pending", "sent", "failed"},
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="sidebar_logo_url",
        env_var="SIDEBAR_LOGO_URL",
        value_type=SettingValueType.string,
        default="",
        label="Sidebar Logo URL",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="sidebar_logo_dark_url",
        env_var="SIDEBAR_LOGO_DARK_URL",
        value_type=SettingValueType.string,
        default="",
        label="Sidebar Dark Mode Logo URL",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="favicon_url",
        env_var="FAVICON_URL",
        value_type=SettingValueType.string,
        default="",
        label="Favicon URL",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="brand_primary_color",
        env_var="BRAND_PRIMARY_COLOR_OVERRIDE",
        value_type=SettingValueType.string,
        default="#206a07",
        label="Brand Primary Colour",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="brand_secondary_color",
        env_var="BRAND_SECONDARY_COLOR_OVERRIDE",
        value_type=SettingValueType.string,
        default="#06b6d4",
        label="Brand Secondary Colour",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="login_hero_customer_url",
        env_var="LOGIN_HERO_CUSTOMER_URL",
        value_type=SettingValueType.string,
        default="",
        label="Customer Login Image URL",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="login_hero_reseller_url",
        env_var="LOGIN_HERO_RESELLER_URL",
        value_type=SettingValueType.string,
        default="",
        label="Reseller Login Image URL",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="login_hero_admin_url",
        env_var="LOGIN_HERO_ADMIN_URL",
        value_type=SettingValueType.string,
        default="",
        label="Admin Login Image URL",
    ),
    # Meta (Facebook/Instagram) Integration Settings
    SettingSpec(
        domain=SettingDomain.comms,
        key="meta_app_id",
        env_var="META_APP_ID",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="meta_app_secret",
        env_var="META_APP_SECRET",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="meta_webhook_verify_token",
        env_var="META_WEBHOOK_VERIFY_TOKEN",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="meta_oauth_redirect_uri",
        env_var="META_OAUTH_REDIRECT_URI",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="meta_graph_api_version",
        env_var="META_GRAPH_API_VERSION",
        value_type=SettingValueType.string,
        default="v21.0",
        label="Meta Graph API Version",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="meta_access_token_override",
        env_var="META_ACCESS_TOKEN_OVERRIDE",
        value_type=SettingValueType.string,
        default=None,
        label="Meta Access Token (Override)",
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="whatsapp_provider",
        env_var="WHATSAPP_PROVIDER",
        value_type=SettingValueType.string,
        default="meta_cloud_api",
        allowed={"meta_cloud_api", "twilio", "messagebird"},
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="whatsapp_api_key",
        env_var="WHATSAPP_API_KEY",
        value_type=SettingValueType.string,
        default="",
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="whatsapp_api_secret",
        env_var="WHATSAPP_API_SECRET",
        value_type=SettingValueType.string,
        default="",
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="whatsapp_phone_number",
        env_var="WHATSAPP_PHONE_NUMBER",
        value_type=SettingValueType.string,
        default="",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="whatsapp_waba_id",
        env_var="WHATSAPP_WABA_ID",
        value_type=SettingValueType.string,
        default="",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="whatsapp_webhook_url",
        env_var="WHATSAPP_WEBHOOK_URL",
        value_type=SettingValueType.string,
        default="",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="whatsapp_message_templates",
        env_var="WHATSAPP_MESSAGE_TEMPLATES_JSON",
        value_type=SettingValueType.json,
        default=[],
    ),
    # ============== Auth Domain: Customer/Reseller/Vendor Portal Auth ==============
    SettingSpec(
        domain=SettingDomain.auth,
        key="customer_session_ttl_seconds",
        env_var="CUSTOMER_SESSION_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="customer_remember_ttl_seconds",
        env_var="CUSTOMER_REMEMBER_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=2592000,
        min_value=86400,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="customer_session_absolute_ttl_seconds",
        env_var="CUSTOMER_SESSION_ABSOLUTE_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=2592000,
        min_value=3600,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="customer_login_max_attempts",
        env_var="CUSTOMER_LOGIN_MAX_ATTEMPTS",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="customer_lockout_minutes",
        env_var="CUSTOMER_LOCKOUT_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="admin_mfa_required",
        env_var="ADMIN_MFA_REQUIRED",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="admin_login_max_attempts",
        env_var="ADMIN_LOGIN_MAX_ATTEMPTS",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="admin_lockout_minutes",
        env_var="ADMIN_LOCKOUT_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="mfa_max_failed_attempts",
        env_var="MFA_MAX_FAILED_ATTEMPTS",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="mfa_lockout_minutes",
        env_var="MFA_LOCKOUT_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="password_min_length",
        env_var="PASSWORD_MIN_LENGTH",
        value_type=SettingValueType.integer,
        default=8,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="reseller_session_ttl_seconds",
        env_var="RESELLER_SESSION_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="reseller_remember_ttl_seconds",
        env_var="RESELLER_REMEMBER_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=2592000,
        min_value=86400,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="reseller_session_absolute_ttl_seconds",
        env_var="RESELLER_SESSION_ABSOLUTE_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=2592000,
        min_value=3600,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="vendor_session_ttl_seconds",
        env_var="VENDOR_SESSION_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="vendor_remember_ttl_seconds",
        env_var="VENDOR_REMEMBER_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=2592000,
        min_value=86400,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="password_reset_expiry_minutes",
        env_var="PASSWORD_RESET_EXPIRY_MINUTES",
        value_type=SettingValueType.integer,
        default=1440,
        min_value=5,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="user_invite_expiry_minutes",
        env_var="USER_INVITE_EXPIRY_MINUTES",
        value_type=SettingValueType.integer,
        default=1440,
        min_value=5,
    ),
    # ============== Comms Domain: External API Timeouts ==============
    SettingSpec(
        domain=SettingDomain.comms,
        key="meta_api_timeout_seconds",
        env_var="META_API_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="whatsapp_api_timeout_seconds",
        env_var="WHATSAPP_API_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=10,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="nextcloud_talk_timeout_seconds",
        env_var="NEXTCLOUD_TALK_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
    ),
    # ============== Notification Domain: Email Settings ==============
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_test_timeout_seconds",
        env_var="SMTP_TEST_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=10,
        min_value=1,
    ),
    # ============== Bandwidth Domain: Bandwidth Processing ==============
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="stream_interval_seconds",
        env_var="BANDWIDTH_STREAM_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="aggregate_interval_seconds",
        env_var="BANDWIDTH_AGGREGATE_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=10,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="cleanup_interval_seconds",
        env_var="BANDWIDTH_CLEANUP_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=3600,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="trim_interval_seconds",
        env_var="BANDWIDTH_TRIM_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=600,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="hot_retention_hours",
        env_var="BANDWIDTH_HOT_RETENTION_HOURS",
        value_type=SettingValueType.integer,
        default=24,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="batch_size",
        env_var="BANDWIDTH_BATCH_SIZE",
        value_type=SettingValueType.integer,
        default=1000,
        min_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="redis_stream_max_length",
        env_var="BANDWIDTH_REDIS_STREAM_MAX_LENGTH",
        value_type=SettingValueType.integer,
        default=100000,
        min_value=1000,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="redis_read_timeout_ms",
        env_var="BANDWIDTH_REDIS_READ_TIMEOUT_MS",
        value_type=SettingValueType.integer,
        default=1000,
        min_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="victoriametrics_timeout_seconds",
        env_var="BANDWIDTH_VICTORIAMETRICS_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
    ),
    # ============== Network Domain: VPN/WireGuard ==============
    SettingSpec(
        domain=SettingDomain.network,
        key="vpn_cache_default_ttl_seconds",
        env_var="VPN_CACHE_DEFAULT_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=900,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_log_retention_days",
        env_var="WIREGUARD_LOG_RETENTION_DAYS",
        value_type=SettingValueType.integer,
        default=90,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_key_encryption_key",
        env_var="WIREGUARD_KEY_ENCRYPTION_KEY",
        value_type=SettingValueType.string,
        default=None,
        required=True,
        is_secret=True,
        label="WireGuard Key Encryption Key",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_log_cleanup_enabled",
        env_var="WIREGUARD_LOG_CLEANUP_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="WireGuard Log Cleanup Enabled",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_log_cleanup_interval_seconds",
        env_var="WIREGUARD_LOG_CLEANUP_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=3600,
        label="WireGuard Log Cleanup Interval (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_token_cleanup_enabled",
        env_var="WIREGUARD_TOKEN_CLEANUP_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="WireGuard Token Cleanup Enabled",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_token_cleanup_interval_seconds",
        env_var="WIREGUARD_TOKEN_CLEANUP_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=3600,
        min_value=300,
        label="WireGuard Token Cleanup Interval (seconds)",
    ),
    # -------------- VPN Server Defaults --------------
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_default_listen_port",
        env_var="WIREGUARD_DEFAULT_LISTEN_PORT",
        value_type=SettingValueType.integer,
        default=51820,
        min_value=1,
        max_value=65535,
        label="VPN Default Listen Port",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_default_vpn_address",
        env_var="WIREGUARD_DEFAULT_VPN_ADDRESS",
        value_type=SettingValueType.string,
        default="10.10.0.1/24",
        label="VPN Default Address (CIDR)",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_default_vpn_address_v6",
        env_var="WIREGUARD_DEFAULT_VPN_ADDRESS_V6",
        value_type=SettingValueType.string,
        default=None,
        label="VPN Default IPv6 Address (CIDR)",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_default_mtu",
        env_var="WIREGUARD_DEFAULT_MTU",
        value_type=SettingValueType.integer,
        default=1420,
        min_value=1280,
        max_value=9000,
        label="VPN Default MTU",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_default_interface_name",
        env_var="WIREGUARD_DEFAULT_INTERFACE_NAME",
        value_type=SettingValueType.string,
        default="wg0",
        label="VPN Default Interface Name",
    ),
    # ============== Provisioning Domain ==============
    *build_provisioning_specs(SettingSpec),
    SettingSpec(
        domain=SettingDomain.snmp,
        key="interface_walk_interval_seconds",
        env_var="SNMP_INTERFACE_WALK_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=30,
    ),
    SettingSpec(
        domain=SettingDomain.snmp,
        key="interface_discovery_interval_seconds",
        env_var="SNMP_INTERFACE_DISCOVERY_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=3600,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.tr069,
        key="default_acs_server_id",
        env_var="TR069_DEFAULT_ACS_SERVER_ID",
        value_type=SettingValueType.string,
        default=None,
        label="Default TR-069 ACS Server ID",
    ),
    # ── Network Monitoring ─────────────────────────────────────────────
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="device_metrics_retention_days",
        env_var="DEVICE_METRICS_RETENTION_DAYS",
        value_type=SettingValueType.integer,
        default=90,
        label="Device Metrics Retention (days)",
        min_value=7,
        max_value=3650,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="alert_evaluation_interval_seconds",
        env_var="ALERT_EVALUATION_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        label="Alert Evaluation Interval (seconds)",
        min_value=10,
        max_value=600,
    ),
]

DOMAIN_SETTINGS_SERVICE = {
    SettingDomain.auth: settings_service.auth_settings,
    SettingDomain.audit: settings_service.audit_settings,
    SettingDomain.billing: settings_service.billing_settings,
    SettingDomain.catalog: settings_service.catalog_settings,
    SettingDomain.subscriber: settings_service.subscriber_settings,
    SettingDomain.imports: settings_service.imports_settings,
    SettingDomain.notification: settings_service.notification_settings,
    SettingDomain.network: settings_service.network_settings,
    SettingDomain.network_monitoring: settings_service.network_monitoring_settings,
    SettingDomain.provisioning: settings_service.provisioning_settings,
    SettingDomain.geocoding: settings_service.geocoding_settings,
    SettingDomain.usage: settings_service.usage_settings,
    SettingDomain.radius: settings_service.radius_settings,
    SettingDomain.collections: settings_service.collections_settings,
    SettingDomain.lifecycle: settings_service.lifecycle_settings,
    SettingDomain.inventory: settings_service.inventory_settings,
    SettingDomain.comms: settings_service.comms_settings,
    SettingDomain.tr069: settings_service.tr069_settings,
    SettingDomain.snmp: settings_service.snmp_settings,
    SettingDomain.bandwidth: settings_service.bandwidth_settings,
    SettingDomain.subscription_engine: settings_service.subscription_engine_settings,
    SettingDomain.gis: settings_service.gis_settings,
    SettingDomain.scheduler: settings_service.scheduler_settings,
    SettingDomain.vas: settings_service.vas_settings,
}


def get_spec(domain: SettingDomain, key: str) -> SettingSpec | None:
    for spec in SETTINGS_SPECS:
        if spec.domain == domain and spec.key == key:
            return spec
    return None


def list_specs(domain: SettingDomain) -> list[SettingSpec]:
    return [spec for spec in SETTINGS_SPECS if spec.domain == domain]


def resolve_value(db, domain: SettingDomain, key: str) -> Any:
    """Resolve a setting value with Redis caching.

    Checks Redis cache first, falls back to database query, then caches result.
    """
    spec = get_spec(domain, key)
    if not spec:
        return None

    # 1. Check cache first
    cached = SettingsCache.get(domain.value, key)
    if cached is not None:
        return cast(object, cached)

    # 2. Query database
    service = DOMAIN_SETTINGS_SERVICE.get(domain)
    setting = None
    if service:
        try:
            setting = service.get_by_key(db, key)
        except HTTPException:
            setting = None
    raw = extract_db_value(setting)
    if raw is None:
        raw = spec.default
    value, error = coerce_value(spec, raw)
    if error:
        value = spec.default
    if spec.allowed and value is not None and value not in spec.allowed:
        value = spec.default
    if spec.value_type == SettingValueType.integer and value is not None:
        parsed = _coerce_int_value(value)
        if parsed is None:
            parsed = spec.default if isinstance(spec.default, int) else None
        if (
            spec.min_value is not None
            and parsed is not None
            and parsed < spec.min_value
        ):
            parsed = spec.default if isinstance(spec.default, int) else None
        if (
            spec.max_value is not None
            and parsed is not None
            and parsed > spec.max_value
        ):
            parsed = spec.default if isinstance(spec.default, int) else None
        value = parsed

    # 3. Cache the result (only non-None values)
    if value is not None:
        SettingsCache.set(domain.value, key, value)

    return value


def resolve_values_atomic(db, domain: SettingDomain, keys: list[str]) -> dict[str, Any]:
    """Read multiple settings atomically to prevent race conditions.

    This function retrieves multiple settings in a single database query,
    preventing inconsistent reads that can occur when settings are read
    one at a time while another process is updating them.

    Args:
        db: Database session
        domain: The setting domain
        keys: List of setting keys to retrieve

    Returns:
        Dict mapping keys to their resolved values (missing keys are omitted)
    """
    if not keys:
        return {}

    # 1. Check cache for all keys
    cached = SettingsCache.get_multi(domain.value, keys)
    missing_keys = [k for k in keys if k not in cached]

    if not missing_keys:
        return cached

    # 2. Query database for missing keys in single query
    service = DOMAIN_SETTINGS_SERVICE.get(domain)
    if not service:
        return cached

    from app.models.domain_settings import DomainSetting

    settings = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == domain)
        .filter(DomainSetting.key.in_(missing_keys))
        .all()
    )

    settings_by_key = {s.key: s for s in settings}
    to_cache: dict[str, Any] = {}

    for key in missing_keys:
        spec = get_spec(domain, key)
        if not spec:
            continue

        setting = settings_by_key.get(key)
        raw = extract_db_value(setting)
        if raw is None:
            raw = spec.default
        value, error = coerce_value(spec, raw)
        if error:
            value = spec.default
        if spec.allowed and value is not None and value not in spec.allowed:
            value = spec.default
        if spec.value_type == SettingValueType.integer and value is not None:
            parsed = _coerce_int_value(value)
            if parsed is None:
                parsed = spec.default if isinstance(spec.default, int) else None
            if (
                spec.min_value is not None
                and parsed is not None
                and parsed < spec.min_value
            ):
                parsed = spec.default if isinstance(spec.default, int) else None
            if (
                spec.max_value is not None
                and parsed is not None
                and parsed > spec.max_value
            ):
                parsed = spec.default if isinstance(spec.default, int) else None
            value = parsed

        if value is not None:
            cached[key] = value
            to_cache[key] = value

    # 3. Cache all retrieved values atomically
    if to_cache:
        SettingsCache.set_multi(domain.value, to_cache)

    return cached


def extract_db_value(setting: "DomainSetting | None") -> object | None:
    if not setting:
        return None
    if setting.value_text is not None:
        return cast(object, setting.value_text)
    if setting.value_json is not None:
        return cast(object, setting.value_json)
    return None


def coerce_value(spec: SettingSpec, raw: object) -> tuple[object | None, str | None]:
    if raw is None:
        return None, None
    if spec.value_type == SettingValueType.boolean:
        if isinstance(raw, bool):
            return raw, None
        if isinstance(raw, str):
            normalized = raw.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True, None
            if normalized in {"0", "false", "no", "off"}:
                return False, None
        return None, "Value must be boolean"
    if spec.value_type == SettingValueType.integer:
        if isinstance(raw, int):
            return raw, None
        if isinstance(raw, str):
            try:
                return int(raw), None
            except ValueError:
                return None, "Value must be an integer"
        return None, "Value must be an integer"
    if spec.value_type == SettingValueType.string:
        if isinstance(raw, str):
            return raw, None
        return str(raw), None
    return raw, None


def normalize_for_db(
    spec: SettingSpec, value: object
) -> tuple[str | None, object | None]:
    if spec.value_type == SettingValueType.boolean:
        bool_value = bool(value)
        return ("true" if bool_value else "false"), None
    if spec.value_type == SettingValueType.integer:
        parsed = _coerce_int_value(value)
        if parsed is None:
            # Should be prevented by validation in callers, but avoid crashing on bad inputs.
            return None, value
        return str(parsed), None
    if spec.value_type == SettingValueType.string:
        return str(value), None
    return None, value
