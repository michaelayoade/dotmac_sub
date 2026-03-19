"""Tests for ONT bulk operation Celery tasks (Phase 7)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.network.ont_action_common import ActionResult


class TestDispatchAction:
    """Test action routing in bulk task dispatcher."""

    def test_reboot_action(self):
        from app.tasks.ont_bulk import _dispatch_action

        db = MagicMock()
        with patch(
            "app.services.network.ont_actions.ont_actions.reboot",
            return_value=ActionResult(success=True, message="Rebooted"),
        ):
            result = _dispatch_action(db, "ont-1", "reboot", {})
            assert result.success is True

    def test_factory_reset_action(self):
        from app.tasks.ont_bulk import _dispatch_action

        db = MagicMock()
        with patch(
            "app.services.network.ont_actions.ont_actions.factory_reset",
            return_value=ActionResult(success=True, message="Reset"),
        ):
            result = _dispatch_action(db, "ont-1", "factory_reset", {})
            assert result.success is True

    def test_speed_update_action(self):
        from app.tasks.ont_bulk import _dispatch_action

        db = MagicMock()
        with patch(
            "app.services.network.ont_write.OntWriteService.update_speed_profile",
            return_value=ActionResult(success=True, message="Updated"),
        ):
            result = _dispatch_action(
                db,
                "ont-1",
                "speed_update",
                {"download_profile_id": "prof-1"},
            )
            assert result.success is True

    def test_catv_toggle_action(self):
        from app.tasks.ont_bulk import _dispatch_action

        db = MagicMock()
        with patch(
            "app.services.network.ont_features.OntFeatureService.toggle_catv",
            return_value=ActionResult(success=True, message="Toggled"),
        ):
            result = _dispatch_action(
                db, "ont-1", "catv_toggle", {"enabled": True}
            )
            assert result.success is True

    def test_wifi_update_action(self):
        from app.tasks.ont_bulk import _dispatch_action

        db = MagicMock()
        with patch(
            "app.services.network.ont_features.OntFeatureService.set_wifi_config",
            return_value=ActionResult(success=True, message="Updated"),
        ):
            result = _dispatch_action(
                db, "ont-1", "wifi_update", {"ssid": "NewSSID"}
            )
            assert result.success is True

    def test_voip_toggle_action(self):
        from app.tasks.ont_bulk import _dispatch_action

        db = MagicMock()
        with patch(
            "app.services.network.ont_features.OntFeatureService.toggle_voip",
            return_value=ActionResult(success=True, message="Toggled"),
        ):
            result = _dispatch_action(
                db, "ont-1", "voip_toggle", {"enabled": False}
            )
            assert result.success is True

    def test_unknown_action(self):
        from app.tasks.ont_bulk import _dispatch_action

        db = MagicMock()
        result = _dispatch_action(db, "ont-1", "nonexistent", {})
        assert result.success is False
        assert "Unknown action" in result.message
