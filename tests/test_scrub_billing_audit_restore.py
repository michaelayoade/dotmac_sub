"""Safety regression tests for the isolated billing-audit restore scrub."""

from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.one_off import scrub_billing_audit_restore as scrub_module
from scripts.one_off.scrub_billing_audit_restore import (
    SCRUB_ACTIONS,
    ColumnInfo,
    ScrubSafetyError,
    assert_fingerprints_equal,
    assert_no_residuals,
    incompatible_scrub_actions,
    incompatible_secret_actions,
    typed_secret_values,
    unknown_secret_columns,
    unknown_sensitive_columns,
    validate_target,
)


def _column(table: str, name: str, *, nullable: bool = True) -> ColumnInfo:
    return ColumnInfo(table, name, "text", nullable)


def test_target_requires_all_three_write_gates():
    validate_target(
        database_name="dotmac_sub_audit",
        dialect_name="postgresql",
        execute=True,
        ephemeral_flag="1",
    )

    invalid = [
        {"execute": False},
        {"ephemeral_flag": None},
        {"database_name": "dotmac_sub"},
        {"dialect_name": "sqlite"},
    ]
    baseline = {
        "database_name": "dotmac_sub_audit",
        "dialect_name": "postgresql",
        "execute": True,
        "ephemeral_flag": "1",
    }
    for override in invalid:
        with pytest.raises(ScrubSafetyError):
            validate_target(**(baseline | override))


def test_cli_refuses_before_opening_a_connection(monkeypatch):
    def unexpected_connection():
        raise AssertionError("database connection should not be opened")

    monkeypatch.setattr(scrub_module, "_engine", unexpected_connection)
    monkeypatch.delenv("BILLING_AUDIT_EPHEMERAL", raising=False)
    with pytest.raises(ScrubSafetyError, match="BILLING_AUDIT_EPHEMERAL"):
        scrub_module.main(["--execute"])

    monkeypatch.setenv("BILLING_AUDIT_EPHEMERAL", "1")
    with pytest.raises(ScrubSafetyError, match="without --execute"):
        scrub_module.main([])


def test_new_secret_column_fails_closed():
    unknown = unknown_secret_columns(
        [
            _column("nas_devices", "shared_secret"),
            _column("future_integrations", "super_secret"),
        ]
    )

    assert [(item.table_name, item.column_name) for item in unknown] == [
        ("future_integrations", "super_secret")
    ]


def test_secret_metadata_and_deleted_capability_tables_do_not_false_positive():
    columns = [
        _column("domain_settings", "is_secret", nullable=False),
        _column("oauth_tokens", "access_token"),
        _column("radius_users", "access_credential_id", nullable=False),
        _column("system_users", "device_login_secret_set_at"),
        _column("ai_insights", "tokens_in"),
    ]

    assert unknown_secret_columns(columns) == []
    assert incompatible_secret_actions(columns) == []


def test_current_model_schema_has_no_unclassified_secret_columns():
    import app.models  # noqa: F401
    from app.db import Base

    columns = [
        ColumnInfo(table.name, column.name, str(column.type), column.nullable)
        for table in Base.metadata.tables.values()
        for column in table.columns
    ]

    assert unknown_secret_columns(columns) == []
    assert unknown_sensitive_columns(columns) == []
    assert incompatible_secret_actions(columns) == []
    assert incompatible_scrub_actions(columns) == []


def test_new_subscriber_pii_column_fails_closed():
    unknown = unknown_sensitive_columns(
        [
            _column("subscribers", "id", nullable=False),
            _column("subscribers", "category"),
            _column("subscribers", "passport_number"),
        ]
    )

    assert [(item.table_name, item.column_name) for item in unknown] == [
        ("subscribers", "passport_number")
    ]


def test_new_or_changed_integration_schema_fails_closed():
    unknown = unknown_sensitive_columns(
        [
            _column("integration_hooks", "headers"),
            _column("future_integration_adapter", "id", nullable=False),
            _column("future_integration_adapter", "configuration"),
        ]
    )

    assert [(item.table_name, item.column_name) for item in unknown] == [
        ("future_integration_adapter", "configuration"),
        ("future_integration_adapter", "id"),
        ("integration_hooks", "headers"),
    ]


def test_every_direct_scrub_action_defines_its_verification():
    assert SCRUB_ACTIONS
    for key, action in SCRUB_ACTIONS.items():
        assert action.expression, key
        assert "{column}" in action.residual_condition, key


def test_opaque_integration_payloads_are_scrubbed_explicitly():
    expected = {
        ("connector_configs", "headers"),
        ("integration_connectors", "configuration"),
        ("integration_hooks", "auth_config"),
        ("integration_records", "payload_snapshot"),
        ("payment_webhook_dead_letters", "payload"),
    }

    assert expected <= SCRUB_ACTIONS.keys()


def test_non_null_secret_columns_have_explicit_marker_actions():
    columns = [
        _column("routers", "rest_api_password", nullable=False),
        _column("radius_clients", "shared_secret_hash", nullable=False),
        _column("user_credentials", "password_hash"),
    ]

    assert unknown_secret_columns(columns) == []
    assert incompatible_secret_actions(columns) == []


def test_nullable_scrub_action_fails_closed_if_schema_makes_column_required():
    columns = [_column("access_credentials", "secret_hash", nullable=False)]

    assert incompatible_secret_actions(columns) == columns

    pii_columns = [_column("subscribers", "phone", nullable=False)]
    assert incompatible_scrub_actions(pii_columns) == pii_columns


@pytest.mark.parametrize(
    ("value_type", "expected"),
    [
        ("string", ("!scrubbed", None)),
        ("integer", ("!scrubbed", None)),
        ("boolean", ("!scrubbed", None)),
        ("json", (None, {})),
    ],
)
def test_typed_secret_settings_preserve_value_alignment(value_type, expected):
    assert typed_secret_values(value_type) == expected


def test_financial_fingerprint_must_be_exact():
    before = {
        "payments": ("98284", "100.00", "5.00"),
        "service_extensions": ("4", "10"),
    }
    assert_fingerprints_equal(before, dict(before))

    after = dict(before)
    after["payments"] = ("98284", str(Decimal("99.99")), "5.00")
    with pytest.raises(ScrubSafetyError, match="payments"):
        assert_fingerprints_equal(before, after)


def test_any_verification_residual_aborts_the_scrub():
    assert_no_residuals({"sessions.rows": 0, "subscribers.identity": 0})

    with pytest.raises(ScrubSafetyError, match="routers.rest_api_password"):
        assert_no_residuals({"sessions.rows": 0, "routers.rest_api_password": 1})
