from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models.subscriber import (
    Subscriber,
    SubscriberCategory,
    SubscriberStatus,
    UserType,
)
from app.schemas.table_config import TableColumnPreference
from app.services.table_config import TableConfigurationService, TableRegistry


def _subscriber(
    db_session,
    email: str,
    first_name: str = "Test",
    *,
    user_type: UserType = UserType.system_user,
) -> Subscriber:
    subscriber = Subscriber(
        first_name=first_name,
        last_name="User",
        email=email,
        status=SubscriberStatus.active,
        is_active=True,
        user_type=user_type,
        billing_enabled=True,
        marketing_opt_in=False,
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


def test_registry_has_customers_and_subscribers():
    assert TableRegistry.exists("customers") is True
    assert TableRegistry.exists("subscribers") is True


def test_default_columns_are_generated_without_saved_config(db_session):
    user = _subscriber(db_session, "table-default@example.com")

    columns = TableConfigurationService.get_columns(db_session, user.id, "customers")

    assert columns
    assert columns[0].column_key == "customer_name"
    assert all(isinstance(column.is_visible, bool) for column in columns)


def test_save_columns_validates_and_persists(db_session):
    user = _subscriber(db_session, "table-save@example.com")

    updated = TableConfigurationService.save_columns(
        db_session,
        user.id,
        "customers",
        payload=[
            TableColumnPreference(
                column_key="email", display_order=0, is_visible=False
            ),
            TableColumnPreference(
                column_key="customer_name", display_order=1, is_visible=True
            ),
        ],
    )

    email_col = next(column for column in updated if column.column_key == "email")
    assert email_col.is_visible is False


def test_apply_query_config_selects_only_visible_columns(db_session):
    # ``apply_query_config`` for the "subscribers" table filters out
    # ``UserType.system_user`` rows (production excludes internal/system
    # accounts from subscriber listings), so the rows we expect the query
    # to return must be customer-type.
    user = _subscriber(
        db_session,
        "table-data-1@example.com",
        first_name="Alice",
        user_type=UserType.customer,
    )
    _subscriber(
        db_session,
        "table-data-2@example.com",
        first_name="Bob",
        user_type=UserType.customer,
    )

    TableConfigurationService.save_columns(
        db_session,
        user.id,
        "subscribers",
        payload=[
            TableColumnPreference(
                column_key="subscriber_name",
                display_order=0,
                is_visible=True,
            ),
            TableColumnPreference(
                column_key="email",
                display_order=1,
                is_visible=False,
            ),
            TableColumnPreference(
                column_key="status",
                display_order=2,
                is_visible=True,
            ),
        ],
    )

    columns, items, count = TableConfigurationService.apply_query_config(
        db_session,
        user.id,
        "subscribers",
        {
            "limit": "25",
            "offset": "0",
            "q": "example.com",
            "status": "active",
            "sort_by": "subscriber_name",
            "sort_dir": "asc",
            "_ts": "1771944545000",
        },
    )

    assert count >= 2
    visibility_by_key = {column.column_key: column.is_visible for column in columns}
    assert visibility_by_key["subscriber_name"] is True
    assert visibility_by_key["email"] is False
    assert items
    assert all("email" not in item for item in items)
    assert all("subscriber_name" in item for item in items)

    sortable = {column.column_key for column in columns if column.sortable}
    assert sortable == {
        "subscriber_number",
        "status",
        "created_at",
        "updated_at",
        "subscriber_name",
    }


def test_resolution_hierarchy_user_then_system_then_registry(db_session):
    user = _subscriber(db_session, "hier-user@example.com")
    other = _subscriber(db_session, "hier-other@example.com")

    # No user/default config -> registry fallback.
    registry_columns = TableConfigurationService.get_columns(
        db_session, user.id, "customers"
    )
    assert registry_columns

    # Add system defaults.
    TableConfigurationService.save_system_default_columns(
        db_session,
        "customers",
        payload=[
            TableColumnPreference(column_key="email", display_order=0, is_visible=True),
            TableColumnPreference(
                column_key="customer_name", display_order=1, is_visible=True
            ),
            TableColumnPreference(
                column_key="status", display_order=2, is_visible=True
            ),
        ],
    )

    system_columns_for_other = TableConfigurationService.get_columns(
        db_session, other.id, "customers"
    )
    assert system_columns_for_other[0].column_key == "email"

    # User config overrides system defaults.
    TableConfigurationService.save_columns(
        db_session,
        user.id,
        "customers",
        payload=[
            TableColumnPreference(
                column_key="customer_name", display_order=0, is_visible=True
            ),
            TableColumnPreference(
                column_key="email", display_order=1, is_visible=False
            ),
        ],
    )
    user_columns = TableConfigurationService.get_columns(
        db_session, user.id, "customers"
    )
    assert user_columns[0].column_key == "customer_name"
    email_col = next(column for column in user_columns if column.column_key == "email")
    assert email_col.is_visible is False


def test_user_configuration_is_isolated_per_user(db_session):
    user_a = _subscriber(db_session, "isol-a@example.com", first_name="UserA")
    user_b = _subscriber(db_session, "isol-b@example.com", first_name="UserB")

    TableConfigurationService.save_columns(
        db_session,
        user_a.id,
        "subscribers",
        payload=[
            TableColumnPreference(
                column_key="subscriber_name",
                display_order=0,
                is_visible=True,
            ),
            TableColumnPreference(
                column_key="email",
                display_order=1,
                is_visible=False,
            ),
        ],
    )

    columns_a = TableConfigurationService.get_columns(
        db_session, user_a.id, "subscribers"
    )
    columns_b = TableConfigurationService.get_columns(
        db_session, user_b.id, "subscribers"
    )

    subscriber_name_a = next(
        column for column in columns_a if column.column_key == "subscriber_name"
    )
    subscriber_name_b = next(
        column for column in columns_b if column.column_key == "subscriber_name"
    )
    email_a = next(column for column in columns_a if column.column_key == "email")
    email_b = next(column for column in columns_b if column.column_key == "email")

    assert subscriber_name_a.display_order == 0
    assert subscriber_name_b.display_order != 0
    assert email_a.is_visible is False
    assert email_a.display_order == 1
    assert email_b.display_order != 1


def test_customers_table_returns_business_account_id_meta_for_org_rows(db_session):
    user = _subscriber(db_session, "table-org-user@example.com")
    org_subscriber = Subscriber(
        first_name="Org",
        last_name="Member",
        email="org.member@example.com",
        company_name="Acme Ltd",
        status=SubscriberStatus.active,
        is_active=True,
        user_type=UserType.customer,
        billing_enabled=True,
        marketing_opt_in=False,
    )
    org_subscriber.category = SubscriberCategory.business
    db_session.add(org_subscriber)
    db_session.commit()

    _, items, _ = TableConfigurationService.apply_query_config(
        db_session,
        user.id,
        "customers",
        {
            "limit": "25",
            "offset": "0",
            "customer_type": "business",
            "sort_by": "created_at",
            "sort_dir": "desc",
        },
    )

    org_item = next(item for item in items if item["id"] == str(org_subscriber.id))
    assert org_item["customer_type"] == "business"
    assert org_item["business_account_id"] == str(org_subscriber.id)


def test_customer_table_data_uses_canonical_scope_and_effective_offset(db_session):
    user = _subscriber(db_session, "table-canonical-user@example.com")
    customer = _subscriber(
        db_session,
        "table-canonical-customer@example.com",
        first_name="Canonical",
        user_type=UserType.customer,
    )
    reseller = _subscriber(
        db_session,
        "table-canonical-reseller@example.com",
        first_name="Canonical",
        user_type=UserType.reseller,
    )

    projection = TableConfigurationService.build_data_projection(
        db_session,
        user.id,
        "customers",
        {
            "limit": "25",
            "offset": "100",
            "q": "Canonical",
            "sort_by": "customer_name",
            "sort_dir": "asc",
        },
    )

    item_ids = {item["id"] for item in projection.items}
    assert str(customer.id) in item_ids
    assert str(reseller.id) not in item_ids
    assert projection.count == 1
    assert projection.limit == 25
    assert projection.offset == 0


def test_customer_table_data_rejects_legacy_scalar_filter_path(db_session):
    user = _subscriber(db_session, "table-legacy-filter@example.com")

    with pytest.raises(HTTPException, match="Unsupported customer list parameters"):
        TableConfigurationService.apply_query_config(
            db_session,
            user.id,
            "customers",
            {"billing_enabled": "true"},
        )


def test_subscriber_table_data_is_read_only_for_missing_subscriber_number(db_session):
    user = _subscriber(db_session, "subscriber-table-reader@example.com")
    subscriber = _subscriber(
        db_session,
        "subscriber-without-number@example.com",
        first_name="NoNumber",
        user_type=UserType.customer,
    )
    assert subscriber.subscriber_number is None

    projection = TableConfigurationService.build_data_projection(
        db_session,
        user.id,
        "subscribers",
        {
            "limit": "25",
            "offset": "0",
            "q": "subscriber-without-number@example.com",
            "sort_by": "subscriber_name",
            "sort_dir": "asc",
        },
    )

    row = next(item for item in projection.items if item["id"] == str(subscriber.id))
    assert row["subscriber_number"] is None
    db_session.refresh(subscriber)
    assert subscriber.subscriber_number is None


def test_subscriber_table_data_clamps_offset_and_excludes_system_users(db_session):
    user = _subscriber(db_session, "subscriber-canonical-reader@example.com")
    subscriber = _subscriber(
        db_session,
        "subscriber-canonical-customer@example.com",
        first_name="SubscriberCanonical",
        user_type=UserType.customer,
    )
    system_user = _subscriber(
        db_session,
        "subscriber-canonical-system@example.com",
        first_name="SubscriberCanonical",
        user_type=UserType.system_user,
    )

    projection = TableConfigurationService.build_data_projection(
        db_session,
        user.id,
        "subscribers",
        {
            "limit": "25",
            "offset": "100",
            "q": "SubscriberCanonical",
            "sort_by": "subscriber_name",
            "sort_dir": "asc",
        },
    )

    item_ids = {item["id"] for item in projection.items}
    assert str(subscriber.id) in item_ids
    assert str(system_user.id) not in item_ids
    assert projection.count == 1
    assert projection.offset == 0


def test_subscriber_table_data_rejects_generic_scalar_filter_path(db_session):
    user = _subscriber(db_session, "subscriber-legacy-filter@example.com")

    with pytest.raises(HTTPException, match="Unsupported subscriber list parameters"):
        TableConfigurationService.apply_query_config(
            db_session,
            user.id,
            "subscribers",
            {"billing_enabled": "true"},
        )
