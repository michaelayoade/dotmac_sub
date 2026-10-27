"""Guard attribution-only reads and preserve the existing Lead decision owner."""

import ast
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]


class WebsiteHandoffBoundaryTests(TestCase):
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
