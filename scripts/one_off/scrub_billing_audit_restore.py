"""Fail-closed scrub for an isolated billing-audit database restore.

This tool is deliberately narrower than a general anonymiser. It prepares a
restored Sub database for the billing alignment audit by destroying credentials,
login/delivery state and primary customer identity while preserving the billing,
legacy-mirror, entitlement and service-extension facts under audit.

Safety is structural:

* the database name must end in ``_audit``;
* ``BILLING_AUDIT_EPHEMERAL=1`` and ``--execute`` are both required;
* only PostgreSQL is accepted;
* a schema scan rejects every new secret-looking column until it is explicitly
  classified here;
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
class SecretAction:
    kind: Literal["null", "marker", "unique_marker"]
    marker: str | None = None


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
    "tokens_in",
    "tokens_out",
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
SECRET_ACTIONS: dict[tuple[str, str], SecretAction] = {
    ("access_credentials", "secret_hash"): SecretAction("null"),
    ("bank_accounts", "token"): SecretAction("null"),
    ("campaign_recipients", "unsubscribe_token"): SecretAction("null"),
    ("connectivity_state_backups", "credentials"): SecretAction("null"),
    ("connector_configs", "auth_config"): SecretAction("null"),
    ("integration_hooks", "auth_config"): SecretAction("null"),
    ("jump_hosts", "ssh_password"): SecretAction("null"),
    ("mfa_methods", "secret"): SecretAction("null"),
    ("nas_devices", "shared_secret"): SecretAction("null"),
    ("nas_devices", "ssh_password"): SecretAction("null"),
    ("nas_devices", "api_password"): SecretAction("null"),
    ("nas_devices", "api_token"): SecretAction("null"),
    ("network_device_bandwidth_graphs", "public_token"): SecretAction("null"),
    ("network_devices", "snmp_auth_secret"): SecretAction("null"),
    ("network_devices", "snmp_priv_secret"): SecretAction("null"),
    ("olt_devices", "ssh_password"): SecretAction("null"),
    ("olt_devices", "api_password"): SecretAction("null"),
    ("olt_devices", "api_token"): SecretAction("null"),
    ("ont_assignments", "pppoe_password"): SecretAction("null"),
    ("ont_assignments", "wifi_password"): SecretAction("null"),
    ("ont_profile_wan_services", "pppoe_static_password"): SecretAction("null"),
    ("ont_provisioning_profiles", "cr_password"): SecretAction("null"),
    ("ont_wan_service_instances", "pppoe_password"): SecretAction("null"),
    ("payment_methods", "token"): SecretAction("null"),
    ("payment_providers", "webhook_secret_ref"): SecretAction("null"),
    ("radius_clients", "shared_secret_hash"): SecretAction("unique_marker"),
    ("radius_users", "secret_hash"): SecretAction("null"),
    ("routers", "rest_api_password"): SecretAction(
        "marker", "!scrubbed-not-a-password"
    ),
    ("snmp_credentials", "auth_secret_hash"): SecretAction("null"),
    ("snmp_credentials", "priv_secret_hash"): SecretAction("null"),
    ("system_users", "device_login_secret"): SecretAction("null"),
    ("tr069_acs_servers", "cwmp_password"): SecretAction("null"),
    ("tr069_acs_servers", "connection_request_password"): SecretAction("null"),
    ("user_credentials", "password_hash"): SecretAction(
        "marker", "!scrubbed-not-a-valid-hash"
    ),
    ("vas_transactions", "token_encrypted"): SecretAction("null"),
    ("webhook_endpoints", "secret"): SecretAction("null"),
    ("wireguard_peers", "private_key"): SecretAction("null"),
    ("wireguard_peers", "preshared_key"): SecretAction("null"),
    ("wireguard_peers", "provision_token_hash"): SecretAction("null"),
    ("wireguard_servers", "private_key"): SecretAction("null"),
}

# Rows in these tables are delivery/authentication capability, not audit facts.
# Deleting the whole table also handles every secret-looking column it contains.
DELETE_TABLES = (
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
            and column.key not in SECRET_ACTIONS
        ),
        key=lambda item: item.key,
    )


def incompatible_secret_actions(columns: Sequence[ColumnInfo]) -> list[ColumnInfo]:
    """Return known columns whose configured action violates nullability."""
    return sorted(
        (
            column
            for column in columns
            if column.key in SECRET_ACTIONS
            and SECRET_ACTIONS[column.key].kind == "null"
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


def _apply_secret_actions(
    connection: Connection,
    columns_by_table: Mapping[str, set[str]],
) -> None:
    for (table_name, column_name), action in SECRET_ACTIONS.items():
        if column_name not in columns_by_table.get(table_name, set()):
            continue
        table = _quote(connection, table_name)
        column = _quote(connection, column_name)
        if action.kind == "null":
            connection.execute(
                text(f"UPDATE {table} SET {column} = NULL WHERE {column} IS NOT NULL")
            )
        elif action.kind == "unique_marker":
            connection.execute(
                text(
                    f"UPDATE {table} SET {column} = "
                    f"'!scrubbed-' || id::text WHERE {column} IS NOT NULL"
                )
            )
        else:
            connection.execute(
                text(
                    f"UPDATE {table} SET {column} = :marker WHERE {column} IS NOT NULL"
                ),
                {"marker": action.marker},
            )


def _delete_capability_rows(
    connection: Connection, columns_by_table: Mapping[str, set[str]]
) -> None:
    for table_name in DELETE_TABLES:
        if table_name in columns_by_table:
            connection.execute(text(f"DELETE FROM {_quote(connection, table_name)}"))


def _update_existing(
    connection: Connection,
    columns_by_table: Mapping[str, set[str]],
    table_name: str,
    assignments: Mapping[str, str],
) -> None:
    actual = columns_by_table.get(table_name)
    if actual is None:
        return
    selected = [
        f"{_quote(connection, column)} = {expression}"
        for column, expression in assignments.items()
        if column in actual
    ]
    if selected:
        connection.execute(
            text(f"UPDATE {_quote(connection, table_name)} SET {', '.join(selected)}")
        )


def _scrub_identity(
    connection: Connection, columns_by_table: Mapping[str, set[str]]
) -> None:
    _update_existing(
        connection,
        columns_by_table,
        "subscribers",
        {
            "first_name": "'Customer'",
            "last_name": "substr(md5(id::text), 1, 8)",
            "display_name": "'Customer ' || substr(md5(id::text), 1, 8)",
            "email": ("'cust+' || replace(id::text, '-', '') || '@example.invalid'"),
            "email_verified": "false",
            "phone": "NULL",
            "nin": "NULL",
            "date_of_birth": "NULL",
            "gender": "'unknown'",
            "preferred_contact_method": "NULL",
            "avatar_url": "NULL",
            "company_name": "NULL",
            "legal_name": "NULL",
            "tax_id": "NULL",
            "domain": "NULL",
            "website": "NULL",
            "address_line1": "NULL",
            "address_line2": "NULL",
            "postal_code": "NULL",
            "subscriber_number": (
                "CASE WHEN subscriber_number IS NULL THEN NULL ELSE "
                "'SUB-' || id::text END"
            ),
            "account_number": (
                "CASE WHEN account_number IS NULL THEN NULL ELSE 'ACC-' || id::text END"
            ),
            "billing_name": "'Customer ' || substr(md5(id::text), 1, 8)",
            "billing_address_line1": "NULL",
            "billing_address_line2": "NULL",
            "billing_postal_code": "NULL",
            "notes": "NULL",
            "crm_subscriber_id": "NULL",
            "metadata": "NULL",
        },
    )
    _update_existing(
        connection,
        columns_by_table,
        "system_users",
        {
            "first_name": "'Staff'",
            "last_name": "substr(md5(id::text), 1, 8)",
            "display_name": "'Staff ' || substr(md5(id::text), 1, 8)",
            "email": ("'staff+' || replace(id::text, '-', '') || '@example.invalid'"),
            "phone": "NULL",
            "device_login_enabled": "false",
        },
    )
    _update_existing(
        connection,
        columns_by_table,
        "resellers",
        {
            "name": "'Reseller ' || substr(md5(id::text), 1, 8)",
            "contact_email": (
                "CASE WHEN contact_email IS NULL THEN NULL ELSE "
                "'reseller+' || substr(md5(id::text), 1, 12) || '@example.invalid' END"
            ),
            "contact_phone": "NULL",
            "notes": "NULL",
        },
    )
    _update_existing(
        connection,
        columns_by_table,
        "reseller_users",
        {
            "email": (
                "CASE WHEN email IS NULL THEN NULL ELSE "
                "'reseller-user+' || substr(md5(id::text), 1, 12) || "
                "'@example.invalid' END"
            ),
            "full_name": "'Reseller user ' || substr(md5(id::text), 1, 8)",
        },
    )
    _update_existing(
        connection,
        columns_by_table,
        "organizations",
        {
            "name": "'Organization ' || substr(md5(id::text), 1, 8)",
            "legal_name": "NULL",
            "tax_id": "NULL",
            "domain": "NULL",
            "website": "NULL",
            "phone": "NULL",
            "email": "NULL",
            "address_line1": "NULL",
            "address_line2": "NULL",
            "postal_code": "NULL",
            "notes": "NULL",
            "metadata": "NULL",
        },
    )
    _update_existing(
        connection,
        columns_by_table,
        "addresses",
        {
            "label": "NULL",
            "address_line1": "'Redacted address'",
            "address_line2": "NULL",
            "postal_code": "NULL",
            "latitude": "NULL",
            "longitude": "NULL",
            "geom": "NULL",
        },
    )
    _update_existing(
        connection,
        columns_by_table,
        "subscriber_channels",
        {
            "address": "'redacted-' || substr(md5(id::text), 1, 12)",
            "label": "NULL",
            "is_verified": "false",
            "verified_at": "NULL",
            "metadata": "NULL",
        },
    )
    _update_existing(
        connection,
        columns_by_table,
        "subscriber_contacts",
        {
            "full_name": "'Contact ' || substr(md5(id::text), 1, 8)",
            "phone": "NULL",
            "email": "NULL",
            "whatsapp": "NULL",
            "facebook": "NULL",
            "instagram": "NULL",
            "x_handle": "NULL",
            "telegram": "NULL",
            "linkedin": "NULL",
            "other_social": "NULL",
            "notes": "NULL",
            "receives_notifications": "false",
        },
    )
    _update_existing(
        connection,
        columns_by_table,
        "subscriber_nin_verifications",
        {
            "nin": "'00000000000'",
            "mono_response": "NULL",
            "failure_reason": "NULL",
        },
    )
    _update_existing(
        connection,
        columns_by_table,
        "mfa_methods",
        {"phone": "NULL", "email": "NULL", "enabled": "false"},
    )
    _update_existing(
        connection,
        columns_by_table,
        "user_credentials",
        {
            "username": (
                "CASE WHEN username IS NULL THEN NULL ELSE "
                "'login-' || replace(id::text, '-', '') END"
            )
        },
    )
    _update_existing(
        connection,
        columns_by_table,
        "access_credentials",
        {"username": "'access-' || replace(id::text, '-', '')"},
    )
    _update_existing(
        connection,
        columns_by_table,
        "radius_users",
        {"username": "'radius-' || replace(id::text, '-', '')"},
    )
    _update_existing(
        connection,
        columns_by_table,
        "subscriptions",
        {
            "login": (
                "CASE WHEN login IS NULL THEN NULL ELSE "
                "'service-' || replace(id::text, '-', '') END"
            ),
            "ipv4_address": "NULL",
            "ipv6_address": "NULL",
            "last_seen_framed_ipv4": "NULL",
            "last_seen_framed_ipv6": "NULL",
            "mac_address": "NULL",
        },
    )
    _update_existing(
        connection,
        columns_by_table,
        "payment_methods",
        {
            "label": "'Redacted'",
            "last4": "'0000'",
            "brand": "'redacted'",
            "expires_month": "NULL",
            "expires_year": "NULL",
        },
    )
    _update_existing(
        connection,
        columns_by_table,
        "bank_accounts",
        {"account_last4": "'0000'", "routing_last4": "'0000'"},
    )


def _scrub_typed_settings(
    connection: Connection, columns_by_table: Mapping[str, set[str]]
) -> None:
    for table_name in ("domain_settings", "subscription_engine_settings"):
        required = {"value_type", "value_text", "value_json", "is_secret"}
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
                 WHERE is_secret IS TRUE
                """
            )
        )

    custom_field_columns = columns_by_table.get("subscriber_custom_fields", set())
    if {"value_type", "value_text", "value_json"}.issubset(custom_field_columns):
        connection.execute(
            text(
                """
                UPDATE subscriber_custom_fields
                   SET value_text = CASE WHEN value_type::text = 'json'
                                         THEN NULL ELSE '!scrubbed' END,
                       value_json = CASE WHEN value_type::text = 'json'
                                         THEN '{}'::jsonb ELSE NULL END
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
    for table_name in (
        "connector_configs",
        "integration_hooks",
        "webhook_endpoints",
        "payment_providers",
    ):
        if "is_active" in columns_by_table.get(table_name, set()):
            connection.execute(
                text(f"UPDATE {_quote(connection, table_name)} SET is_active = false")
            )
    if "headers" in columns_by_table.get("connector_configs", set()):
        connection.execute(text("UPDATE connector_configs SET headers = NULL"))


def _verification_residuals(
    connection: Connection, columns_by_table: Mapping[str, set[str]]
) -> dict[str, int]:
    residuals: dict[str, int] = {}
    for (table_name, column_name), action in SECRET_ACTIONS.items():
        if column_name not in columns_by_table.get(table_name, set()):
            continue
        table = _quote(connection, table_name)
        column = _quote(connection, column_name)
        if action.kind == "null":
            condition = f"{column} IS NOT NULL"
        elif action.kind == "unique_marker":
            condition = f"{column} <> '!scrubbed-' || id::text"
        else:
            condition = f"{column} IS NOT NULL AND {column} <> :marker"
        residuals[f"{table_name}.{column_name}"] = int(
            connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE {condition}"),
                {"marker": action.marker},
            ).scalar_one()
        )
    for table_name in DELETE_TABLES:
        if table_name in columns_by_table:
            residuals[f"{table_name}.rows"] = int(
                connection.execute(
                    text(f"SELECT count(*) FROM {_quote(connection, table_name)}")
                ).scalar_one()
            )
    identity_checks = (
        (
            "subscribers.identity",
            "subscribers",
            {"first_name", "email", "phone", "nin", "address_line1"},
            "first_name <> 'Customer' OR email NOT LIKE '%@example.invalid' "
            "OR phone IS NOT NULL OR nin IS NOT NULL OR address_line1 IS NOT NULL",
        ),
        (
            "system_users.identity",
            "system_users",
            {"first_name", "email", "phone"},
            "first_name <> 'Staff' OR email NOT LIKE '%@example.invalid' "
            "OR phone IS NOT NULL",
        ),
        (
            "resellers.identity",
            "resellers",
            {"contact_email", "contact_phone"},
            "(contact_email IS NOT NULL AND contact_email NOT LIKE '%@example.invalid') "
            "OR contact_phone IS NOT NULL",
        ),
        (
            "reseller_users.identity",
            "reseller_users",
            {"email"},
            "email IS NOT NULL AND email NOT LIKE '%@example.invalid'",
        ),
        (
            "organizations.identity",
            "organizations",
            {"email", "phone", "address_line1", "legal_name", "tax_id"},
            "email IS NOT NULL OR phone IS NOT NULL OR address_line1 IS NOT NULL "
            "OR legal_name IS NOT NULL OR tax_id IS NOT NULL",
        ),
        (
            "addresses.identity",
            "addresses",
            {"address_line1", "address_line2", "postal_code", "geom"},
            "address_line1 <> 'Redacted address' OR address_line2 IS NOT NULL "
            "OR postal_code IS NOT NULL OR geom IS NOT NULL",
        ),
        (
            "subscriber_channels.identity",
            "subscriber_channels",
            {"address"},
            "address NOT LIKE 'redacted-%'",
        ),
        (
            "subscriber_contacts.identity",
            "subscriber_contacts",
            {"phone", "email", "whatsapp", "other_social"},
            "phone IS NOT NULL OR email IS NOT NULL OR whatsapp IS NOT NULL "
            "OR other_social IS NOT NULL",
        ),
        (
            "mfa_methods.identity",
            "mfa_methods",
            {"phone", "email"},
            "phone IS NOT NULL OR email IS NOT NULL",
        ),
        (
            "user_credentials.identity",
            "user_credentials",
            {"username"},
            "username IS NOT NULL AND username NOT LIKE 'login-%'",
        ),
        (
            "access_credentials.identity",
            "access_credentials",
            {"username"},
            "username NOT LIKE 'access-%'",
        ),
        (
            "subscriptions.identity",
            "subscriptions",
            {"login", "ipv4_address", "ipv6_address", "mac_address"},
            "(login IS NOT NULL AND login NOT LIKE 'service-%') "
            "OR ipv4_address IS NOT NULL OR ipv6_address IS NOT NULL "
            "OR mac_address IS NOT NULL",
        ),
    )
    for name, table_name, required, condition in identity_checks:
        if not required.issubset(columns_by_table.get(table_name, set())):
            continue
        residuals[name] = int(
            connection.execute(
                text(
                    f"SELECT count(*) FROM {_quote(connection, table_name)} "
                    f"WHERE {condition}"
                )
            ).scalar_one()
        )
    for table_name in ("domain_settings", "subscription_engine_settings"):
        if {"value_type", "value_text", "value_json", "is_secret"}.issubset(
            columns_by_table.get(table_name, set())
        ):
            table = _quote(connection, table_name)
            residuals[f"{table_name}.secret_values"] = int(
                connection.execute(
                    text(
                        f"""
                        SELECT count(*) FROM {table}
                         WHERE is_secret IS TRUE
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
    incompatible = incompatible_secret_actions(columns)
    if incompatible:
        names = ", ".join(
            f"{column.table_name}.{column.column_name}" for column in incompatible
        )
        raise ScrubSafetyError(
            "secret columns require a reviewed non-null scrub action: " + names
        )

    before = _financial_fingerprint(connection, columns_by_table)
    _delete_capability_rows(connection, columns_by_table)
    _apply_secret_actions(connection, columns_by_table)
    _scrub_identity(connection, columns_by_table)
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
