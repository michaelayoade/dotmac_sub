from types import SimpleNamespace
from uuid import uuid4

from app.services import provisioning_context, provisioning_helpers
from app.services.network.subscriber_ont_adapter import ProvisioningContext
from app.services.provisioning_step_executors import execute_create_olt_service_port


class _EmptyQuery:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return None


class _FakeSession:
    def __init__(self, subscription):
        self.subscription = subscription

    def get(self, model, key):
        if model.__name__ == "Subscription":
            return self.subscription
        return None

    def query(self, *args, **kwargs):
        return _EmptyQuery()


def test_extend_provisioning_context_merges_canonical_network_context(monkeypatch):
    subscriber_id = uuid4()
    subscription_id = uuid4()
    resolved_ont_id = uuid4()
    nas_device_id = uuid4()
    service_address_id = uuid4()
    db = _FakeSession(SimpleNamespace(id=subscription_id, subscriber_id=subscriber_id))

    def fake_resolve(db_arg, *, subscriber_id=None, subscription_id=None, ont_id=None):
        assert db_arg is db
        assert subscriber_id == str(subscriber_id_param)
        assert subscription_id == str(subscription_id_param)
        return ProvisioningContext(
            subscriber_id=str(subscriber_id_param),
            subscription_id=str(subscription_id_param),
            ont_id=str(resolved_ont_id),
            ont_serial="ONT123",
            olt_id=str(uuid4()),
            olt_name="OLT-A",
            fsp="0/1/2",
            ont_id_on_olt=17,
            service_address_id=str(service_address_id),
            nas_device_id=str(nas_device_id),
        )

    subscriber_id_param = subscriber_id
    subscription_id_param = subscription_id
    monkeypatch.setattr(
        provisioning_context,
        "resolve_operations_provisioning_context",
        fake_resolve,
    )

    context = {}

    provisioning_context.extend_provisioning_context(
        db,
        str(subscription_id),
        context,
    )

    assert context["subscriber_id"] == str(subscriber_id)
    assert context["subscription_id"] == str(subscription_id)
    assert context["ont_id"] == str(resolved_ont_id)
    assert context["ont_unit_id"] == str(resolved_ont_id)
    assert context["nas_device_id"] == str(nas_device_id)
    assert context["service_address_id"] == str(service_address_id)
    assert context["fsp"] == "0/1/2"
    assert context["ont_id_on_olt"] == 17


def test_legacy_extend_provisioning_context_delegates(monkeypatch):
    calls = {}

    def fake_extend(db, subscription_id, context):
        calls["args"] = (db, subscription_id, context)
        context["delegated"] = True
        return context

    monkeypatch.setattr(
        provisioning_helpers, "extend_provisioning_context", fake_extend
    )
    context = {}
    result = provisioning_helpers._extend_provisioning_context(
        object(),
        "subscription-1",
        context,
    )

    assert result is context
    assert context["delegated"] is True
    assert calls["args"][1] == "subscription-1"


def test_olt_service_port_executor_uses_operations_context(monkeypatch):
    def fake_resolve(db, *, subscriber_id=None, subscription_id=None, ont_id=None):
        return SimpleNamespace(ont_id="ont-1")

    monkeypatch.setattr(
        provisioning_context,
        "resolve_operations_provisioning_context",
        fake_resolve,
    )

    result = execute_create_olt_service_port(
        object(),
        {"subscription_id": "subscription-1"},
        {},
    )

    assert result.status == "failed"
    assert result.detail == (
        "VLAN ID is required in step config for OLT service-port creation."
    )
