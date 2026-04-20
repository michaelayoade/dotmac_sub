"""Application-facing adapter for live OLT profile data."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.adapters import adapter_registry


class OltProfileAdapter:
    """Keep OLT profile UI reads behind the operational OLT boundary."""

    name = "olt_profile"

    def line_profiles_context(self, db: Session, olt_id: str) -> dict[str, Any]:
        from app.models.network import OLTDevice
        from app.services.network import olt_ssh_profiles

        olt = db.get(OLTDevice, olt_id)
        if not olt:
            return {
                "error": "OLT not found",
                "line_profiles": [],
                "service_profiles": [],
            }

        context: dict[str, Any] = {
            "olt": olt,
            "line_profiles": [],
            "service_profiles": [],
            "error": None,
        }

        ok, msg, profiles = olt_ssh_profiles.get_line_profiles(olt)
        if ok:
            context["line_profiles"] = profiles
        else:
            context["error"] = msg

        ok2, msg2, service_profiles = olt_ssh_profiles.get_service_profiles(olt)
        if ok2:
            context["service_profiles"] = service_profiles
        elif not context["error"]:
            context["error"] = msg2

        return context

    def tr069_profiles_context(self, db: Session, olt_id: str) -> dict[str, Any]:
        from app.models.network import OLTDevice
        from app.services.network import olt_ssh_profiles

        olt = db.get(OLTDevice, olt_id)
        if not olt:
            return {"error": "OLT not found", "tr069_profiles": []}

        context: dict[str, Any] = {
            "olt": olt,
            "tr069_profiles": [],
            "error": None,
        }

        ok, msg, profiles = olt_ssh_profiles.get_tr069_server_profiles(olt)
        if ok:
            context["tr069_profiles"] = profiles
        else:
            context["error"] = msg

        return context


olt_profile_adapter = OltProfileAdapter()
adapter_registry.register(olt_profile_adapter)
