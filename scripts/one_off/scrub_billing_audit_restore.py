"""Fail-closed scrub for an isolated billing-audit database restore.

This tool is deliberately narrower than a general anonymiser. It prepares a
restored Sub database for the billing alignment audit by destroying credentials,
login/delivery state and primary customer identity while preserving the billing,
legacy-mirror, entitlement and service-extension facts under audit.

Safety is structural:

* the database name must end in ``_audit``;
* ``BILLING_AUDIT_EPHEMERAL=1`` and ``--execute`` are both required;
* only PostgreSQL is accepted;
* a schema scan rejects every new secret-looking column, every unclassified
  column in a sensitive table, and every new or changed integration table;
* one declarative policy drives direct scrubs and their residual verification;
* scrub, verification and financial fingerprints share one transaction, so any
  failure rolls the whole operation back.

The script never prints source values. Run it only inside the isolated restore
workflow documented in ``docs/audits/BILLING_ALIGNMENT_RUN_2026-07-12.md``.
"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool


class ScrubSafetyError(RuntimeError):
    """The restore cannot be scrubbed without weakening a safety invariant."""


@dataclass(frozen=True)
class ColumnInfo:
    table_name: str
    column_name: str
    data_type: str
    nullable: bool

    @property
    def key(self) -> tuple[str, str]:
        return (self.table_name, self.column_name)


@dataclass(frozen=True)
class ScrubAction:
    """One reviewed mutation and the predicate that proves it completed."""

    expression: str
    residual_condition: str
    category: Literal["secret", "identity", "opaque", "capability"]


def _null_action(
    category: Literal["secret", "identity", "opaque", "capability"],
) -> ScrubAction:
    return ScrubAction("NULL", "{column} IS NOT NULL", category)


def _exact_action(
    expression: str,
    category: Literal["secret", "identity", "opaque", "capability"],
) -> ScrubAction:
    return ScrubAction(
        expression,
        f"{{column}} IS DISTINCT FROM ({expression})",
        category,
    )


def _nullable_exact_action(
    expression: str,
    category: Literal["secret", "identity", "opaque", "capability"],
) -> ScrubAction:
    return ScrubAction(
        f"CASE WHEN {{column}} IS NULL THEN NULL ELSE {expression} END",
        f"{{column}} IS NOT NULL AND {{column}} IS DISTINCT FROM ({expression})",
        category,
    )


SECRET_PATTERN = re.compile(
    r"(secret|password|passwd|token|api_key|apikey|auth_config|credential|"
    r"private_key|shared_secret|key_hash|code_hash|preshared_key)"
)

# Columns that describe secret handling but do not contain a secret. Foreign-key
# identifiers are handled by the ``_id`` rule in ``is_secret_metadata``.
SECRET_METADATA_COLUMNS = {
    "is_secret",
    "must_change_password",
    "token_type",
    "from_credential",
}
SECRET_METADATA_SUFFIXES = (
    "_at",
    "_set",
    "_mode",
    "_path",
    "_expires",
    "_expires_at",
    "_rotated_at",
)

# Complete for the credential surface observed in the retained 2026-07-12 Sub
# backup. A future secret-looking column is not guessed at: schema discovery
# rejects it and requires review here.
SECRET_ACTIONS: dict[tuple[str, str], ScrubAction] = {
    ("access_credentials", "secret_hash"): _null_action("secret"),
    ("bank_accounts", "token"): _null_action("secret"),
    ("campaign_recipients", "unsubscribe_token"): _null_action("secret"),
    ("connectivity_state_backups", "credentials"): _null_action("secret"),
    ("connector_configs", "auth_config"): _null_action("secret"),
    ("integration_hooks", "auth_config"): _null_action("secret"),
    ("jump_hosts", "ssh_password"): _null_action("secret"),
    ("mfa_methods", "secret"): _null_action("secret"),
    ("nas_devices", "shared_secret"): _null_action("secret"),
    ("nas_devices", "ssh_password"): _null_action("secret"),
    ("nas_devices", "api_password"): _null_action("secret"),
    ("nas_devices", "api_token"): _null_action("secret"),
    ("network_device_bandwidth_graphs", "public_token"): _null_action("secret"),
    ("network_devices", "snmp_auth_secret"): _null_action("secret"),
    ("network_devices", "snmp_priv_secret"): _null_action("secret"),
    ("olt_devices", "ssh_password"): _null_action("secret"),
    ("olt_devices", "api_password"): _null_action("secret"),
    ("olt_devices", "api_token"): _null_action("secret"),
    ("ont_assignments", "pppoe_password"): _null_action("secret"),
    ("ont_assignments", "wifi_password"): _null_action("secret"),
    ("ont_profile_wan_services", "pppoe_static_password"): _null_action("secret"),
    ("ont_provisioning_profiles", "cr_password"): _null_action("secret"),
    ("ont_wan_service_instances", "pppoe_password"): _null_action("secret"),
    ("payment_methods", "token"): _null_action("secret"),
    ("payment_providers", "webhook_secret_ref"): _null_action("secret"),
    ("radius_clients", "shared_secret_hash"): _nullable_exact_action(
        "'!scrubbed-' || id::text", "secret"
    ),
    ("radius_users", "secret_hash"): _null_action("secret"),
    ("routers", "rest_api_password"): _nullable_exact_action(
        "'!scrubbed-not-a-password'", "secret"
    ),
    ("snmp_credentials", "auth_secret_hash"): _null_action("secret"),
    ("snmp_credentials", "priv_secret_hash"): _null_action("secret"),
    ("system_users", "device_login_secret"): _null_action("secret"),
    ("tr069_acs_servers", "cwmp_password"): _null_action("secret"),
    ("tr069_acs_servers", "connection_request_password"): _null_action("secret"),
    ("user_credentials", "password_hash"): _nullable_exact_action(
        "'!scrubbed-not-a-valid-hash'", "secret"
    ),
    ("vas_transactions", "token_encrypted"): _null_action("secret"),
    ("webhook_endpoints", "secret"): _null_action("secret"),
    ("wireguard_peers", "private_key"): _null_action("secret"),
    ("wireguard_peers", "preshared_key"): _null_action("secret"),
    ("wireguard_peers", "provision_token_hash"): _null_action("secret"),
    ("wireguard_servers", "private_key"): _null_action("secret"),
}

# Primary identity and direct customer/device identifiers. These actions replace
# the previous hand-maintained UPDATE blocks. Each action carries the predicate
# used to prove the value was actually rewritten.
IDENTITY_ACTIONS: dict[tuple[str, str], ScrubAction] = {
    ("subscribers", "first_name"): _exact_action("'Customer'", "identity"),
    ("subscribers", "last_name"): _exact_action(
        "substr(md5(id::text), 1, 8)", "identity"
    ),
    ("subscribers", "display_name"): _exact_action(
        "'Customer ' || substr(md5(id::text), 1, 8)", "identity"
    ),
    ("subscribers", "avatar_url"): _null_action("identity"),
    ("subscribers", "company_name"): _null_action("identity"),
    ("subscribers", "legal_name"): _null_action("identity"),
    ("subscribers", "tax_id"): _null_action("identity"),
    ("subscribers", "domain"): _null_action("identity"),
    ("subscribers", "website"): _null_action("identity"),
    ("subscribers", "email"): _exact_action(
        "'cust+' || replace(id::text, '-', '') || '@example.invalid'", "identity"
    ),
    ("subscribers", "email_verified"): _exact_action("false", "identity"),
    ("subscribers", "phone"): _null_action("identity"),
    ("subscribers", "nin"): _null_action("identity"),
    ("subscribers", "date_of_birth"): _null_action("identity"),
    ("subscribers", "gender"): _exact_action("'unknown'", "identity"),
    ("subscribers", "preferred_contact_method"): _null_action("identity"),
    ("subscribers", "address_line1"): _null_action("identity"),
    ("subscribers", "address_line2"): _null_action("identity"),
    ("subscribers", "postal_code"): _null_action("identity"),
    ("subscribers", "subscriber_number"): _nullable_exact_action(
        "'SUB-' || id::text", "identity"
    ),
    ("subscribers", "account_number"): _nullable_exact_action(
        "'ACC-' || id::text", "identity"
    ),
    ("subscribers", "billing_name"): _exact_action(
        "'Customer ' || substr(md5(id::text), 1, 8)", "identity"
    ),
    ("subscribers", "billing_address_line1"): _null_action("identity"),
    ("subscribers", "billing_address_line2"): _null_action("identity"),
    ("subscribers", "billing_postal_code"): _null_action("identity"),
    ("subscribers", "notes"): _null_action("identity"),
    ("subscribers", "crm_subscriber_id"): _null_action("identity"),
    ("subscribers", "metadata"): _null_action("identity"),
    ("system_users", "first_name"): _exact_action("'Staff'", "identity"),
    ("system_users", "last_name"): _exact_action(
        "substr(md5(id::text), 1, 8)", "identity"
    ),
    ("system_users", "display_name"): _exact_action(
        "'Staff ' || substr(md5(id::text), 1, 8)", "identity"
    ),
    ("system_users", "email"): _exact_action(
        "'staff+' || replace(id::text, '-', '') || '@example.invalid'", "identity"
    ),
    ("system_users", "phone"): _null_action("identity"),
    ("system_users", "device_login_enabled"): _exact_action("false", "capability"),
    ("resellers", "name"): _exact_action(
        "'Reseller ' || substr(md5(id::text), 1, 8)", "identity"
    ),
    ("resellers", "code"): _nullable_exact_action(
        "'RES-' || replace(id::text, '-', '')", "identity"
    ),
    ("resellers", "contact_email"): _nullable_exact_action(
        "'reseller+' || substr(md5(id::text), 1, 12) || '@example.invalid'",
        "identity",
    ),
    ("resellers", "contact_phone"): _null_action("identity"),
    ("resellers", "notes"): _null_action("identity"),
    ("reseller_users", "email"): _nullable_exact_action(
        "'reseller-user+' || substr(md5(id::text), 1, 12) || '@example.invalid'",
        "identity",
    ),
    ("reseller_users", "full_name"): _exact_action(
        "'Reseller user ' || substr(md5(id::text), 1, 8)", "identity"
    ),
    ("organizations", "name"): _exact_action(
        "'Organization ' || substr(md5(id::text), 1, 8)", "identity"
    ),
    ("organizations", "legal_name"): _null_action("identity"),
    ("organizations", "tax_id"): _null_action("identity"),
    ("organizations", "domain"): _null_action("identity"),
    ("organizations", "website"): _null_action("identity"),
    ("organizations", "phone"): _null_action("identity"),
    ("organizations", "email"): _null_action("identity"),
    ("organizations", "address_line1"): _null_action("identity"),
    ("organizations", "address_line2"): _null_action("identity"),
    ("organizations", "postal_code"): _null_action("identity"),
    ("organizations", "erp_id"): _null_action("identity"),
    ("organizations", "erpnext_id"): _null_action("identity"),
    ("organizations", "notes"): _null_action("identity"),
    ("organizations", "tags"): _null_action("identity"),
    ("organizations", "metadata"): _null_action("identity"),
    ("addresses", "label"): _null_action("identity"),
    ("addresses", "address_line1"): _exact_action("'Redacted address'", "identity"),
    ("addresses", "address_line2"): _null_action("identity"),
    ("addresses", "postal_code"): _null_action("identity"),
    ("addresses", "latitude"): _null_action("identity"),
    ("addresses", "longitude"): _null_action("identity"),
    ("addresses", "geom"): _null_action("identity"),
    ("subscriber_channels", "address"): _exact_action(
        "'redacted-' || substr(md5(id::text), 1, 12)", "identity"
    ),
    ("subscriber_channels", "label"): _null_action("identity"),
    ("subscriber_channels", "is_verified"): _exact_action("false", "identity"),
    ("subscriber_channels", "verified_at"): _null_action("identity"),
    ("subscriber_channels", "metadata"): _null_action("identity"),
    ("subscriber_contacts", "full_name"): _exact_action(
        "'Contact ' || substr(md5(id::text), 1, 8)", "identity"
    ),
    ("subscriber_contacts", "phone"): _null_action("identity"),
    ("subscriber_contacts", "email"): _null_action("identity"),
    ("subscriber_contacts", "whatsapp"): _null_action("identity"),
    ("subscriber_contacts", "facebook"): _null_action("identity"),
    ("subscriber_contacts", "instagram"): _null_action("identity"),
    ("subscriber_contacts", "x_handle"): _null_action("identity"),
    ("subscriber_contacts", "telegram"): _null_action("identity"),
    ("subscriber_contacts", "linkedin"): _null_action("identity"),
    ("subscriber_contacts", "other_social"): _null_action("identity"),
    ("subscriber_contacts", "notes"): _null_action("identity"),
    ("subscriber_contacts", "receives_notifications"): _exact_action(
        "false", "capability"
    ),
    ("subscriber_nin_verifications", "nin"): _exact_action("'00000000000'", "identity"),
    ("subscriber_nin_verifications", "mono_response"): _null_action("opaque"),
    ("subscriber_nin_verifications", "failure_reason"): _null_action("identity"),
    ("mfa_methods", "label"): _null_action("identity"),
    ("mfa_methods", "phone"): _null_action("identity"),
    ("mfa_methods", "email"): _null_action("identity"),
    ("mfa_methods", "enabled"): _exact_action("false", "capability"),
    ("mfa_methods", "is_active"): _exact_action("false", "capability"),
    ("user_credentials", "username"): _nullable_exact_action(
        "'login-' || replace(id::text, '-', '')", "identity"
    ),
    ("user_credentials", "is_active"): _exact_action("false", "capability"),
    ("access_credentials", "username"): _exact_action(
        "'access-' || replace(id::text, '-', '')", "identity"
    ),
    ("access_credentials", "circuit_id"): _null_action("identity"),
    ("access_credentials", "remote_id"): _null_action("identity"),
    ("radius_users", "username"): _exact_action(
        "'radius-' || replace(id::text, '-', '')", "identity"
    ),
    ("subscriptions", "login"): _nullable_exact_action(
        "'service-' || replace(id::text, '-', '')", "identity"
    ),
    ("subscriptions", "ipv4_address"): _null_action("identity"),
    ("subscriptions", "ipv6_address"): _null_action("identity"),
    ("subscriptions", "last_seen_framed_ipv4"): _null_action("identity"),
    ("subscriptions", "last_seen_framed_ipv6"): _null_action("identity"),
    ("subscriptions", "mac_address"): _null_action("identity"),
    ("payment_methods", "label"): _exact_action("'Redacted'", "identity"),
    ("payment_methods", "last4"): _exact_action("'0000'", "identity"),
    ("payment_methods", "brand"): _exact_action("'redacted'", "identity"),
    ("payment_methods", "expires_month"): _null_action("identity"),
    ("payment_methods", "expires_year"): _null_action("identity"),
    ("bank_accounts", "account_last4"): _exact_action("'0000'", "identity"),
    ("bank_accounts", "routing_last4"): _exact_action("'0000'", "identity"),
}

# Opaque collaboration payloads are not billing facts. Keeping their rows and
# operational timestamps is useful for provenance, but no restored database
# needs endpoints, free text, request/response bodies or executable config.
INTEGRATION_ACTIONS: dict[tuple[str, str], ScrubAction] = {
    ("connector_configs", "name"): _exact_action(
        "'Connector ' || substr(md5(id::text), 1, 8)", "opaque"
    ),
    ("connector_configs", "base_url"): _null_action("opaque"),
    ("connector_configs", "headers"): _null_action("opaque"),
    ("connector_configs", "retry_policy"): _null_action("opaque"),
    ("connector_configs", "metadata"): _null_action("opaque"),
    ("connector_configs", "notes"): _null_action("opaque"),
    ("connector_configs", "is_active"): _exact_action("false", "capability"),
    ("integration_connectors", "name"): _exact_action(
        "'Integration connector ' || substr(md5(id::text), 1, 8)", "opaque"
    ),
    ("integration_connectors", "configuration"): _null_action("opaque"),
    ("integration_connectors", "status"): _exact_action("'disabled'", "capability"),
    ("integration_hooks", "title"): _exact_action(
        "'Integration hook ' || substr(md5(id::text), 1, 8)", "opaque"
    ),
    ("integration_hooks", "command"): _null_action("opaque"),
    ("integration_hooks", "url"): _null_action("opaque"),
    ("integration_hooks", "event_filters"): _null_action("opaque"),
    ("integration_hooks", "notes"): _null_action("opaque"),
    ("integration_hooks", "is_enabled"): _exact_action("false", "capability"),
    ("integration_jobs", "name"): _exact_action(
        "'Integration job ' || substr(md5(id::text), 1, 8)", "opaque"
    ),
    ("integration_jobs", "mapping_config"): _null_action("opaque"),
    ("integration_jobs", "filter_config"): _null_action("opaque"),
    ("integration_jobs", "notes"): _null_action("opaque"),
    ("integration_jobs", "is_active"): _exact_action("false", "capability"),
    ("integration_records", "local_id"): _null_action("opaque"),
    ("integration_records", "remote_id"): _null_action("opaque"),
    ("integration_records", "remote_number"): _null_action("opaque"),
    ("integration_records", "reason"): _null_action("opaque"),
    ("integration_records", "payload_snapshot"): _null_action("opaque"),
    ("integration_runs", "requested_by"): _null_action("identity"),
    ("integration_runs", "error"): _null_action("opaque"),
    ("integration_runs", "metrics"): _null_action("opaque"),
    ("integration_targets", "name"): _exact_action(
        "'Integration target ' || substr(md5(id::text), 1, 8)", "opaque"
    ),
    ("integration_targets", "notes"): _null_action("opaque"),
    ("integration_targets", "is_active"): _exact_action("false", "capability"),
    ("payment_providers", "name"): _exact_action(
        "'Payment provider ' || substr(md5(id::text), 1, 8)", "opaque"
    ),
    ("payment_providers", "notes"): _null_action("opaque"),
    ("payment_providers", "is_active"): _exact_action("false", "capability"),
    ("payment_webhook_dead_letters", "external_id"): _null_action("opaque"),
    ("payment_webhook_dead_letters", "idempotency_key"): _nullable_exact_action(
        "'dead-letter-' || replace(id::text, '-', '')", "opaque"
    ),
    ("payment_webhook_dead_letters", "payload"): _null_action("opaque"),
    ("payment_webhook_dead_letters", "error"): _null_action("opaque"),
    ("webhook_endpoints", "name"): _exact_action(
        "'Webhook endpoint ' || substr(md5(id::text), 1, 8)", "opaque"
    ),
    ("webhook_endpoints", "url"): _exact_action(
        "'https://example.invalid/scrubbed'", "opaque"
    ),
    ("webhook_endpoints", "is_active"): _exact_action("false", "capability"),
    ("webhook_subscriptions", "is_active"): _exact_action("false", "capability"),
}

SCRUB_ACTIONS: dict[tuple[str, str], ScrubAction] = {
    **SECRET_ACTIONS,
    **IDENTITY_ACTIONS,
    **INTEGRATION_ACTIONS,
}

# Rows in these tables are delivery/authentication capability, not audit facts.
# Deleting the whole table also handles every secret-looking column it contains.
DELETE_TABLES = (
    "crm_webhook_deliveries",
    "integration_hook_executions",
    "webhook_deliveries",
    "notification_deliveries",
    "notification_queue",
    "sessions",
    "oauth_tokens",
    "device_tokens",
    "field_vendor_device_tokens",
    "ticket_access_tokens",
    "mfa_recovery_codes",
    "api_keys",
)

INTEGRATION_TABLE_PATTERN = re.compile(r"(^|_)(integration|connector|webhook)(_|$)")

# Conditional value scrubs have row-dependent semantics and are verified by
# ``_verification_residuals`` alongside the direct column actions.
TYPED_VALUE_SCRUB_FILTERS: dict[str, str] = {
    "domain_settings": "is_secret IS TRUE",
    "subscription_engine_settings": "is_secret IS TRUE",
    "subscriber_custom_fields": "TRUE",
}
CONDITIONAL_SCRUB_COLUMNS: dict[str, frozenset[str]] = {
    table_name: frozenset({"value_text", "value_json"})
    for table_name in TYPED_VALUE_SCRUB_FILTERS
}

# Every non-scrubbed column in a sensitive table is named here deliberately.
# A column absent from both this map and SCRUB_ACTIONS is rejected before any
# UPDATE. This is the PII/config schema-drift boundary; it does not guess from
# names. Tables matching INTEGRATION_TABLE_PATTERN must also be present here or
# be deleted wholesale above.
SENSITIVE_TABLE_PRESERVED_COLUMNS: dict[str, frozenset[str]] = {
    "subscribers": frozenset(
        {
            "id",
            "locale",
            "timezone",
            "city",
            "region",
            "country_code",
            "pop_site_id",
            "account_start_date",
            "status",
            "lifecycle_override_status",
            "lifecycle_override_reason",
            "lifecycle_override_source",
            "lifecycle_override_at",
            "user_type",
            "is_active",
            "marketing_opt_in",
            "reseller_id",
            "tax_rate_id",
            "policy_set_id",
            "billing_enabled",
            "captive_redirect_enabled",
            "billing_city",
            "billing_region",
            "billing_country_code",
            "payment_method",
            "deposit",
            "billing_mode",
            "billing_day",
            "payment_due_days",
            "grace_period_days",
            "min_balance",
            "prepaid_low_balance_at",
            "prepaid_deactivation_at",
            "mrr_total",
            "category",
            "splynx_customer_id",
            "party_status",
            "organization_id",
            "sales_order_id",
            "created_at",
            "updated_at",
        }
    ),
    "system_users": frozenset(
        {
            "id",
            "user_type",
            "is_active",
            "device_login_secret_set_at",
            "device_login_revoked_at",
            "created_at",
            "updated_at",
        }
    ),
    "resellers": frozenset(
        {
            "id",
            "policy_set_id",
            "is_active",
            "is_house",
            "restrict_to_assigned_offers",
            "created_at",
            "updated_at",
        }
    ),
    "reseller_users": frozenset(
        {
            "id",
            "person_id",
            "reseller_id",
            "is_active",
            "last_login_at",
            "created_at",
            "updated_at",
        }
    ),
    "organizations": frozenset(
        {
            "id",
            "account_type",
            "account_status",
            "parent_id",
            "primary_contact_id",
            "owner_id",
            "industry",
            "employee_count",
            "annual_revenue",
            "source",
            "city",
            "region",
            "country_code",
            "commission_rate",
            "is_active",
            "created_at",
            "updated_at",
        }
    ),
    "addresses": frozenset(
        {
            "id",
            "subscriber_id",
            "tax_rate_id",
            "address_type",
            "city",
            "region",
            "country_code",
            "is_primary",
            "created_at",
            "updated_at",
        }
    ),
    "subscriber_channels": frozenset(
        {
            "id",
            "subscriber_id",
            "channel_type",
            "is_primary",
            "created_at",
            "updated_at",
        }
    ),
    "subscriber_contacts": frozenset(
        {
            "id",
            "subscriber_id",
            "relationship",
            "contact_type",
            "is_billing_contact",
            "is_authorized",
            "created_at",
            "updated_at",
        }
    ),
    "subscriber_nin_verifications": frozenset(
        {
            "id",
            "subscriber_id",
            "status",
            "is_match",
            "match_score",
            "verified_at",
            "created_at",
        }
    ),
    "subscriber_custom_fields": frozenset(
        {
            "id",
            "subscriber_id",
            "key",
            "value_type",
            "is_active",
            "created_at",
            "updated_at",
        }
    ),
    "mfa_methods": frozenset(
        {
            "id",
            "subscriber_id",
            "system_user_id",
            "reseller_user_id",
            "method_type",
            "is_primary",
            "verified_at",
            "last_used_at",
            "failed_attempts",
            "locked_until",
            "created_at",
            "updated_at",
        }
    ),
    "user_credentials": frozenset(
        {
            "id",
            "subscriber_id",
            "system_user_id",
            "reseller_user_id",
            "provider",
            "radius_server_id",
            "must_change_password",
            "password_updated_at",
            "failed_login_attempts",
            "locked_until",
            "last_login_at",
            "created_at",
            "updated_at",
        }
    ),
    "access_credentials": frozenset(
        {
            "id",
            "subscriber_id",
            "subscription_id",
            "is_active",
            "last_auth_at",
            "radius_profile_id",
            "pre_throttle_radius_profile_id",
            "connection_type",
            "created_at",
            "updated_at",
        }
    ),
    "radius_users": frozenset(
        {
            "id",
            "subscriber_id",
            "subscription_id",
            "access_credential_id",
            "radius_profile_id",
            "is_active",
            "last_sync_at",
            "created_at",
            "updated_at",
        }
    ),
    "subscriptions": frozenset(
        {
            "id",
            "subscriber_id",
            "offer_id",
            "offer_version_id",
            "service_address_id",
            "bundle_id",
            "provisioning_nas_device_id",
            "radius_profile_id",
            "status",
            "access_state",
            "billing_mode",
            "contract_term",
            "start_at",
            "end_at",
            "next_billing_at",
            "canceled_at",
            "cancel_reason",
            "splynx_service_id",
            "router_id",
            "service_description",
            "quantity",
            "unit",
            "unit_price",
            "discount",
            "discount_value",
            "discount_type",
            "discount_start_at",
            "discount_end_at",
            "discount_description",
            "service_status_raw",
            "created_at",
            "updated_at",
        }
    ),
    "payment_methods": frozenset(
        {
            "id",
            "account_id",
            "reseller_id",
            "payment_channel_id",
            "method_type",
            "is_default",
            "is_active",
            "created_at",
            "updated_at",
        }
    ),
    "bank_accounts": frozenset(
        {
            "id",
            "account_id",
            "payment_method_id",
            "bank_name",
            "account_type",
            "is_default",
            "is_active",
            "created_at",
            "updated_at",
        }
    ),
    "connector_configs": frozenset(
        {
            "id",
            "connector_type",
            "auth_type",
            "timeout_sec",
            "created_at",
            "updated_at",
        }
    ),
    "integration_connectors": frozenset(
        {
            "id",
            "version",
            "connector_type",
            "last_sync_at",
            "created_at",
            "updated_at",
        }
    ),
    "integration_hooks": frozenset(
        {
            "id",
            "hook_type",
            "http_method",
            "auth_type",
            "retry_max",
            "retry_backoff_ms",
            "timeout_seconds",
            "last_triggered_at",
            "created_at",
            "updated_at",
        }
    ),
    "integration_jobs": frozenset(
        {
            "id",
            "target_id",
            "job_type",
            "schedule_type",
            "interval_minutes",
            "interval_seconds",
            "adapter_key",
            "action",
            "entity_type",
            "direction",
            "trigger_mode",
            "conflict_policy",
            "last_run_at",
            "created_at",
            "updated_at",
        }
    ),
    "integration_records": frozenset(
        {
            "id",
            "run_id",
            "entity_type",
            "direction",
            "action",
            "status",
            "created_at",
        }
    ),
    "integration_runs": frozenset(
        {
            "id",
            "job_id",
            "status",
            "started_at",
            "finished_at",
            "trigger",
            "created_at",
        }
    ),
    "integration_targets": frozenset(
        {
            "id",
            "target_type",
            "connector_config_id",
            "created_at",
            "updated_at",
        }
    ),
    "payment_providers": frozenset(
        {
            "id",
            "provider_type",
            "connector_config_id",
            "created_at",
            "updated_at",
        }
    ),
    "payment_webhook_dead_letters": frozenset(
        {
            "id",
            "provider_type",
            "event_type",
            "status",
            "retry_count",
            "received_at",
            "last_attempt_at",
        }
    ),
    "webhook_endpoints": frozenset(
        {
            "id",
            "connector_config_id",
            "delivery_timeout_seconds",
            "max_retries",
            "retry_backoff_seconds",
            "created_at",
            "updated_at",
        }
    ),
    "webhook_subscriptions": frozenset(
        {"id", "endpoint_id", "event_type", "created_at", "updated_at"}
    ),
}

REQUIRED_AUDIT_TABLES = {
    "subscribers",
    "billing_accounts",
    "subscriptions",
    "ledger_entries",
    "invoices",
    "invoice_lines",
    "payments",
    "payment_allocations",
    "credit_notes",
    "service_entitlements",
    "service_extensions",
    "service_extension_entries",
    "splynx_billing_transactions",
}

# Row counts are always fingerprinted. Named numeric columns are summed when
# present in the restored schema. This includes service-period and extension
# facts as well as the conventional money documents.
FINANCIAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "subscribers": ("deposit", "min_balance", "mrr_total"),
    "billing_accounts": ("balance",),
    "subscriptions": ("unit_price", "discount_value"),
    "ledger_entries": ("amount",),
    "invoices": ("subtotal", "tax_total", "total", "balance_due"),
    "invoice_lines": ("quantity", "unit_price", "amount"),
    "payments": ("amount", "refunded_amount", "provider_fee"),
    "payment_allocations": ("amount",),
    "credit_notes": ("subtotal", "tax_total", "total", "applied_total"),
    "credit_note_lines": ("quantity", "unit_price", "amount"),
    "credit_note_applications": ("amount",),
    "service_entitlements": ("amount_funded",),
    "service_extensions": ("days", "affected_count", "skipped_count"),
    "service_extension_entries": (),
    "enforcement_locks": (),
    "splynx_billing_transactions": ("amount",),
    "payment_proofs": (
        "amount",
        "gross_amount",
        "verified_amount",
        "wht_amount",
    ),
}


def is_secret_metadata(column_name: str) -> bool:
    return (
        column_name in SECRET_METADATA_COLUMNS
        or column_name.endswith(("tokens_in", "tokens_out"))
        or column_name.endswith("_id")
        or column_name.endswith(SECRET_METADATA_SUFFIXES)
    )


def unknown_secret_columns(columns: Sequence[ColumnInfo]) -> list[ColumnInfo]:
    deleted = set(DELETE_TABLES)
    return sorted(
        (
            column
            for column in columns
            if SECRET_PATTERN.search(column.column_name)
            and not is_secret_metadata(column.column_name)
            and column.table_name not in deleted
            and column.key not in SCRUB_ACTIONS
        ),
        key=lambda item: item.key,
    )


def unknown_sensitive_columns(columns: Sequence[ColumnInfo]) -> list[ColumnInfo]:
    """Reject unreviewed PII/config columns before the scrub writes anything."""
    deleted = set(DELETE_TABLES)
    unknown: list[ColumnInfo] = []
    for column in columns:
        if column.table_name in deleted:
            continue
        preserved = SENSITIVE_TABLE_PRESERVED_COLUMNS.get(column.table_name)
        if preserved is None:
            if INTEGRATION_TABLE_PATTERN.search(column.table_name):
                unknown.append(column)
            continue
        conditional = CONDITIONAL_SCRUB_COLUMNS.get(column.table_name, frozenset())
        if (
            column.column_name not in preserved
            and column.key not in SCRUB_ACTIONS
            and column.column_name not in conditional
        ):
            unknown.append(column)
    return sorted(unknown, key=lambda item: item.key)


def incompatible_secret_actions(columns: Sequence[ColumnInfo]) -> list[ColumnInfo]:
    """Return known columns whose configured action violates nullability."""
    return sorted(
        (
            column
            for column in columns
            if column.key in SECRET_ACTIONS
            and SECRET_ACTIONS[column.key].expression == "NULL"
            and not column.nullable
        ),
        key=lambda item: item.key,
    )


def incompatible_scrub_actions(columns: Sequence[ColumnInfo]) -> list[ColumnInfo]:
    """Return any configured NULL scrub that the restored schema rejects."""
    return sorted(
        (
            column
            for column in columns
            if column.key in SCRUB_ACTIONS
            and SCRUB_ACTIONS[column.key].expression == "NULL"
            and not column.nullable
        ),
        key=lambda item: item.key,
    )


def validate_target(
    *, database_name: str, dialect_name: str, execute: bool, ephemeral_flag: str | None
) -> None:
    if not execute:
        raise ScrubSafetyError("refusing to write without --execute")
    if ephemeral_flag != "1":
        raise ScrubSafetyError("BILLING_AUDIT_EPHEMERAL must equal 1")
    if dialect_name != "postgresql":
        raise ScrubSafetyError("only PostgreSQL audit restores are supported")
    if not database_name.lower().endswith("_audit"):
        raise ScrubSafetyError("target database name must end in _audit")


def typed_secret_values(value_type: str) -> tuple[str | None, dict[str, Any] | None]:
    if value_type == "json":
        return None, {}
    return "!scrubbed", None


def assert_fingerprints_equal(
    before: Mapping[str, tuple[Any, ...]], after: Mapping[str, tuple[Any, ...]]
) -> None:
    if before != after:
        changed = sorted(set(before) | set(after))
        changed = [key for key in changed if before.get(key) != after.get(key)]
        raise ScrubSafetyError(
            "financial/service fingerprint changed: " + ", ".join(changed)
        )


def assert_no_residuals(residuals: Mapping[str, int]) -> None:
    failed = sorted(name for name, count in residuals.items() if count != 0)
    if failed:
        raise ScrubSafetyError("post-scrub verification failed: " + ", ".join(failed))


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _schema_columns(connection: Connection) -> list[ColumnInfo]:
    rows = connection.execute(
        text(
            """
            SELECT c.table_name, c.column_name, c.data_type,
                   c.is_nullable = 'YES' AS nullable
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema
             AND t.table_name = c.table_name
            WHERE c.table_schema = 'public'
              AND t.table_type = 'BASE TABLE'
            ORDER BY c.table_name, c.ordinal_position
            """
        )
    ).mappings()
    return [
        ColumnInfo(
            table_name=str(row["table_name"]),
            column_name=str(row["column_name"]),
            data_type=str(row["data_type"]),
            nullable=bool(row["nullable"]),
        )
        for row in rows
    ]


def _column_map(columns: Sequence[ColumnInfo]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for column in columns:
        result.setdefault(column.table_name, set()).add(column.column_name)
    return result


def _financial_fingerprint(
    connection: Connection, columns_by_table: Mapping[str, set[str]]
) -> dict[str, tuple[Any, ...]]:
    fingerprint: dict[str, tuple[Any, ...]] = {}
    for table_name, candidates in FINANCIAL_COLUMNS.items():
        actual_columns = columns_by_table.get(table_name)
        if actual_columns is None:
            continue
        summed = [column for column in candidates if column in actual_columns]
        expressions = ["count(*)::text"]
        expressions.extend(
            f"coalesce(sum({_quote(connection, column)}), 0)::text" for column in summed
        )
        table = _quote(connection, table_name)
        fingerprint[table_name] = tuple(
            connection.execute(
                text(f"SELECT {', '.join(expressions)} FROM {table}")
            ).one()
        )
    return fingerprint


def _apply_scrub_actions(
    connection: Connection,
    columns_by_table: Mapping[str, set[str]],
) -> None:
    by_table: dict[str, list[tuple[str, ScrubAction]]] = {}
    for (table_name, column_name), action in SCRUB_ACTIONS.items():
        if column_name in columns_by_table.get(table_name, set()):
            by_table.setdefault(table_name, []).append((column_name, action))

    for table_name, actions in sorted(by_table.items()):
        assignments = []
        for column_name, action in sorted(actions):
            column = _quote(connection, column_name)
            expression = action.expression.format(column=column)
            assignments.append(f"{column} = {expression}")
        connection.execute(
            text(
                f"UPDATE {_quote(connection, table_name)} SET {', '.join(assignments)}"
            )
        )


def _delete_capability_rows(
    connection: Connection, columns_by_table: Mapping[str, set[str]]
) -> None:
    for table_name in DELETE_TABLES:
        if table_name in columns_by_table:
            connection.execute(text(f"DELETE FROM {_quote(connection, table_name)}"))


def _scrub_typed_settings(
    connection: Connection, columns_by_table: Mapping[str, set[str]]
) -> None:
    for table_name, row_filter in TYPED_VALUE_SCRUB_FILTERS.items():
        required = {"value_type", "value_text", "value_json"}
        if "is_secret" in row_filter:
            required.add("is_secret")
        if not required.issubset(columns_by_table.get(table_name, set())):
            continue
        table = _quote(connection, table_name)
        connection.execute(
            text(
                f"""
                UPDATE {table}
                   SET value_text = CASE WHEN value_type::text = 'json'
                                         THEN NULL ELSE '!scrubbed' END,
                       value_json = CASE WHEN value_type::text = 'json'
                                         THEN '{{}}'::jsonb ELSE NULL END
                 WHERE {row_filter}
                """
            )
        )

    # An audit restore must have no outbound control plane. Billing policy is
    # intentionally retained because the audit resolves funding thresholds.
    domain_columns = columns_by_table.get("domain_settings", set())
    if {"domain", "is_active"}.issubset(domain_columns):
        connection.execute(
            text(
                """
                UPDATE domain_settings
                   SET is_active = false
                 WHERE domain::text IN
                       ('notification', 'comms', 'integration', 'scheduler', 'vas')
                """
            )
        )


def _verification_residuals(
    connection: Connection, columns_by_table: Mapping[str, set[str]]
) -> dict[str, int]:
    residuals: dict[str, int] = {}
    for (table_name, column_name), action in SCRUB_ACTIONS.items():
        if column_name not in columns_by_table.get(table_name, set()):
            continue
        table = _quote(connection, table_name)
        column = _quote(connection, column_name)
        condition = action.residual_condition.format(column=column)
        residuals[f"{table_name}.{column_name}"] = int(
            connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE {condition}")
            ).scalar_one()
        )

    for table_name in DELETE_TABLES:
        if table_name in columns_by_table:
            residuals[f"{table_name}.rows"] = int(
                connection.execute(
                    text(f"SELECT count(*) FROM {_quote(connection, table_name)}")
                ).scalar_one()
            )

    for table_name, row_filter in TYPED_VALUE_SCRUB_FILTERS.items():
        required = {"value_type", "value_text", "value_json"}
        if "is_secret" in row_filter:
            required.add("is_secret")
        if not required.issubset(columns_by_table.get(table_name, set())):
            continue
        table = _quote(connection, table_name)
        residuals[f"{table_name}.typed_values"] = int(
            connection.execute(
                text(
                    f"""
                    SELECT count(*) FROM {table}
                     WHERE {row_filter}
                       AND NOT (
                           (value_type::text = 'json'
                            AND value_text IS NULL
                            AND value_json::jsonb = '{{}}'::jsonb)
                        OR (value_type::text <> 'json'
                            AND value_text = '!scrubbed'
                            AND value_json IS NULL)
                       )
                    """
                )
            ).scalar_one()
        )

    domain_columns = columns_by_table.get("domain_settings", set())
    if {"domain", "is_active"}.issubset(domain_columns):
        residuals["domain_settings.outbound_control"] = int(
            connection.execute(
                text(
                    """
                    SELECT count(*) FROM domain_settings
                     WHERE domain::text IN
                           ('notification', 'comms', 'integration', 'scheduler', 'vas')
                       AND is_active IS TRUE
                    """
                )
            ).scalar_one()
        )
    return residuals


def scrub_restore(
    connection: Connection,
    *,
    execute: bool,
    ephemeral_flag: str | None,
    statement_timeout_ms: int,
    lock_timeout_ms: int,
) -> dict[str, int]:
    database_name = str(
        connection.execute(text("SELECT current_database() ")).scalar_one()
    )
    validate_target(
        database_name=database_name,
        dialect_name=connection.dialect.name,
        execute=execute,
        ephemeral_flag=ephemeral_flag,
    )
    connection.execute(text(f"SET LOCAL statement_timeout = {statement_timeout_ms}"))
    connection.execute(text(f"SET LOCAL lock_timeout = {lock_timeout_ms}"))
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('billing-audit-restore-scrub'))")
    )

    columns = _schema_columns(connection)
    columns_by_table = _column_map(columns)
    missing = sorted(REQUIRED_AUDIT_TABLES - set(columns_by_table))
    if missing:
        raise ScrubSafetyError(
            "required audit tables are missing: " + ", ".join(missing)
        )
    unknown = unknown_secret_columns(columns)
    if unknown:
        names = ", ".join(
            f"{column.table_name}.{column.column_name}" for column in unknown
        )
        raise ScrubSafetyError("unclassified secret-looking columns: " + names)
    unknown_sensitive = unknown_sensitive_columns(columns)
    if unknown_sensitive:
        names = ", ".join(
            f"{column.table_name}.{column.column_name}" for column in unknown_sensitive
        )
        raise ScrubSafetyError("unclassified sensitive columns: " + names)
    incompatible = incompatible_scrub_actions(columns)
    if incompatible:
        names = ", ".join(
            f"{column.table_name}.{column.column_name}" for column in incompatible
        )
        raise ScrubSafetyError(
            "scrub columns require a reviewed non-null action: " + names
        )

    before = _financial_fingerprint(connection, columns_by_table)
    _delete_capability_rows(connection, columns_by_table)
    _apply_scrub_actions(connection, columns_by_table)
    _scrub_typed_settings(connection, columns_by_table)
    residuals = _verification_residuals(connection, columns_by_table)
    assert_no_residuals(residuals)
    after = _financial_fingerprint(connection, columns_by_table)
    assert_fingerprints_equal(before, after)
    return residuals


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="Required: scrub and commit the audit DB"
    )
    parser.add_argument("--statement-timeout-ms", type=int, default=900_000)
    parser.add_argument("--lock-timeout-ms", type=int, default=10_000)
    return parser


def _engine() -> Engine:
    from app.config import settings

    return create_engine(settings.database_url, poolclass=NullPool, pool_pre_ping=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ephemeral_flag = os.environ.get("BILLING_AUDIT_EPHEMERAL")
    # Refuse before opening a connection when the two operator gates are absent.
    if not args.execute:
        raise ScrubSafetyError("refusing to connect without --execute")
    if ephemeral_flag != "1":
        raise ScrubSafetyError("BILLING_AUDIT_EPHEMERAL must equal 1")
    if args.statement_timeout_ms <= 0 or args.lock_timeout_ms <= 0:
        raise ScrubSafetyError("timeouts must be positive")

    engine = _engine()
    try:
        with engine.begin() as connection:
            residuals = scrub_restore(
                connection,
                execute=args.execute,
                ephemeral_flag=ephemeral_flag,
                statement_timeout_ms=args.statement_timeout_ms,
                lock_timeout_ms=args.lock_timeout_ms,
            )
    finally:
        engine.dispose()
    print(
        "audit restore scrub committed; "
        f"{len(residuals)} credential/identity checks passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
