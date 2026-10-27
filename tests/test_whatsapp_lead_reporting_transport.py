"""Database-free transport tests; HTTP is always mocked, never sent to Meta."""

from datetime import UTC, datetime, timedelta
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import httpx

from app.services.integrations.connectors import meta_social_runtime as transport
from app.services.integrations.registry import (
    connector_definition,
    supported_connector_definitions,
)
from app.services.integrations.runtime import (
    OperationEnvelope,
    OperationStatus,
    OperationTrigger,
)


class WhatsAppLeadReportingTransportTests(TestCase):
    def envelope(self, source: str = "system_generated") -> OperationEnvelope:
        definition = connector_definition("meta.capi")
        assert definition is not None
        return OperationEnvelope(
            operation_id=uuid4(),
            correlation_id="test-reporting",
            installation_id=uuid4(),
            capability_binding_id=uuid4(),
            capability_id=transport.META_WEBSITE_LEAD_CAPABILITY,
            connector_key=definition.key,
            connector_version=definition.version,
            manifest_digest=definition.digest,
            config_revision_id=uuid4(),
            trigger=OperationTrigger.event,
            idempotency_key="test-stable-lead",
            deadline_at=datetime.now(UTC) + timedelta(seconds=30),
            payload={
                "action": "send_website_lead",
                "params": {
                    "event_id": "stable-lead-id",
                    "event_time": 1793000000,
                    "action_source": source,
                    "event_source_url": "https://fiber.dotmac.ng/coverage/",
                    "user_data": {"ph": ["a" * 64]},
                },
            },
        )

    def test_reporting_is_explicitly_disabled_by_default(self) -> None:
        with (
            patch.object(transport, "require"),
            patch.object(transport.httpx, "post") as post,
        ):
            result = transport.MetaCapiRunner().execute(
                self.envelope(),
                config={"pixel_id": "410389919883152", "api_version": "v26.0"},
                secret_material={"access_token": "test-only-token"},
            )
        self.assertEqual(result.status, OperationStatus.rejected)
        self.assertEqual(result.error_code, "whatsapp_reporting_disabled")
        post.assert_not_called()

    def test_crm_lead_uses_actual_source_and_minimized_contact_hash(self) -> None:
        accepted = httpx.Response(
            200,
            json={"events_received": 1},
            request=httpx.Request("POST", "https://graph.facebook.com"),
        )
        with (
            patch.object(transport, "require"),
            patch.object(transport.httpx, "post", return_value=accepted) as post,
        ):
            result = transport.MetaCapiRunner().execute(
                self.envelope(),
                config={
                    "pixel_id": "410389919883152",
                    "api_version": "v26.0",
                    "report_whatsapp_leads": True,
                },
                secret_material={"access_token": "test-only-token"},
            )
        self.assertEqual(result.status, OperationStatus.succeeded)
        event = post.call_args.kwargs["json"]["data"][0]
        self.assertEqual(event["event_name"], "Lead")
        self.assertEqual(event["event_id"], "stable-lead-id")
        self.assertEqual(event["action_source"], "system_generated")
        self.assertNotIn("event_source_url", event)
        self.assertEqual(event["user_data"], {"ph": ["a" * 64]})

    def test_legacy_website_reporting_retains_website_contract(self) -> None:
        accepted = httpx.Response(
            200,
            json={"events_received": 1},
            request=httpx.Request("POST", "https://graph.facebook.com"),
        )
        with (
            patch.object(transport, "require"),
            patch.object(transport.httpx, "post", return_value=accepted) as post,
        ):
            result = transport.MetaCapiRunner().execute(
                self.envelope("website"),
                config={"pixel_id": "410389919883152", "api_version": "v26.0"},
                secret_material={"access_token": "test-only-token"},
            )
        self.assertEqual(result.status, OperationStatus.succeeded)
        event = post.call_args.kwargs["json"]["data"][0]
        self.assertEqual(event["action_source"], "website")
        self.assertEqual(event["event_source_url"], "https://fiber.dotmac.ng/coverage/")

    def test_old_manifest_pin_is_preserved_and_new_flag_defaults_off(self) -> None:
        versions = {
            item.version: item
            for item in supported_connector_definitions()
            if item.key == "meta.capi"
        }
        self.assertEqual(
            versions["1.0.0"].digest,
            "4ba859b88a33bdca0adbf1711f35f5289e7a67393aaf7fb1fa74d5016b816d98",
        )
        self.assertFalse(
            versions["1.1.0"].config_schema["properties"]["report_whatsapp_leads"][
                "default"
            ]
        )
