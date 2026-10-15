"""Capture sensitivity canaries; execution belongs to hosted CI."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.exc import PendingRollbackError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.models.integration_platform import (
    IntegrationCapabilityBinding,
    IntegrationConfigRevision,
    IntegrationInstallation,
)
from app.services.integrations import (
    installations,
    payment_capability,
    runtime_execution,
)
from app.services.integrations.connectors.payment_gateway import (
    PAYMENT_RECONCILE_CAPABILITY,
    PaymentGatewayRunner,
    _decode_capture_record,
)
from app.services.integrations.payment_capture import (
    AssertedMerchantNamespace,
    CaptureProvider,
    CaptureRefusal,
    PaymentCaptureError,
    PaymentCaptureQuery,
)
from app.services.integrations.runtime import OperationStatus, ValidationResult
from app.services.integrations.runtime_execution import (
    _build_execution_context_from_binding,
    build_execution_context,
)
from tests.integration_platform_helpers import enable_payment_provider
from tests.test_integration_payment_capability import _Client, _envelope


def _transaction() -> dict[str, object]:
    return {
        "id": 123,
        "reference": "opening-ref",
        "status": "success",
        "amount": 123456789012345678,
        "currency": "NGN",
        "fees": 10,
        "created_at": "2024-01-01T10:00:00Z",
        "paid_at": "2024-01-01T10:01:00Z",
        "customer": {"email": "private@example.test"},
        "authorization": {"authorization_code": "AUTH-private"},
        "metadata": {"private": "private-payload"},
    }


def _body(data: dict[str, object] | None = None) -> dict[str, object]:
    return {"status": True, "data": _transaction() if data is None else data}


def test_capture_retains_separate_states_exact_money_and_real_acquisition_time():
    data = _transaction()
    data["status"] = "failed"
    body = _body(data)
    client = _Client(body)
    before = datetime.now(UTC)
    result = PaymentGatewayRunner("paystack", client_override=client).execute(
        _envelope(
            "verify",
            {"reference": "opening-ref", "capture_record": True},
            capability_id=PAYMENT_RECONCILE_CAPABILITY,
        ),
        config={"base_url": "https://gateway.example.test"},
        secret_material={"gateway_credentials": "private-credential"},
    )
    record = _decode_capture_record(result.output)
    assert record.envelope_status is True
    assert record.transaction_status == "failed"
    assert record.amount == Decimal("123456789012345678")
    assert record.fee == Decimal(10)
    assert record.amount_unit == "minor"
    assert before <= record.retrieved_at <= datetime.now(UTC)
    assert record.provider_paid_at < record.retrieved_at
    assert set(result.output) == {"capture_record"}
    serialized = str(result.output)
    for forbidden in (
        "private@example.test",
        "AUTH-private",
        "private-payload",
        "private-credential",
        "customer",
        "authorization",
        "metadata",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("envelope_status", [False, "true", 1, None, "success"])
def test_capture_never_accepts_failed_or_coerced_paystack_envelope(envelope_status):
    body = _body()
    body["status"] = envelope_status
    result = PaymentGatewayRunner("paystack", client_override=_Client(body)).execute(
        _envelope(
            "verify",
            {"reference": "opening-ref", "capture_record": True},
            capability_id=PAYMENT_RECONCILE_CAPABILITY,
        ),
        config={"base_url": "https://gateway.example.test"},
        secret_material={"gateway_credentials": "test"},
    )
    assert result.status is OperationStatus.rejected
    assert not result.output


def _query(db: Session) -> PaymentCaptureQuery:
    binding = enable_payment_provider(db, "paystack")[PAYMENT_RECONCILE_CAPABILITY]
    revision = binding.installation.current_config_revision
    claim = AssertedMerchantNamespace(
        namespace="asserted-merchant-7",
        assertion_reference=uuid4(),
        installation_id=binding.installation_id,
        config_revision_id=revision.id,
        config_digest=revision.config_digest,
    )
    return PaymentCaptureQuery(
        provider=CaptureProvider.PAYSTACK,
        reference="opening-ref",
        installation_id=binding.installation_id,
        capability_binding_id=binding.id,
        config_revision_id=revision.id,
        config_digest=revision.config_digest,
        expected_amount=Decimal("1234567890123456.78"),
        expected_currency="NGN",
        asserted_merchant_namespace=claim,
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"asserted_merchant_namespace": None}, CaptureRefusal.UNBOUND),
        ({"installation_id": uuid4()}, CaptureRefusal.UNBOUND),
        ({"config_revision_id": uuid4()}, CaptureRefusal.UNBOUND),
    ],
)
def test_unbound_capture_refuses_before_db_secret_or_network(
    db_session, monkeypatch, change, reason
):
    query = replace(_query(db_session), **change)

    def forbidden(*args, **kwargs):
        raise AssertionError("unbound capture reached a protected boundary")

    monkeypatch.setattr(db_session, "execute", forbidden)
    monkeypatch.setattr(
        payment_capability, "_build_execution_context_from_binding", forbidden
    )
    assert (
        payment_capability.capture_transaction_record(db_session, query=query).refusal
        is reason
    )


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("reference", "other-ref", CaptureRefusal.REFERENCE_MISMATCH),
        ("currency", "USD", CaptureRefusal.CURRENCY_MISMATCH),
        ("amount", 1, CaptureRefusal.AMOUNT_MISMATCH),
        ("amount", True, CaptureRefusal.PROVIDER_FAILURE),
        ("currency", "ngn", CaptureRefusal.INVALID_RECORD),
    ],
)
def test_capture_validates_observations_without_financial_writes(
    db_session, monkeypatch, field, value, reason
):
    query = _query(db_session)
    data = _transaction()
    data[field] = value
    body = _body(data)
    client = _Client(body)

    def context(*, binding):
        return _build_execution_context_from_binding(
            binding=binding,
            runner_override=PaymentGatewayRunner("paystack", client_override=client),
            secret_resolver=lambda reference: "test-only-secret",
        )

    monkeypatch.setattr(
        payment_capability, "_build_execution_context_from_binding", context
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("capture attempted a database write")

    for method in ("add", "add_all", "delete", "flush", "commit", "rollback"):
        monkeypatch.setattr(db_session, method, forbidden)
    outcome = payment_capability.capture_transaction_record(db_session, query=query)
    assert outcome.refusal is reason
    assert outcome.record is None
    assert len(client.calls) == 1
    assert client.calls[0]["method"] == "GET"


def test_capture_namespace_claim_is_never_provider_observed(db_session, monkeypatch):
    query = _query(db_session)
    client = _Client(_body())

    def context(*, binding):
        return _build_execution_context_from_binding(
            binding=binding,
            runner_override=PaymentGatewayRunner("paystack", client_override=client),
            secret_resolver=lambda reference: "test-only-secret",
        )

    monkeypatch.setattr(
        payment_capability, "_build_execution_context_from_binding", context
    )
    binding = db_session.get(IntegrationCapabilityBinding, query.capability_binding_id)
    binding.installation.name = "dirty caller state must not flush"
    db_session.expire(binding, ["installation"])

    def forbidden(*args, **kwargs):
        raise AssertionError("capture attempted a database write")

    for method in ("add", "add_all", "delete", "flush", "commit", "rollback"):
        monkeypatch.setattr(db_session, method, forbidden)
    outcome = payment_capability.capture_transaction_record(db_session, query=query)
    assert outcome.refusal is None
    assert (
        outcome.provenance.asserted_merchant_namespace
        == query.asserted_merchant_namespace
    )
    assert not hasattr(outcome.record, "merchant_namespace")
    assert "asserted-merchant-7" not in str(client.calls)


@pytest.mark.parametrize(
    "scenario,reason",
    [
        ("missing_binding", CaptureRefusal.UNBOUND),
        ("wrong_provider", CaptureRefusal.UNSUPPORTED),
        ("stale_config", CaptureRefusal.STALE_BINDING),
        ("disabled", CaptureRefusal.UNSUPPORTED),
    ],
)
def test_capture_never_falls_back_to_an_enabled_default(
    db_session, monkeypatch, scenario, reason
):
    query = _query(db_session)
    if scenario == "missing_binding":
        query = replace(query, capability_binding_id=uuid4())
    elif scenario == "wrong_provider":
        query = replace(query, provider=CaptureProvider.FLUTTERWAVE)
    elif scenario == "stale_config":
        claim = replace(query.asserted_merchant_namespace, config_revision_id=uuid4())
        query = replace(
            query,
            config_revision_id=claim.config_revision_id,
            asserted_merchant_namespace=claim,
        )
    else:
        binding = db_session.get(
            IntegrationCapabilityBinding, query.capability_binding_id
        )
        db_session.execute(
            update(IntegrationCapabilityBinding)
            .where(IntegrationCapabilityBinding.id == binding.id)
            .values(state="disabled")
            .execution_options(synchronize_session=False)
        )

    def forbidden(reference):
        raise AssertionError("refused capture attempted to resolve a secret")

    def context(*, binding):
        return _build_execution_context_from_binding(
            binding=binding, secret_resolver=forbidden
        )

    monkeypatch.setattr(
        payment_capability, "_build_execution_context_from_binding", context
    )
    assert (
        payment_capability.capture_transaction_record(db_session, query=query).refusal
        is reason
    )


def test_cross_installation_revision_refuses_before_runtime_or_secrets(
    db_session, monkeypatch
):
    query = _query(db_session)
    binding = db_session.get(IntegrationCapabilityBinding, query.capability_binding_id)
    other = enable_payment_provider(db_session, "flutterwave")[
        PAYMENT_RECONCILE_CAPABILITY
    ]
    foreign_revision = other.installation.current_config_revision
    db_session.execute(
        update(IntegrationInstallation)
        .where(IntegrationInstallation.id == binding.installation_id)
        .values(current_config_revision_id=foreign_revision.id)
        .execution_options(synchronize_session=False)
    )
    claim = replace(
        query.asserted_merchant_namespace,
        config_revision_id=foreign_revision.id,
        config_digest=foreign_revision.config_digest,
    )
    query = replace(
        query,
        config_revision_id=foreign_revision.id,
        config_digest=foreign_revision.config_digest,
        asserted_merchant_namespace=claim,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "cross-installation capture reached runtime or write boundary"
        )

    monkeypatch.setattr(
        payment_capability, "_build_execution_context_from_binding", forbidden
    )
    for method in ("add", "add_all", "delete", "flush", "commit", "rollback"):
        monkeypatch.setattr(db_session, method, forbidden)
    outcome = payment_capability.capture_transaction_record(db_session, query=query)
    assert outcome.refusal is CaptureRefusal.UNBOUND
    assert outcome.record is None


@pytest.mark.parametrize("boundary", ["lookup", "runtime_context"])
@pytest.mark.parametrize("error_type", [SQLAlchemyError, PendingRollbackError])
def test_capture_database_exceptions_propagate(
    db_session, monkeypatch, boundary, error_type
):
    query = _query(db_session)
    failure = error_type("database read failed")

    def fail(*args, **kwargs):
        raise failure

    if boundary == "lookup":
        monkeypatch.setattr(db_session, "execute", fail)
        monkeypatch.setattr(
            payment_capability, "_build_execution_context_from_binding", fail
        )
    else:
        monkeypatch.setattr(
            payment_capability, "_build_execution_context_from_binding", fail
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("database failure attempted a transaction side effect")

    for method in ("flush", "commit", "rollback", "add", "delete"):
        monkeypatch.setattr(db_session, method, forbidden)
    with pytest.raises(SQLAlchemyError) as caught:
        payment_capability.capture_transaction_record(db_session, query=query)
    assert caught.value is failure


@pytest.mark.parametrize(
    "field,in_place",
    [
        ("config_json", False),
        ("secret_refs", False),
        ("config_json", True),
        ("secret_refs", True),
    ],
)
def test_dirty_configuration_refuses_without_overwriting_caller(
    db_session, monkeypatch, field, in_place
):
    query = _query(db_session)
    revision = db_session.get(IntegrationConfigRevision, query.config_revision_id)
    replacement = (
        {"base_url": "https://modified.example.test"}
        if field == "config_json"
        else {"gateway_credentials": "env://MODIFIED_TEST_ONLY"}
    )
    if in_place:
        getattr(revision, field).update(replacement)
    else:
        setattr(revision, field, replacement)
    retained = dict(getattr(revision, field))

    def forbidden(*args, **kwargs):
        raise AssertionError("dirty configuration reached materialization or a write")

    monkeypatch.setattr(
        payment_capability, "_build_execution_context_from_binding", forbidden
    )
    for method in ("flush", "commit", "rollback", "add", "delete"):
        monkeypatch.setattr(db_session, method, forbidden)
    outcome = payment_capability.capture_transaction_record(db_session, query=query)
    assert outcome.refusal is CaptureRefusal.STALE_BINDING
    assert getattr(revision, field) == retained


def test_persisted_configuration_digest_mismatch_refuses_before_materialization(
    db_session, monkeypatch
):
    query = _query(db_session)
    db_session.execute(
        update(IntegrationConfigRevision)
        .where(IntegrationConfigRevision.id == query.config_revision_id)
        .values(config_json={"base_url": "https://changed.example.test"})
        .execution_options(synchronize_session=False)
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid persisted digest reached materialization")

    monkeypatch.setattr(
        payment_capability, "_build_execution_context_from_binding", forbidden
    )
    assert (
        payment_capability.capture_transaction_record(db_session, query=query).refusal
        is CaptureRefusal.STALE_BINDING
    )


@pytest.mark.parametrize("pin", ["old", "fresh"])
def test_capture_uses_fresh_scalar_pointer_and_preserves_stale_identity(
    db_session, monkeypatch, pin
):
    original = _query(db_session)
    binding = db_session.get(
        IntegrationCapabilityBinding, original.capability_binding_id
    )
    installation = binding.installation
    old_revision = installation.current_config_revision
    fresh = installations.create_config_revision(
        db_session,
        installation_id=installation.id,
        config={
            "base_url": "https://fresh.example.test",
            "timeout_seconds": 5,
            "default_currency": "NGN",
        },
        secret_refs={
            "gateway_credentials": "env://FRESH_TEST_ONLY",
            "public_key": "env://PUBLIC_TEST_ONLY",
        },
    )
    installations.validate_static(db_session, installation_id=installation.id)
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
    )
    db_session.flush()
    # Plant a clean stale ORM identity while the persisted pointer stays fresh.
    set_committed_value(installation, "current_config_revision_id", old_revision.id)
    set_committed_value(installation, "current_config_revision", old_revision)
    query = original
    if pin == "fresh":
        claim = replace(
            original.asserted_merchant_namespace,
            config_revision_id=fresh.id,
            config_digest=fresh.config_digest,
        )
        query = replace(
            original,
            config_revision_id=fresh.id,
            config_digest=fresh.config_digest,
            asserted_merchant_namespace=claim,
        )
    client = _Client(_body())
    references: list[str | None] = []

    def secret(reference: str | None) -> str:
        references.append(reference)
        return "test-only-secret"

    def context(*, binding):
        return _build_execution_context_from_binding(
            binding=binding,
            runner_override=PaymentGatewayRunner("paystack", client_override=client),
            secret_resolver=secret,
        )

    monkeypatch.setattr(
        payment_capability, "_build_execution_context_from_binding", context
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("fresh snapshot attempted database or caller mutation")

    for method in ("flush", "commit", "rollback", "add", "delete", "refresh"):
        monkeypatch.setattr(db_session, method, forbidden)
    outcome = payment_capability.capture_transaction_record(db_session, query=query)
    assert installation.current_config_revision_id == old_revision.id
    assert installation.current_config_revision is old_revision
    if pin == "old":
        assert outcome.refusal is CaptureRefusal.STALE_BINDING
        assert not client.calls and not references
    else:
        assert outcome.refusal is None
        assert outcome.provenance.config_revision_id == fresh.id
        assert outcome.provenance.config_digest == fresh.config_digest
        assert client.calls[0]["url"].startswith("https://fresh.example.test/")
        assert set(references) == {"env://FRESH_TEST_ONLY", "env://PUBLIC_TEST_ONLY"}


def test_existing_runtime_builder_preserves_supplied_options_and_binding(
    db_session, monkeypatch
):
    query = _query(db_session)
    runner = PaymentGatewayRunner("paystack", client_override=_Client(_body()))
    observed: list[IntegrationCapabilityBinding] = []

    def build(*, binding, **kwargs):
        observed.append(binding)
        return _build_execution_context_from_binding(binding=binding, **kwargs)

    monkeypatch.setattr(
        runtime_execution, "_build_execution_context_from_binding", build
    )
    context = build_execution_context(
        db_session,
        capability_binding_id=query.capability_binding_id,
        runner_override=runner,
        secret_resolver=lambda reference: "test-only-secret",
    )
    assert observed == [
        db_session.get(IntegrationCapabilityBinding, query.capability_binding_id)
    ]
    assert context.binding is observed[0]
    assert context.runner is runner
    assert (
        context.config == observed[0].installation.current_config_revision.config_json
    )
    assert context.secret_material == {
        "gateway_credentials": "test-only-secret",
        "public_key": "test-only-secret",
    }


def test_flutterwave_capture_preserves_decimal_json_lexeme_and_absent_fee():
    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.params["tx_ref"] == "flw-ref"
        return httpx.Response(
            200,
            content=b'{"status":"success","data":{"id":321,"tx_ref":"flw-ref","status":"successful","amount":100.12345678901234567890123456789,"currency":"NGN","created_at":"2024-01-01T10:00:00Z"}}',
        )

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = PaymentGatewayRunner("flutterwave", client_override=client).execute(
            _envelope(
                "verify",
                {"reference": "flw-ref", "capture_record": True},
                capability_id=PAYMENT_RECONCILE_CAPABILITY,
            ),
            config={"base_url": "https://gateway.example.test"},
            secret_material={"gateway_credentials": "test"},
        )
    record = _decode_capture_record(result.output)
    assert record.amount == Decimal("100.12345678901234567890123456789")
    assert record.envelope_status == "success"
    assert record.transaction_status == "successful"
    assert record.fee is None
    assert record.provider_paid_at is None


def test_asserted_namespace_cannot_retain_email_identity():
    with pytest.raises(PaymentCaptureError):
        AssertedMerchantNamespace(
            namespace="merchant@example.test",
            assertion_reference=uuid4(),
            installation_id=uuid4(),
            config_revision_id=uuid4(),
            config_digest="a" * 64,
        )


def test_capture_query_rejects_inexact_money():
    with pytest.raises(PaymentCaptureError):
        PaymentCaptureQuery(
            provider=CaptureProvider.PAYSTACK,
            reference="ref",
            installation_id=uuid4(),
            capability_binding_id=uuid4(),
            config_revision_id=uuid4(),
            config_digest="a" * 64,
            expected_amount=1.1,
            expected_currency="NGN",
        )
