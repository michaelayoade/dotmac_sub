"""Legacy bound ERP observations do not enroll genuine manual requests."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.field import material_requests as owner
from app.services.owner_commands import CommandContext


@pytest.mark.parametrize(
    ("system", "reference", "allowed"),
    [
        ("dotmac_erp", "erp-id", True),
        ("dotmac_erp", "MR-001", True),
        ("dotmac_erp", None, False),
        ("dotmac_erp", "another-request", False),
        ("other-provider", "erp-id", False),
        (None, None, False),
    ],
)
def test_manual_default_requires_an_existing_exact_erp_binding(
    monkeypatch, system, reference, allowed
):
    request = SimpleNamespace(
        fulfillment_channel="manual", support_system=system, support_reference=reference
    )
    command_id = uuid4()
    command = owner.ObserveErpMaterialStatus(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="test",
            scope="test",
            reason="legacy observation",
            idempotency_key=str(command_id),
        ),
        request_id=uuid4(),
        provider_request_id="erp-id",
        provider_request_number="MR-001",
        provider_status="PARTIALLY_ISSUED",
        observed_at=datetime.now(UTC),
    )
    applied = MagicMock()
    monkeypatch.setattr(owner, "_locked_request", lambda *_args: request)
    monkeypatch.setattr(owner, "_request_view", lambda row: row)
    monkeypatch.setattr(
        owner, "execute_owner_command", lambda _db, **kw: kw["operation"]()
    )
    monkeypatch.setattr(
        owner.field_material_requests, "apply_backoffice_outcome", applied
    )
    db = MagicMock()
    if allowed:
        assert owner.observe_erp_material_status(db, command) is request
        applied.assert_called_once()
        assert request.fulfillment_channel == "manual"
    else:
        with pytest.raises(owner.MaterialRequestError, match="manual"):
            owner.observe_erp_material_status(db, command)
        applied.assert_not_called()
