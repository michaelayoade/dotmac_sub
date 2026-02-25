from __future__ import annotations

from app.models.subscriber import Subscriber, SubscriberStatus, UserType
from app.schemas.table_config import TableColumnPreference
from app.services.table_config import TableConfigurationService, TableRegistry


def _subscriber(db_session, email: str, first_name: str = "Test") -> Subscriber:
    subscriber = Subscriber(
        first_name=first_name,
        last_name="User",
        email=email,
        status=SubscriberStatus.active,
        is_active=True,
        user_type=UserType.system_user,
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
            TableColumnPreference(column_key="email", display_order=0, is_visible=False),
            TableColumnPreference(
                column_key="customer_name", display_order=1, is_visible=True
            ),
        ],
    )

    email_col = next(column for column in updated if column.column_key == "email")
    assert email_col.is_visible is False


def test_apply_query_config_selects_only_visible_columns(db_session):
    user = _subscriber(db_session, "table-data-1@example.com", first_name="Alice")
    _subscriber(db_session, "table-data-2@example.com", first_name="Bob")

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
            "limit": "20",
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


def test_resolution_hierarchy_user_then_system_then_registry(db_session):
    user = _subscriber(db_session, "hier-user@example.com")
    other = _subscriber(db_session, "hier-other@example.com")

    # No user/default config -> registry fallback.
    registry_columns = TableConfigurationService.get_columns(db_session, user.id, "customers")
    assert registry_columns

    # Add system defaults.
    TableConfigurationService.save_system_default_columns(
        db_session,
        "customers",
        payload=[
            TableColumnPreference(column_key="email", display_order=0, is_visible=True),
            TableColumnPreference(column_key="customer_name", display_order=1, is_visible=True),
            TableColumnPreference(column_key="status", display_order=2, is_visible=True),
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
            TableColumnPreference(column_key="customer_name", display_order=0, is_visible=True),
            TableColumnPreference(column_key="email", display_order=1, is_visible=False),
        ],
    )
    user_columns = TableConfigurationService.get_columns(db_session, user.id, "customers")
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

    columns_a = TableConfigurationService.get_columns(db_session, user_a.id, "subscribers")
    columns_b = TableConfigurationService.get_columns(db_session, user_b.id, "subscribers")

    email_a = next(column for column in columns_a if column.column_key == "email")
    email_b = next(column for column in columns_b if column.column_key == "email")

    assert email_a.is_visible is False
    assert email_b.is_visible is not False
