"""Guard attribution-only reads and preserve the existing Lead decision owner."""

import ast
import runpy
from pathlib import Path
from unittest import TestCase

from app.services.sot_manifest import contract_validation_errors

ROOT = Path(__file__).resolve().parents[2]


class WebsiteHandoffBoundaryTests(TestCase):
    def test_sales_delegates_clearance_interpretation_to_the_identity_owner(
        self,
    ) -> None:
        tree = ast.parse(
            (ROOT / "app/services/sales/lead_intake.py").read_text(encoding="utf-8")
        )
        eligibility = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_manual_invitation_eligibility"
        )
        source = ast.unparse(eligibility)
        self.assertIn(
            "team_inbox_prospect_resolution.lead_intake_resolution_eligibility", source
        )
        self.assertNotIn("reviewed_no_match", source)

    def test_lifecycle_contract_is_structurally_complete(self) -> None:
        # Execute the real declaration shard, without importing the full runtime
        # registry. This verifies its shape, not the complete dependency graph.
        declaration = runpy.run_path(
            str(ROOT / "app/services/sot_registry/domains/sales_referrals/lifecycle.py")
        )
        service = next(
            item
            for item in declaration["SERVICES"]
            if item.name == "sales.lead_lifecycle"
        )
        self.assertEqual(
            contract_validation_errors(
                service,
                service_names={service.name, *service.depends_on, "events.dispatcher"},
            ),
            (),
        )
        self.assertIn("app.services.lead_origin_values", service.implementation_modules)

    def test_attribution_resolver_is_read_only(self) -> None:
        tree = ast.parse(
            (ROOT / "app/services/website_handoff_attribution.py").read_text(
                encoding="utf-8"
            )
        )
        forbidden = {
            "commit",
            "rollback",
            "flush",
            "add",
            "delete",
            "create_party_lead",
            "emit_event",
        }
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(calls & forbidden)

    def test_handoff_does_not_enter_immediate_lead_capture(self) -> None:
        source = (ROOT / "app/services/team_inbox_receive.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("should_capture_lead = not payload.is_whatsapp_handoff", source)

    def test_existing_lead_decision_is_not_in_attribution_policy(self) -> None:
        source = (ROOT / "app/services/website_handoff_values.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("create_party_lead", source)
        self.assertNotIn("AiIntakeConfig", source)

    def test_lifecycle_is_the_only_acquisition_composition_caller(self) -> None:
        for relative in (
            "sales/lead_intake.py",
            "sales/lead_authoring.py",
            "inbox_lead_actions.py",
            "integrations/meta_capi_lead.py",
        ):
            source = (ROOT / "app/services" / relative).read_text(encoding="utf-8")
            self.assertNotIn("resolve_for_message(", source)
            self.assertNotIn("resolve_for_conversation(", source)
            self.assertNotIn("compose_inbox_acquisition(", source)

    def test_all_inbox_creation_paths_supply_context_and_origin_event_identity(
        self,
    ) -> None:
        for relative in (
            "sales/lead_intake.py",
            "sales/lead_authoring.py",
            "inbox_lead_actions.py",
        ):
            source = (ROOT / "app/services" / relative).read_text(encoding="utf-8")
            self.assertIn("inbox_acquisition=", source)
            self.assertIn('"origin_capture_id": str(lead.origin_capture.id)', source)
