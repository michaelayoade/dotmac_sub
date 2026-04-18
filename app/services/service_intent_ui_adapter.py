"""UI-facing service intent adapter.

Keeps admin/customer web services from importing network intent helpers
directly. Network modules still own the actual ONT interpretation.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


class ServiceIntentUiAdapter:
    def build_ont_service_intent(
        self,
        ont: object,
        *,
        db: Session | None = None,
        subscriber_info: dict[str, object] | None = None,
        ont_plan: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        from app.services.network.ont_service_intent import build_service_intent

        return build_service_intent(
            ont,
            db=db,
            subscriber_info=subscriber_info,
            ont_plan=ont_plan,
        )

    def load_ont_plan_for_ont(self, db: Session, *, ont_id: str) -> dict[str, Any]:
        from app.services.network.ont_service_intent import load_ont_plan_for_ont

        return load_ont_plan_for_ont(db, ont_id=ont_id)


service_intent_ui_adapter = ServiceIntentUiAdapter()

