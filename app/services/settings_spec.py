from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import domain_settings as settings_service
from app.services.response import ListResponseMixin
from app.services.settings_cache import SettingsCache


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
        domain=SettingDomain.auth,
        key="jwt_secret",
        label=None,
        env_var="JWT_SECRET",
        value_type=SettingValueType.string,
        default=None,
        required=True,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="jwt_algorithm",
        label=None,
        env_var="JWT_ALGORITHM",
        value_type=SettingValueType.string,
        default="HS256",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="jwt_access_ttl_minutes",
        env_var="JWT_ACCESS_TTL_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="jwt_refresh_ttl_days",
        env_var="JWT_REFRESH_TTL_DAYS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_name",
        env_var="REFRESH_COOKIE_NAME",
        value_type=SettingValueType.string,
        default="refresh_token",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_secure",
        env_var="REFRESH_COOKIE_SECURE",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_samesite",
        env_var="REFRESH_COOKIE_SAMESITE",
        value_type=SettingValueType.string,
        default="lax",
        allowed={"lax", "strict", "none"},
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_domain",
        env_var="REFRESH_COOKIE_DOMAIN",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_path",
        env_var="REFRESH_COOKIE_PATH",
        value_type=SettingValueType.string,
        default="/auth",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="totp_issuer",
        env_var="TOTP_ISSUER",
        value_type=SettingValueType.string,
        default="dotmac_sm",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="totp_encryption_key",
        env_var="TOTP_ENCRYPTION_KEY",
        value_type=SettingValueType.string,
        default=None,
        required=True,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="api_key_rate_window_seconds",
        env_var="API_KEY_RATE_WINDOW_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="api_key_rate_max",
        env_var="API_KEY_RATE_MAX",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="sync_enabled",
        env_var="GIS_SYNC_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="sync_interval_minutes",
        env_var="GIS_SYNC_INTERVAL_MINUTES",
        value_type=SettingValueType.integer,
        default=60,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="sync_pop_sites",
        env_var="GIS_SYNC_POP_SITES",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="sync_addresses",
        env_var="GIS_SYNC_ADDRESSES",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="sync_deactivate_missing",
        env_var="GIS_SYNC_DEACTIVATE_MISSING",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="map_customer_limit",
        env_var="GIS_MAP_CUSTOMER_LIMIT",
        value_type=SettingValueType.integer,
        default=2000,
        min_value=100,
        max_value=50000,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="map_nearest_search_max_km",
        env_var="GIS_MAP_NEAREST_SEARCH_MAX_KM",
        value_type=SettingValueType.integer,
        default=50,
        min_value=1,
        max_value=1000,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="map_snap_max_m",
        env_var="GIS_MAP_SNAP_MAX_M",
        value_type=SettingValueType.integer,
        default=250,
        min_value=10,
        max_value=5000,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="map_allow_straightline_fallback",
        env_var="GIS_MAP_ALLOW_STRAIGHTLINE_FALLBACK",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="alert_notifications_enabled",
        env_var="ALERT_NOTIFICATIONS_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="alert_notifications_default_channel",
        env_var="ALERT_NOTIFICATIONS_DEFAULT_CHANNEL",
        value_type=SettingValueType.string,
        default="email",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="alert_notifications_default_recipient",
        env_var="ALERT_NOTIFICATIONS_DEFAULT_RECIPIENT",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="alert_notifications_default_template_id",
        env_var="ALERT_NOTIFICATIONS_DEFAULT_TEMPLATE_ID",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="alert_notifications_default_rotation_id",
        env_var="ALERT_NOTIFICATIONS_DEFAULT_ROTATION_ID",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="alert_notifications_default_delay_minutes",
        env_var="ALERT_NOTIFICATIONS_DEFAULT_DELAY_MINUTES",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_host",
        env_var="SMTP_HOST",
        value_type=SettingValueType.string,
        default="localhost",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_port",
        env_var="SMTP_PORT",
        value_type=SettingValueType.integer,
        default=587,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_username",
        env_var="SMTP_USERNAME",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_password",
        env_var="SMTP_PASSWORD",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_from_email",
        env_var="SMTP_FROM_EMAIL",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_from_name",
        env_var="SMTP_FROM_NAME",
        value_type=SettingValueType.string,
        default="DotMac SM",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_use_tls",
        env_var="SMTP_USE_TLS",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_use_ssl",
        env_var="SMTP_USE_SSL",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="notification_queue_enabled",
        env_var="NOTIFICATION_QUEUE_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="notification_queue_interval_seconds",
        env_var="NOTIFICATION_QUEUE_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=10,
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="usage_rating_enabled",
        env_var="USAGE_RATING_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="usage_rating_interval_seconds",
        env_var="USAGE_RATING_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=300,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="dunning_enabled",
        env_var="DUNNING_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="dunning_interval_seconds",
        env_var="DUNNING_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_enforcement_enabled",
        env_var="PREPAID_ENFORCEMENT_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_enforcement_interval_seconds",
        env_var="PREPAID_ENFORCEMENT_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=3600,
        min_value=300,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_blocking_time",
        env_var="PREPAID_BLOCKING_TIME",
        value_type=SettingValueType.string,
        default="08:00",
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_skip_weekends",
        env_var="PREPAID_SKIP_WEEKENDS",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_skip_holidays",
        env_var="PREPAID_SKIP_HOLIDAYS",
        value_type=SettingValueType.json,
        default=[],
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_grace_days",
        env_var="PREPAID_GRACE_DAYS",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_deactivation_days",
        env_var="PREPAID_DEACTIVATION_DAYS",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_default_min_balance",
        env_var="PREPAID_DEFAULT_MIN_BALANCE",
        value_type=SettingValueType.string,
        default="0.00",
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_warning_subject",
        env_var="PREPAID_WARNING_SUBJECT",
        value_type=SettingValueType.string,
        default="Low Balance Warning",
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_warning_body",
        env_var="PREPAID_WARNING_BODY",
        value_type=SettingValueType.string,
        default="Your prepaid balance is below the minimum threshold ({threshold}). "
                "Current balance: {balance}. Please top up to avoid suspension.",
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_deactivation_subject",
        env_var="PREPAID_DEACTIVATION_SUBJECT",
        value_type=SettingValueType.string,
        default="Service Deactivated",
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_deactivation_body",
        env_var="PREPAID_DEACTIVATION_BODY",
        value_type=SettingValueType.string,
        default="Your prepaid balance has been exhausted and service has been deactivated. "
                "Please contact support to restore service.",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_billing_mode",
        env_var="CATALOG_DEFAULT_BILLING_MODE",
        value_type=SettingValueType.string,
        default="prepaid",
        allowed={"prepaid", "postpaid"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="billing_mode_help_text",
        env_var="CATALOG_BILLING_MODE_HELP_TEXT",
        value_type=SettingValueType.string,
        default="Overrides tariff default.",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="billing_mode_prepaid_notice",
        env_var="CATALOG_BILLING_MODE_PREPAID_NOTICE",
        value_type=SettingValueType.string,
        default="Balance enforcement applies.",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="billing_mode_postpaid_notice",
        env_var="CATALOG_BILLING_MODE_POSTPAID_NOTICE",
        value_type=SettingValueType.string,
        default="This subscription follows dunning steps.",
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="enabled",
        env_var="GEOCODING_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="provider",
        env_var="GEOCODING_PROVIDER",
        value_type=SettingValueType.string,
        default="nominatim",
        allowed={"nominatim"},
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="base_url",
        env_var="GEOCODING_BASE_URL",
        value_type=SettingValueType.string,
        default="https://nominatim.openstreetmap.org",
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="user_agent",
        env_var="GEOCODING_USER_AGENT",
        value_type=SettingValueType.string,
        default="dotmac_sm",
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="email",
        env_var="GEOCODING_EMAIL",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="timeout_sec",
        env_var="GEOCODING_TIMEOUT_SEC",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="broker_url",
        env_var="CELERY_BROKER_URL",
        value_type=SettingValueType.string,
        default="redis://localhost:6379/0",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="result_backend",
        env_var="CELERY_RESULT_BACKEND",
        value_type=SettingValueType.string,
        default="redis://localhost:6379/1",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="timezone",
        env_var="CELERY_TIMEZONE",
        value_type=SettingValueType.string,
        default="UTC",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="beat_max_loop_interval",
        env_var="CELERY_BEAT_MAX_LOOP_INTERVAL",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="beat_refresh_seconds",
        env_var="CELERY_BEAT_REFRESH_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=5,
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="refresh_minutes",
        env_var="CELERY_BEAT_REFRESH_MINUTES",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="auth_server_id",
        env_var="RADIUS_AUTH_SERVER_ID",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="auth_shared_secret",
        env_var="RADIUS_AUTH_SHARED_SECRET",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="auth_dictionary_path",
        env_var="RADIUS_AUTH_DICTIONARY",
        value_type=SettingValueType.string,
        default="/etc/raddb/dictionary",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="auth_timeout_sec",
        env_var="RADIUS_AUTH_TIMEOUT_SEC",
        value_type=SettingValueType.integer,
        default=3,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="coa_enabled",
        env_var="RADIUS_COA_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable RADIUS CoA Disconnects",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="coa_dictionary_path",
        env_var="RADIUS_COA_DICTIONARY",
        value_type=SettingValueType.string,
        default="/etc/raddb/dictionary",
        label="CoA Dictionary Path",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="coa_timeout_sec",
        env_var="RADIUS_COA_TIMEOUT_SEC",
        value_type=SettingValueType.integer,
        default=3,
        min_value=1,
        label="CoA Timeout (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="coa_retries",
        env_var="RADIUS_COA_RETRIES",
        value_type=SettingValueType.integer,
        default=1,
        min_value=0,
        label="CoA Retries",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="refresh_sessions_on_profile_change",
        env_var="RADIUS_REFRESH_SESSIONS_ON_PROFILE_CHANGE",
        value_type=SettingValueType.boolean,
        default=True,
        label="Refresh Sessions on Profile Change",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_currency",
        env_var="BILLING_DEFAULT_CURRENCY",
        value_type=SettingValueType.string,
        default="NGN",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_invoice_status",
        env_var="BILLING_DEFAULT_INVOICE_STATUS",
        value_type=SettingValueType.string,
        default="draft",
        allowed={"draft", "issued", "partially_paid", "paid", "void", "overdue"},
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_tax_application",
        env_var="BILLING_DEFAULT_TAX_APPLICATION",
        value_type=SettingValueType.string,
        default="exclusive",
        allowed={"exclusive", "inclusive", "exempt"},
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_payment_method_type",
        env_var="BILLING_DEFAULT_PAYMENT_METHOD_TYPE",
        value_type=SettingValueType.string,
        default="card",
        allowed={"card", "bank_account", "cash", "check", "transfer", "other"},
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_payment_provider_type",
        env_var="BILLING_DEFAULT_PAYMENT_PROVIDER_TYPE",
        value_type=SettingValueType.string,
        default="custom",
        allowed={"stripe", "paypal", "manual", "custom"},
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_bank_account_type",
        env_var="BILLING_DEFAULT_BANK_ACCOUNT_TYPE",
        value_type=SettingValueType.string,
        default="checking",
        allowed={"checking", "savings", "business", "other"},
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_payment_status",
        env_var="BILLING_DEFAULT_PAYMENT_STATUS",
        value_type=SettingValueType.string,
        default="pending",
        allowed={"pending", "succeeded", "failed", "refunded", "canceled"},
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="billing_enabled",
        env_var="BILLING_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="billing_interval_seconds",
        env_var="BILLING_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=300,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="invoice_due_days",
        env_var="BILLING_INVOICE_DUE_DAYS",
        value_type=SettingValueType.integer,
        default=14,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="proration_enabled",
        env_var="BILLING_PRORATION_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable Proration",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="auto_activate_pending_on_billing",
        env_var="BILLING_AUTO_ACTIVATE_PENDING",
        value_type=SettingValueType.boolean,
        default=True,
        label="Auto-activate Pending Subscriptions on Billing",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="invoice_number_enabled",
        env_var="BILLING_INVOICE_NUMBER_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="invoice_number_prefix",
        env_var="BILLING_INVOICE_NUMBER_PREFIX",
        value_type=SettingValueType.string,
        default="INV-",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="invoice_number_padding",
        env_var="BILLING_INVOICE_NUMBER_PADDING",
        value_type=SettingValueType.integer,
        default=6,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="invoice_number_start",
        env_var="BILLING_INVOICE_NUMBER_START",
        value_type=SettingValueType.integer,
        default=1,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="credit_note_number_enabled",
        env_var="BILLING_CREDIT_NOTE_NUMBER_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="credit_note_number_prefix",
        env_var="BILLING_CREDIT_NOTE_NUMBER_PREFIX",
        value_type=SettingValueType.string,
        default="CR-",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="credit_note_number_padding",
        env_var="BILLING_CREDIT_NOTE_NUMBER_PADDING",
        value_type=SettingValueType.integer,
        default=6,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="credit_note_number_start",
        env_var="BILLING_CREDIT_NOTE_NUMBER_START",
        value_type=SettingValueType.integer,
        default=1,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_proration_policy",
        env_var="CATALOG_DEFAULT_PRORATION_POLICY",
        value_type=SettingValueType.string,
        default="immediate",
        allowed={"immediate", "next_cycle", "none"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_downgrade_policy",
        env_var="CATALOG_DEFAULT_DOWNGRADE_POLICY",
        value_type=SettingValueType.string,
        default="next_cycle",
        allowed={"immediate", "next_cycle", "none"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_suspension_action",
        env_var="CATALOG_DEFAULT_SUSPENSION_ACTION",
        value_type=SettingValueType.string,
        default="suspend",
        allowed={"none", "throttle", "suspend", "reject"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_refund_policy",
        env_var="CATALOG_DEFAULT_REFUND_POLICY",
        value_type=SettingValueType.string,
        default="none",
        allowed={"none", "prorated", "full_within_days"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="subscription_expiration_enabled",
        env_var="SUBSCRIPTION_EXPIRATION_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable Subscription Expiration Task",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="subscription_expiration_interval_seconds",
        env_var="SUBSCRIPTION_EXPIRATION_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,  # Daily
        min_value=3600,  # Minimum 1 hour
        label="Subscription Expiration Check Interval (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_billing_cycle",
        env_var="CATALOG_DEFAULT_BILLING_CYCLE",
        value_type=SettingValueType.string,
        default="monthly",
        allowed={"monthly", "annual"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_contract_term",
        env_var="CATALOG_DEFAULT_CONTRACT_TERM",
        value_type=SettingValueType.string,
        default="month_to_month",
        allowed={"month_to_month", "twelve_month", "twentyfour_month"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_offer_status",
        env_var="CATALOG_DEFAULT_OFFER_STATUS",
        value_type=SettingValueType.string,
        default="active",
        allowed={"active", "inactive", "archived"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_subscription_status",
        env_var="CATALOG_DEFAULT_SUBSCRIPTION_STATUS",
        value_type=SettingValueType.string,
        default="pending",
        allowed={"pending", "active", "suspended", "canceled", "expired"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_price_type",
        env_var="CATALOG_DEFAULT_PRICE_TYPE",
        value_type=SettingValueType.string,
        default="recurring",
        allowed={"recurring", "one_time", "usage"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_addon_type",
        env_var="CATALOG_DEFAULT_ADDON_TYPE",
        value_type=SettingValueType.string,
        default="custom",
        allowed={
            "static_ip",
            "router_rental",
            "install_fee",
            "premium_support",
            "extra_ip",
            "managed_wifi",
            "custom",
        },
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_addon_quantity",
        env_var="CATALOG_DEFAULT_ADDON_QUANTITY",
        value_type=SettingValueType.integer,
        default=1,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_nas_vendor",
        env_var="CATALOG_DEFAULT_NAS_VENDOR",
        value_type=SettingValueType.string,
        default="other",
        allowed={"mikrotik", "cisco", "other"},
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="default_account_status",
        env_var="SUBSCRIBER_DEFAULT_ACCOUNT_STATUS",
        value_type=SettingValueType.string,
        default="active",
        allowed={"active", "suspended", "canceled", "delinquent"},
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="default_address_type",
        env_var="SUBSCRIBER_DEFAULT_ADDRESS_TYPE",
        value_type=SettingValueType.string,
        default="service",
        allowed={"service", "billing", "mailing"},
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="default_contact_role",
        env_var="SUBSCRIBER_DEFAULT_CONTACT_ROLE",
        value_type=SettingValueType.string,
        default="primary",
        allowed={"primary", "billing", "technical", "support"},
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="subscriber_number_enabled",
        env_var="SUBSCRIBER_NUMBER_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="subscriber_number_prefix",
        env_var="SUBSCRIBER_NUMBER_PREFIX",
        value_type=SettingValueType.string,
        default="SUB-",
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="subscriber_number_padding",
        env_var="SUBSCRIBER_NUMBER_PADDING",
        value_type=SettingValueType.integer,
        default=6,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="subscriber_number_start",
        env_var="SUBSCRIBER_NUMBER_START",
        value_type=SettingValueType.integer,
        default=1,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="account_number_enabled",
        env_var="SUBSCRIBER_ACCOUNT_NUMBER_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="account_number_prefix",
        env_var="SUBSCRIBER_ACCOUNT_NUMBER_PREFIX",
        value_type=SettingValueType.string,
        default="ACC-",
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="account_number_padding",
        env_var="SUBSCRIBER_ACCOUNT_NUMBER_PADDING",
        value_type=SettingValueType.integer,
        default=6,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="account_number_start",
        env_var="SUBSCRIBER_ACCOUNT_NUMBER_START",
        value_type=SettingValueType.integer,
        default=1,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="default_charge_status",
        env_var="USAGE_DEFAULT_CHARGE_STATUS",
        value_type=SettingValueType.string,
        default="staged",
        allowed={"staged", "posted", "needs_review", "skipped"},
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="default_rating_run_status",
        env_var="USAGE_DEFAULT_RATING_RUN_STATUS",
        value_type=SettingValueType.string,
        default="running",
        allowed={"running", "success", "failed"},
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="usage_warning_enabled",
        env_var="USAGE_WARNING_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable Usage Warning Events",
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="usage_warning_thresholds",
        env_var="USAGE_WARNING_THRESHOLDS",
        value_type=SettingValueType.string,
        default="0.8,0.9",
        label="Usage Warning Thresholds",
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="fup_throttle_radius_profile_id",
        env_var="USAGE_FUP_THROTTLE_RADIUS_PROFILE_ID",
        value_type=SettingValueType.string,
        default=None,
        label="FUP Throttle RADIUS Profile ID",
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="fup_action",
        env_var="USAGE_FUP_ACTION",
        value_type=SettingValueType.string,
        default="throttle",
        allowed={"throttle", "suspend", "block", "none"},
        label="FUP Exhaustion Action",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="mikrotik_session_kill_enabled",
        env_var="NETWORK_MIKROTIK_SESSION_KILL_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable MikroTik Session Kill",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="address_list_block_enabled",
        env_var="NETWORK_ADDRESS_LIST_BLOCK_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable Address List Blocking",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_mikrotik_address_list",
        env_var="NETWORK_DEFAULT_MIKROTIK_ADDRESS_LIST",
        value_type=SettingValueType.string,
        default=None,
        label="Default MikroTik Address List",
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="default_dunning_case_status",
        env_var="COLLECTIONS_DEFAULT_DUNNING_CASE_STATUS",
        value_type=SettingValueType.string,
        default="open",
        allowed={"open", "paused", "resolved", "closed"},
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="throttle_radius_profile_id",
        env_var="COLLECTIONS_THROTTLE_RADIUS_PROFILE_ID",
        value_type=SettingValueType.string,
        default=None,
        label="Throttle RADIUS Profile ID",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="default_auth_provider",
        env_var="AUTH_DEFAULT_AUTH_PROVIDER",
        value_type=SettingValueType.string,
        default="local",
        allowed={"local", "sso", "radius"},
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="default_session_status",
        env_var="AUTH_DEFAULT_SESSION_STATUS",
        value_type=SettingValueType.string,
        default="active",
        allowed={"active", "revoked", "expired"},
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="default_service_order_status",
        env_var="PROVISIONING_DEFAULT_SERVICE_ORDER_STATUS",
        value_type=SettingValueType.string,
        default="draft",
        allowed={
            "draft",
            "submitted",
            "scheduled",
            "provisioning",
            "active",
            "canceled",
            "failed",
        },
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="default_appointment_status",
        env_var="PROVISIONING_DEFAULT_APPOINTMENT_STATUS",
        value_type=SettingValueType.string,
        default="proposed",
        allowed={"proposed", "confirmed", "completed", "no_show", "canceled"},
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="default_task_status",
        env_var="PROVISIONING_DEFAULT_TASK_STATUS",
        value_type=SettingValueType.string,
        default="pending",
        allowed={"pending", "in_progress", "blocked", "completed", "failed"},
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="default_vendor",
        env_var="PROVISIONING_DEFAULT_VENDOR",
        value_type=SettingValueType.string,
        default="other",
        allowed={"mikrotik", "huawei", "zte", "nokia", "genieacs", "other"},
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="default_workflow_id",
        env_var="PROVISIONING_DEFAULT_WORKFLOW_ID",
        value_type=SettingValueType.string,
        default=None,
        label="Default Provisioning Workflow ID",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_device_type",
        env_var="NETWORK_DEFAULT_DEVICE_TYPE",
        value_type=SettingValueType.string,
        default="ont",
        allowed={"ont", "router", "modem", "cpe"},
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_device_status",
        env_var="NETWORK_DEFAULT_DEVICE_STATUS",
        value_type=SettingValueType.string,
        default="active",
        allowed={"active", "inactive", "retired"},
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_port_type",
        env_var="NETWORK_DEFAULT_PORT_TYPE",
        value_type=SettingValueType.string,
        default="ethernet",
        allowed={"pon", "ethernet", "wifi", "mgmt"},
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_port_status",
        env_var="NETWORK_DEFAULT_PORT_STATUS",
        value_type=SettingValueType.string,
        default="down",
        allowed={"up", "down", "disabled"},
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_ip_version",
        env_var="NETWORK_DEFAULT_IP_VERSION",
        value_type=SettingValueType.string,
        default="ipv4",
        allowed={"ipv4", "ipv6"},
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_olt_port_type",
        env_var="NETWORK_DEFAULT_OLT_PORT_TYPE",
        value_type=SettingValueType.string,
        default="pon",
        allowed={"pon", "uplink", "ethernet", "mgmt"},
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_fiber_strand_status",
        env_var="NETWORK_DEFAULT_FIBER_STRAND_STATUS",
        value_type=SettingValueType.string,
        default="available",
        allowed={
            "available",
            "in_use",
            "reserved",
            "damaged",
            "retired",
        },
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_splitter_input_ports",
        env_var="NETWORK_DEFAULT_SPLITTER_INPUT_PORTS",
        value_type=SettingValueType.integer,
        default=1,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_splitter_output_ports",
        env_var="NETWORK_DEFAULT_SPLITTER_OUTPUT_PORTS",
        value_type=SettingValueType.integer,
        default=8,
        min_value=1,
    ),
    # Fiber installation planning cost rates
    SettingSpec(
        domain=SettingDomain.network,
        key="fiber_drop_cable_cost_per_meter",
        env_var="NETWORK_FIBER_DROP_CABLE_COST_PER_METER",
        value_type=SettingValueType.string,
        default="2.50",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="fiber_labor_cost_per_meter",
        env_var="NETWORK_FIBER_LABOR_COST_PER_METER",
        value_type=SettingValueType.string,
        default="1.50",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="fiber_ont_device_cost",
        env_var="NETWORK_FIBER_ONT_DEVICE_COST",
        value_type=SettingValueType.string,
        default="85.00",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="fiber_installation_base_fee",
        env_var="NETWORK_FIBER_INSTALLATION_BASE_FEE",
        value_type=SettingValueType.string,
        default="50.00",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="vendor_quote_approval_threshold",
        env_var="NETWORK_VENDOR_QUOTE_APPROVAL_THRESHOLD",
        value_type=SettingValueType.string,
        default="5000",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="vendor_quote_validity_days",
        env_var="NETWORK_VENDOR_QUOTE_VALIDITY_DAYS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="vendor_bid_minimum_days",
        env_var="NETWORK_VENDOR_BID_MINIMUM_DAYS",
        value_type=SettingValueType.integer,
        default=7,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="server_health_disk_warn_pct",
        label="Server Health Disk Warning (%)",
        env_var="SERVER_HEALTH_DISK_WARN_PCT",
        value_type=SettingValueType.integer,
        default=80,
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="server_health_disk_crit_pct",
        label="Server Health Disk Critical (%)",
        env_var="SERVER_HEALTH_DISK_CRIT_PCT",
        value_type=SettingValueType.integer,
        default=90,
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="server_health_mem_warn_pct",
        label="Server Health Memory Warning (%)",
        env_var="SERVER_HEALTH_MEM_WARN_PCT",
        value_type=SettingValueType.integer,
        default=80,
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="server_health_mem_crit_pct",
        label="Server Health Memory Critical (%)",
        env_var="SERVER_HEALTH_MEM_CRIT_PCT",
        value_type=SettingValueType.integer,
        default=90,
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="server_health_load_warn",
        label="Server Health Load Warning (per core)",
        env_var="SERVER_HEALTH_LOAD_WARN",
        value_type=SettingValueType.string,
        default="1.0",
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="server_health_load_crit",
        label="Server Health Load Critical (per core)",
        env_var="SERVER_HEALTH_LOAD_CRIT",
        value_type=SettingValueType.string,
        default="1.5",
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="network_health_warn_pct",
        label="Network Health Warning Threshold (%)",
        env_var="NETWORK_HEALTH_WARN_PCT",
        value_type=SettingValueType.integer,
        default=90,
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="network_health_crit_pct",
        label="Network Health Critical Threshold (%)",
        env_var="NETWORK_HEALTH_CRIT_PCT",
        value_type=SettingValueType.integer,
        default=70,
        min_value=1,
        max_value=100,
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
        default=60,
        min_value=5,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="user_invite_expiry_minutes",
        env_var="USER_INVITE_EXPIRY_MINUTES",
        value_type=SettingValueType.integer,
        default=60,
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
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_peer_stats_sync_enabled",
        env_var="WIREGUARD_PEER_STATS_SYNC_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="WireGuard Peer Stats Sync Enabled",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_peer_stats_sync_interval_seconds",
        env_var="WIREGUARD_PEER_STATS_SYNC_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=60,
        label="WireGuard Peer Stats Sync Interval (seconds)",
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
    # ============== Provisioning Domain: NAS/OAuth Tasks ==============
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="nas_backup_retention_interval_seconds",
        env_var="NAS_BACKUP_RETENTION_INTERVAL",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=3600,
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="oauth_token_refresh_interval_seconds",
        env_var="OAUTH_TOKEN_REFRESH_INTERVAL",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=3600,
    ),
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
}


def get_spec(domain: SettingDomain, key: str) -> SettingSpec | None:
    for spec in SETTINGS_SPECS:
        if spec.domain == domain and spec.key == key:
            return spec
    return None


def list_specs(domain: SettingDomain) -> list[SettingSpec]:
    return [spec for spec in SETTINGS_SPECS if spec.domain == domain]


def resolve_value(db, domain: SettingDomain, key: str) -> object | None:
    """Resolve a setting value with Redis caching.

    Checks Redis cache first, falls back to database query, then caches result.
    """
    spec = get_spec(domain, key)
    if not spec:
        return None

    # 1. Check cache first
    cached = SettingsCache.get(domain.value, key)
    if cached is not None:
        return cached

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
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = spec.default if isinstance(spec.default, int) else None
        if spec.min_value is not None and parsed is not None and parsed < spec.min_value:
            parsed = spec.default
        if spec.max_value is not None and parsed is not None and parsed > spec.max_value:
            parsed = spec.default
        value = parsed

    # 3. Cache the result (only non-None values)
    if value is not None:
        SettingsCache.set(domain.value, key, value)

    return value


def resolve_values_atomic(
    db, domain: SettingDomain, keys: list[str]
) -> dict[str, Any]:
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
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                parsed = spec.default if isinstance(spec.default, int) else None
            if spec.min_value is not None and parsed is not None and parsed < spec.min_value:
                parsed = spec.default
            if spec.max_value is not None and parsed is not None and parsed > spec.max_value:
                parsed = spec.default
            value = parsed

        if value is not None:
            cached[key] = value
            to_cache[key] = value

    # 3. Cache all retrieved values atomically
    if to_cache:
        SettingsCache.set_multi(domain.value, to_cache)

    return cached


def extract_db_value(setting) -> object | None:
    if not setting:
        return None
    if setting.value_text is not None:
        return setting.value_text
    if setting.value_json is not None:
        return setting.value_json
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


def normalize_for_db(spec: SettingSpec, value: object) -> tuple[str | None, object | None]:
    if spec.value_type == SettingValueType.boolean:
        bool_value = bool(value)
        return ("true" if bool_value else "false"), None
    if spec.value_type == SettingValueType.integer:
        return str(int(value)), None
    if spec.value_type == SettingValueType.string:
        return str(value), None
    return None, value
