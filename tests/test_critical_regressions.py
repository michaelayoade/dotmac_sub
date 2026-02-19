from app.models.catalog import NasDevice, NasVendor
from app.services import customer_portal
from app.services.collections._core import _get_account_email
from app.services.enforcement import (
    _apply_mikrotik_address_list,
    _remove_mikrotik_address_list,
)


def test_get_account_email_uses_subscriber_email(db_session, subscriber):
    email = _get_account_email(db_session, str(subscriber.id))
    assert email == subscriber.email


def test_apply_mikrotik_address_list_executes_add_command(monkeypatch):
    commands: list[str] = []

    def _fake_execute_ssh(_device, command):
        commands.append(command)

    monkeypatch.setattr("app.services.enforcement.DeviceProvisioner._execute_ssh", _fake_execute_ssh)

    device = NasDevice(name="edge-1", vendor=NasVendor.mikrotik)
    result = _apply_mikrotik_address_list(device, "blocked", "192.0.2.10")

    assert result is True
    assert commands == [
        '/ip firewall address-list add list="blocked" address="192.0.2.10"'
    ]


def test_remove_mikrotik_address_list_executes_remove_command(monkeypatch):
    commands: list[str] = []

    def _fake_execute_ssh(_device, command):
        commands.append(command)

    monkeypatch.setattr("app.services.enforcement.DeviceProvisioner._execute_ssh", _fake_execute_ssh)

    device = NasDevice(name="edge-1", vendor=NasVendor.mikrotik)
    result = _remove_mikrotik_address_list(device, "blocked", "192.0.2.10")

    assert result is True
    assert commands == [
        '/ip firewall address-list remove [find list="blocked" address="192.0.2.10"]'
    ]


def test_get_invoice_billing_contact_uses_subscriber_fields_without_account_roles(
    db_session, subscriber
):
    invoice = type("Invoice", (), {"account_id": subscriber.id})()
    customer = {"subscriber_id": str(subscriber.id), "current_user": {}}

    result = customer_portal.get_invoice_billing_contact(db_session, invoice, customer)

    assert result["billing_email"] == subscriber.email
    assert result["billing_name"]
