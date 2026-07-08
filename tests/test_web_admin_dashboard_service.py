from app.models.network_monitoring import (
    DeviceInterface,
    InterfaceStatus,
    NetworkDevice,
)
from app.services.web_admin_dashboard import (
    _build_online_customer_summary,
    _build_pon_interface_summary,
)


def test_build_online_customer_summary_counts_distinct_customers(
    db_session, subscriber
):
    from datetime import UTC, datetime

    from app.models.radius_active_session import RadiusActiveSession
    from app.models.subscriber import Subscriber

    second = Subscriber(
        first_name="Online",
        last_name="Customer",
        email="online-customer@example.com",
    )
    db_session.add(second)
    db_session.flush()

    now = datetime(2026, 7, 7, 7, 0, tzinfo=UTC)
    db_session.add_all(
        [
            RadiusActiveSession(
                subscriber_id=subscriber.id,
                username="cust-1-a",
                acct_session_id="sess-1",
                session_start=now,
            ),
            RadiusActiveSession(
                subscriber_id=subscriber.id,
                username="cust-1-b",
                acct_session_id="sess-2",
                session_start=now,
            ),
            RadiusActiveSession(
                subscriber_id=second.id,
                username="cust-2",
                acct_session_id="sess-3",
                session_start=now,
            ),
            RadiusActiveSession(
                username="unresolved",
                acct_session_id="sess-4",
                session_start=now,
            ),
        ]
    )
    db_session.commit()

    assert _build_online_customer_summary(db_session) == {
        "sessions": 4,
        "customers": 2,
    }


def test_build_pon_interface_summary_counts_only_pon_like_interfaces(db_session):
    device = NetworkDevice(name="OLT Monitor", is_active=True)
    inactive_device = NetworkDevice(name="Inactive OLT Monitor", is_active=False)
    db_session.add_all([device, inactive_device])
    db_session.flush()

    db_session.add_all(
        [
            DeviceInterface(
                device_id=device.id,
                name="gpon 0/1/0",
                status=InterfaceStatus.up,
            ),
            DeviceInterface(
                device_id=device.id,
                name="uplink0",
                description="core uplink",
                status=InterfaceStatus.up,
            ),
            DeviceInterface(
                device_id=device.id,
                name="xgs-pon 0/1/1",
                status=InterfaceStatus.down,
            ),
            DeviceInterface(
                device_id=device.id,
                name="if3",
                description="PON board port",
                status=InterfaceStatus.unknown,
            ),
            DeviceInterface(
                device_id=inactive_device.id,
                name="gpon 0/2/0",
                status=InterfaceStatus.down,
            ),
        ]
    )
    db_session.commit()

    summary = _build_pon_interface_summary(db_session)

    assert summary == {
        "up": 1,
        "down": 1,
        "unknown": 1,
        "total": 3,
    }
