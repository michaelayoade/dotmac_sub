"""Dry-run ONT provisioning command preview."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OntProvisioningProfile
from app.services.network.olt_command_gen import (
    HuaweiCommandGenerator,
    OntProvisioningContext,
    build_spec_from_profile,
)
from app.services.network.ont_provisioning.context import resolve_olt_context
from app.services.network.ont_provisioning.credentials import mask_credentials


def preview_commands(
    db: Session,
    ont_id: str,
    profile_id: str,
    *,
    tr069_olt_profile_id: int | None = None,
) -> dict[str, Any]:
    """Generate provisioning commands without executing them."""
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return {"success": False, "message": err, "command_sets": []}

    profile = db.get(OntProvisioningProfile, profile_id)
    if not profile:
        return {"success": False, "message": "Profile not found", "command_sets": []}

    fsp_parts = ctx.fsp.split("/")
    if len(fsp_parts) < 3:
        return {
            "success": False,
            "message": f"FSP '{ctx.fsp}' needs 3 segments (frame/slot/port)",
            "command_sets": [],
        }
    prov_ctx = OntProvisioningContext(
        frame=int(fsp_parts[0]),
        slot=int(fsp_parts[1]),
        port=int(fsp_parts[2]),
        ont_id=ctx.olt_ont_id,
        olt_name=ctx.olt.name,
    )

    spec = build_spec_from_profile(
        profile, prov_ctx, tr069_profile_id=tr069_olt_profile_id
    )
    command_sets = HuaweiCommandGenerator.generate_full_provisioning(spec, prov_ctx)

    return {
        "success": True,
        "message": f"Generated {sum(len(item.commands) for item in command_sets)} command(s)",
        "command_sets": [
            {
                "step": item.step,
                "commands": [mask_credentials(command) for command in item.commands],
                "description": item.description,
            }
            for item in command_sets
        ],
    }
