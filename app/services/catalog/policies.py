"""Policy management services.

Provides services for PolicySets and PolicyDunningSteps.
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import (
    PolicyDunningStep,
    PolicySet,
    ProrationPolicy,
    RefundPolicy,
    SuspensionAction,
)
from app.models.domain_settings import SettingDomain
from app.schemas.catalog import (
    PolicyDunningStepCreate,
    PolicyDunningStepUpdate,
    PolicySetCreate,
    PolicySetUpdate,
)
from app.services import settings_spec
from app.services.common import apply_ordering, apply_pagination, validate_enum
from app.services.response import ListResponseMixin


class PolicySets(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PolicySetCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "proration_policy" not in fields_set:
            default_proration = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_proration_policy"
            )
            if default_proration:
                data["proration_policy"] = validate_enum(
                    default_proration, ProrationPolicy, "proration_policy"
                )
        if "downgrade_policy" not in fields_set:
            default_downgrade = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_downgrade_policy"
            )
            if default_downgrade:
                data["downgrade_policy"] = validate_enum(
                    default_downgrade, ProrationPolicy, "downgrade_policy"
                )
        if "suspension_action" not in fields_set:
            default_suspension = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_suspension_action"
            )
            if default_suspension:
                data["suspension_action"] = validate_enum(
                    default_suspension, SuspensionAction, "suspension_action"
                )
        if "refund_policy" not in fields_set:
            default_refund = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_refund_policy"
            )
            if default_refund:
                data["refund_policy"] = validate_enum(
                    default_refund, RefundPolicy, "refund_policy"
                )
        policy = PolicySet(**data)
        db.add(policy)
        db.commit()
        db.refresh(policy)
        return policy

    @staticmethod
    def get(db: Session, policy_id: str):
        policy = db.get(PolicySet, policy_id)
        if not policy:
            raise HTTPException(status_code=404, detail="Policy set not found")
        return policy

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PolicySet)
        if is_active is None:
            query = query.filter(PolicySet.is_active.is_(True))
        else:
            query = query.filter(PolicySet.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PolicySet.created_at, "name": PolicySet.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, policy_id: str, payload: PolicySetUpdate):
        policy = db.get(PolicySet, policy_id)
        if not policy:
            raise HTTPException(status_code=404, detail="Policy set not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(policy, key, value)
        db.commit()
        db.refresh(policy)
        return policy

    @staticmethod
    def delete(db: Session, policy_id: str):
        policy = db.get(PolicySet, policy_id)
        if not policy:
            raise HTTPException(status_code=404, detail="Policy set not found")
        policy.is_active = False
        db.commit()


class PolicyDunningSteps(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PolicyDunningStepCreate):
        policy = db.get(PolicySet, payload.policy_set_id)
        if not policy:
            raise HTTPException(status_code=404, detail="Policy set not found")
        step = PolicyDunningStep(**payload.model_dump())
        db.add(step)
        db.commit()
        db.refresh(step)
        return step

    @staticmethod
    def get(db: Session, step_id: str):
        step = db.get(PolicyDunningStep, step_id)
        if not step:
            raise HTTPException(status_code=404, detail="Policy dunning step not found")
        return step

    @staticmethod
    def list(
        db: Session,
        policy_set_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PolicyDunningStep)
        if policy_set_id:
            query = query.filter(PolicyDunningStep.policy_set_id == policy_set_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"day_offset": PolicyDunningStep.day_offset, "action": PolicyDunningStep.action},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, step_id: str, payload: PolicyDunningStepUpdate):
        step = db.get(PolicyDunningStep, step_id)
        if not step:
            raise HTTPException(status_code=404, detail="Policy dunning step not found")
        data = payload.model_dump(exclude_unset=True)
        if "policy_set_id" in data:
            policy = db.get(PolicySet, data["policy_set_id"])
            if not policy:
                raise HTTPException(status_code=404, detail="Policy set not found")
        for key, value in data.items():
            setattr(step, key, value)
        db.commit()
        db.refresh(step)
        return step

    @staticmethod
    def delete(db: Session, step_id: str):
        step = db.get(PolicyDunningStep, step_id)
        if not step:
            raise HTTPException(status_code=404, detail="Policy dunning step not found")
        db.delete(step)
        db.commit()
