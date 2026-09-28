"""Confirmed fiber website Lead delivery to Meta Conversions API."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest
from fastapi import HTTPException

from app.models.integration_platform import IntegrationDelivery
from app.models.sales import Lead, LeadOriginCapture
from app.services.events.types import Event, EventType
from app.services.integrations import meta_capi_lead
from app.services.integrations.connectors import meta_social_runtime as meta_capi
from app.services.integrations.registry import connector_definition
from app.services.integrations.runtime import (
    OperationEnvelope,
    OperationStatus,
    OperationTrigger,
)
from app.services.owner_commands import CommandContext
from tests.integration_platform_helpers import enable_capability
from tests.test_fiber_inquiry_webhook import _binding, _coverage_payload, _post


def _enable_capi(db_session, monkeypatch, *, token: str | None = None):
    token = token or "test-meta-token"  # nosec B105 -- non-production fixture
    monkeypatch.setenv("META_CAPI_TEST_ACCESS_TOKEN", token)
    return enable_capability(
        db_session,
        connector_key=meta_capi.META_CAPI_CONNECTOR_KEY,
        capability_id=meta_capi.META_WEBSITE_LEAD_CAPABILITY,
        config={
            "pixel_id": meta_capi.DEFAULT_PIXEL_ID,
            "api_version": meta_capi.DEFAULT_API_VERSION,
            "timeout_seconds": 2,
            "max_attempts": 3,
        },
        secret_refs={"access_token": "env://META_CAPI_TEST_ACCESS_TOKEN"},
    )


def _delivery(db_session) -> IntegrationDelivery:
    return (
        db_session.query(IntegrationDelivery)
        .filter(IntegrationDelivery.event_type == "meta.capi.website_lead")
        .one()
    )


def _deliver(db_session, delivery: IntegrationDelivery) -> IntegrationDelivery:
    delivery_id = delivery.id
    attempt_count = delivery.attempt_count
    db_session.rollback()
    return meta_capi_lead.deliver_lead(
        db_session,
        meta_capi_lead.DeliverMetaCapiLeadCommand(
            context=CommandContext.system(
                actor="test",
                scope=meta_capi_lead.META_CAPI_DELIVERY_SCOPE,
                reason="Test website Lead delivery",
                idempotency_key=f"test-meta-capi:{delivery_id}:{attempt_count}",
            ),
            delivery_id=delivery_id,
        ),
    )


def _accepted_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"events_received": 1, "fbtrace_id": "safe-trace"},
        request=httpx.Request("POST", "https://graph.facebook.com"),
    )


def test_successful_coverage_inquiry_queues_exactly_one_minimized_lead(
    db_session, monkeypatch
) -> None:
    fiber_binding = _binding(db_session, monkeypatch)
    _enable_capi(db_session, monkeypatch)
    monkeypatch.setattr(meta_capi_lead, "queue_delivery", lambda _result: None)
    payload = _coverage_payload(coordinates=False)

    response = _post(db_session, fiber_binding.id, payload, "fiber-meta-capi-1")
    replay = _post(db_session, fiber_binding.id, payload, "fiber-meta-capi-1")

    assert response.replayed is False
    assert replay.replayed is True
    assert db_session.query(Lead).count() == 1
    assert (
        db_session.query(IntegrationDelivery)
        .filter(IntegrationDelivery.event_type == "meta.capi.website_lead")
        .count()
        == 1
    )
    delivery = _delivery(db_session)
    origin = db_session.get(
        LeadOriginCapture, UUID(delivery.payload_json["origin_capture_id"])
    )
    assert origin is not None
    assert origin.external_click_id == "gclid-example"
    assert origin.utm_campaign == "abuja_home"
    assert (
        delivery.payload_json["event_source_url"] == "https://fiber.dotmac.ng/coverage/"
    )
    assert set(delivery.payload_json["user_data"]) == {"em", "ph"}
    serialized = str(delivery.payload_json)
    assert "coverage@example.com" not in serialized
    assert "+2348031234567" not in serialized
    assert "gclid-example" not in serialized
    expected = str(
        uuid5(
            NAMESPACE_URL,
            f"dotmac:meta-capi:website-lead:{origin.id}",
        )
    )
    assert delivery.payload_json["event_id"] == expected


def test_disabled_capi_does_not_break_or_queue_customer_inquiry(
    db_session, monkeypatch
) -> None:
    fiber_binding = _binding(db_session, monkeypatch)

    response = _post(
        db_session,
        fiber_binding.id,
        _coverage_payload(coordinates=False),
        "fiber-meta-disabled",
    )

    assert response.replayed is False
    assert db_session.query(Lead).count() == 1
    assert (
        db_session.query(IntegrationDelivery)
        .filter(IntegrationDelivery.event_type == "meta.capi.website_lead")
        .count()
        == 0
    )


def test_non_coverage_and_non_new_connection_inquiries_are_ineligible(
    db_session, monkeypatch
) -> None:
    fiber_binding = _binding(db_session, monkeypatch)
    _enable_capi(db_session, monkeypatch)
    monkeypatch.setattr(meta_capi_lead, "queue_delivery", lambda _result: None)
    contact = {
        "form_version": "fiber-contact-v1",
        "full_name": "Support Contact",
        "phone": "08031234567",
        "email": "support@example.com",
        "interest": "technical_support",
        "submitted_at": "2026-09-19T12:30:00+01:00",
    }

    _post(db_session, fiber_binding.id, contact, "fiber-meta-support")

    assert (
        db_session.query(IntegrationDelivery)
        .filter(IntegrationDelivery.event_type == "meta.capi.website_lead")
        .count()
        == 0
    )


def test_invalid_inquiry_and_persistence_failure_do_not_queue(
    db_session, monkeypatch
) -> None:
    fiber_binding = _binding(db_session, monkeypatch)
    _enable_capi(db_session, monkeypatch)
    invalid = _coverage_payload(coordinates=False)
    invalid["location"].pop("address")

    with pytest.raises(HTTPException) as invalid_exc:
        _post(db_session, fiber_binding.id, invalid, "fiber-meta-invalid")
    assert invalid_exc.value.status_code == 422
    assert (
        db_session.query(IntegrationDelivery)
        .filter(IntegrationDelivery.event_type == "meta.capi.website_lead")
        .count()
        == 0
    )

    monkeypatch.setattr(
        "app.services.team_inbox_fiber_receive.capture_fiber_prospect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("persistence failed")
        ),
    )
    with pytest.raises(HTTPException) as persistence_exc:
        _post(
            db_session,
            fiber_binding.id,
            _coverage_payload(coordinates=False),
            "fiber-meta-persistence-failed",
        )
    assert persistence_exc.value.status_code == 503
    assert (
        db_session.query(IntegrationDelivery)
        .filter(IntegrationDelivery.event_type == "meta.capi.website_lead")
        .count()
        == 0
    )


def test_normalization_and_hashing_match_meta_contract() -> None:
    assert meta_capi_lead.normalize_and_hash_email("  Person@Example.COM ") == (
        hashlib.sha256(b"person@example.com").hexdigest()
    )
    assert meta_capi_lead.normalize_and_hash_phone("(0803) 123-4567") == (
        hashlib.sha256(b"2348031234567").hexdigest()
    )


def test_success_records_safe_receipt_and_duplicate_worker_is_idempotent(
    db_session, monkeypatch
) -> None:
    fiber_binding = _binding(db_session, monkeypatch)
    _enable_capi(db_session, monkeypatch)
    monkeypatch.setattr(meta_capi_lead, "queue_delivery", lambda _result: None)
    _post(
        db_session,
        fiber_binding.id,
        _coverage_payload(coordinates=False),
        "fiber-meta-success",
    )
    delivery = _delivery(db_session)
    event_id = delivery.payload_json["event_id"]
    requests: list[dict] = []

    def accept(_url, *, json, headers, timeout):
        requests.append(json)
        assert headers["Authorization"] == "Bearer test-meta-token"
        return _accepted_response()

    monkeypatch.setattr(meta_capi.httpx, "post", accept)
    first = _deliver(db_session, delivery)
    second = _deliver(db_session, first)

    assert first.state == second.state == "delivered"
    assert len(requests) == 1
    assert requests[0]["data"][0]["event_name"] == "Lead"
    assert requests[0]["data"][0]["action_source"] == "website"
    assert requests[0]["data"][0]["event_id"] == event_id
    assert first.external_receipt_json == {
        "response_status": 200,
        "trace_id": "safe-trace",
        "events_received": 1,
    }


def test_event_replay_deduplicates_and_preserves_event_id(
    db_session, monkeypatch
) -> None:
    fiber_binding = _binding(db_session, monkeypatch)
    _enable_capi(db_session, monkeypatch)
    monkeypatch.setattr(meta_capi_lead, "queue_delivery", lambda _result: None)
    _post(
        db_session,
        fiber_binding.id,
        _coverage_payload(coordinates=False),
        "fiber-meta-event-replay",
    )
    delivery = _delivery(db_session)
    origin_id = delivery.payload_json["origin_capture_id"]
    event_id = delivery.payload_json["event_id"]
    db_session.commit()

    result = meta_capi_lead.stage_lead(
        db_session,
        meta_capi_lead.StageMetaCapiLeadCommand(
            context=CommandContext.system(
                actor="test",
                scope=meta_capi_lead.META_CAPI_STAGE_SCOPE,
                reason="Replay committed lead event",
                idempotency_key="test-meta-capi-stage-replay",
            ),
            event=Event(
                event_type=EventType.lead_created,
                payload={"origin_capture_id": origin_id},
            ),
        ),
    )

    assert result.outcome is meta_capi_lead.StageOutcome.deduplicated
    assert result.delivery_id == delivery.id
    assert result.event_id == event_id
    db_session.refresh(delivery)
    assert delivery.external_receipt_json["deduplicated_count"] == 1


def test_transient_5xx_timeout_and_rate_limit_are_retryable() -> None:
    manifest = connector_definition(meta_capi.META_CAPI_CONNECTOR_KEY)
    assert manifest is not None
    envelope = OperationEnvelope(
        operation_id=uuid4(),
        correlation_id="test-capi",
        installation_id=uuid4(),
        capability_binding_id=uuid4(),
        capability_id=meta_capi.META_WEBSITE_LEAD_CAPABILITY,
        connector_key=manifest.key,
        connector_version=manifest.version,
        manifest_digest=manifest.digest,
        config_revision_id=uuid4(),
        trigger=OperationTrigger.event,
        idempotency_key="test-capi-event",
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
        payload={
            "action": "send_website_lead",
            "params": {
                "event_id": "stable-event",
                "event_time": 1_790_000_000,
                "event_source_url": "https://fiber.dotmac.ng/coverage/",
                "user_data": {"em": ["a" * 64]},
            },
        },
    )
    runner = meta_capi.MetaCapiRunner()
    config = {"pixel_id": meta_capi.DEFAULT_PIXEL_ID, "api_version": "v26.0"}
    secrets = {"access_token": "not-logged"}
    cases = (
        httpx.Response(
            503,
            json={"error": {"code": 2}, "fbtrace_id": "trace-503"},
            request=httpx.Request("POST", "https://graph.facebook.com"),
        ),
        httpx.Response(
            429,
            headers={"Retry-After": "17"},
            json={"error": {"code": 4}},
            request=httpx.Request("POST", "https://graph.facebook.com"),
        ),
    )
    for response in cases:
        original = meta_capi.httpx.post
        meta_capi.httpx.post = lambda *_args, _response=response, **_kwargs: _response
        try:
            result = runner.execute(envelope, config=config, secret_material=secrets)
        finally:
            meta_capi.httpx.post = original
        assert result.status is OperationStatus.retryable
    original = meta_capi.httpx.post
    meta_capi.httpx.post = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        httpx.ReadTimeout("timeout")
    )
    try:
        timed_out = runner.execute(envelope, config=config, secret_material=secrets)
    finally:
        meta_capi.httpx.post = original
    assert timed_out.status is OperationStatus.retryable
    assert timed_out.error_code == "network_timeout"


def test_permanent_validation_and_authentication_failures_are_not_retried(
    monkeypatch,
) -> None:
    manifest = connector_definition(meta_capi.META_CAPI_CONNECTOR_KEY)
    assert manifest is not None
    envelope = OperationEnvelope(
        operation_id=uuid4(),
        correlation_id="test-capi-reject",
        installation_id=uuid4(),
        capability_binding_id=uuid4(),
        capability_id=meta_capi.META_WEBSITE_LEAD_CAPABILITY,
        connector_key=manifest.key,
        connector_version=manifest.version,
        manifest_digest=manifest.digest,
        config_revision_id=uuid4(),
        trigger=OperationTrigger.event,
        idempotency_key="test-capi-reject",
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
        payload={
            "action": "send_website_lead",
            "params": {
                "event_id": "stable-event",
                "event_time": 1_790_000_000,
                "event_source_url": "https://fiber.dotmac.ng/",
                "user_data": {"ph": ["b" * 64]},
            },
        },
    )
    responses = (
        (400, {"error": {"code": 100}}, "validation_rejected"),
        (401, {"error": {"code": 190}}, "authentication_failed"),
    )
    for status, body, code in responses:
        monkeypatch.setattr(
            meta_capi.httpx,
            "post",
            lambda *_args, status=status, body=body, **_kwargs: httpx.Response(
                status,
                json=body,
                request=httpx.Request("POST", "https://graph.facebook.com"),
            ),
        )
        result = meta_capi.MetaCapiRunner().execute(
            envelope,
            config={"pixel_id": meta_capi.DEFAULT_PIXEL_ID, "api_version": "v26.0"},
            secret_material={"access_token": "not-logged"},
        )
        assert result.status is OperationStatus.rejected
        assert result.error_code == code


def test_missing_token_does_not_break_inquiry_and_dead_letters_delivery(
    db_session, monkeypatch
) -> None:
    fiber_binding = _binding(db_session, monkeypatch)
    _enable_capi(db_session, monkeypatch)
    monkeypatch.setattr(meta_capi_lead, "queue_delivery", lambda _result: None)
    _post(
        db_session,
        fiber_binding.id,
        _coverage_payload(coordinates=False),
        "fiber-meta-no-token",
    )
    delivery = _delivery(db_session)
    monkeypatch.delenv("META_CAPI_TEST_ACCESS_TOKEN")

    result = _deliver(db_session, delivery)

    assert result.state == "dead_letter"
    assert result.error_code == "configuration_unavailable"
    assert db_session.query(Lead).count() == 1


def test_logs_and_delivery_evidence_contain_no_raw_pii_or_token(
    db_session, monkeypatch, caplog
) -> None:
    raw_email = "private.person@example.com"
    raw_phone = "+2348098765432"
    raw_token = "very-secret-meta-token"
    fiber_binding = _binding(db_session, monkeypatch)
    _enable_capi(db_session, monkeypatch, token=raw_token)
    monkeypatch.setattr(meta_capi_lead, "queue_delivery", lambda _result: None)
    caplog.set_level(logging.INFO)
    _post(
        db_session,
        fiber_binding.id,
        _coverage_payload(email=raw_email, phone=raw_phone, coordinates=False),
        "fiber-meta-private",
    )
    delivery = _delivery(db_session)
    monkeypatch.setattr(
        meta_capi.httpx, "post", lambda *_args, **_kwargs: _accepted_response()
    )

    _deliver(db_session, delivery)

    evidence = f"{caplog.text} {delivery.payload_json} {delivery.external_receipt_json}"
    assert raw_email not in evidence
    assert raw_phone not in evidence
    assert raw_token not in evidence
