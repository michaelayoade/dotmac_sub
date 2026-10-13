"""Lossless released Billing facts for dormant ERP transport and pure preview.

Parsing captured JSON proves structure only. It never authenticates a source or
admits a staging command. A trusted committed-Billing consumer remains pending.
ERP alone decides account mapping, recognition and document relationships.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from dotmac_billing import (
    AccountingAllocationEffectV1,
    AccountingEffectV1,
    AccountingFactV1,
    AppliedFxSnapshotV1,
    AppliedTaxSnapshotV1,
)
from dotmac_billing.contracts import ACCOUNTING_FACT_CONTRACT
from dotmac_kernel.cache import PlatformScope, TenantScope
from dotmac_kernel.money import Currency, Money
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from app.services.domain_errors import DomainError

EXPORT_CONTRACT = "billing.accounting.export.v1"
EffectKind = Literal[
    "invoice_issued",
    "credit_note_issued",
    "settlement_accepted",
    "allocation",
    "deallocation",
    "reallocation",
    "refund",
    "reversal",
]
AllocationKind = Literal[
    "allocation", "deallocation", "reallocation", "refund", "reversal"
]
NonEmpty = Annotated[str, Field(min_length=1, strict=True)]
Timestamp = Annotated[datetime, AwareDatetime]
Payload = dict[str, JsonValue]
_PAYLOAD = TypeAdapter(Payload)


class BillingAccountingFactError(DomainError):
    """Malformed transport evidence; never an accounting decision."""


def _invalid(message: str) -> BillingAccountingFactError:
    return BillingAccountingFactError(
        code="integration.dotmac_erp_billing_adapter.invalid_accounting_fact",
        message=message,
        retryable=False,
    )


def _decimal_text(value: Decimal) -> str:
    """Canonical exact digits without ambient Decimal-context arithmetic."""
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", "+0"} else rendered


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def exact_primitives(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("financial transport refuses inexact float values")
        if isinstance(value, str) and not value.strip():
            raise ValueError("empty or whitespace-only evidence is forbidden")
        return value


class _Scope(_Wire):
    plane: Literal["tenant", "platform"]
    tenant_id: UUID | None = None

    @model_validator(mode="after")
    def coherent_plane(self) -> _Scope:
        if (self.plane == "tenant") != (self.tenant_id is not None):
            raise ValueError("tenant plane requires tenant_id; platform forbids it")
        return self


class _Money(_Wire):
    amount: Decimal
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$", strict=True)]
    minor_units: Annotated[int, Field(ge=0, strict=True)]

    @model_validator(mode="after")
    def exact_amount(self) -> _Money:
        if not self.amount.is_finite():
            raise ValueError("money must be finite")
        if self.amount != self.amount.quantize(Decimal(1).scaleb(-self.minor_units)):
            raise ValueError("money must already agree with its explicit minor units")
        return self

    def money(self) -> Money:
        return Money(self.amount, Currency(self.currency, self.minor_units))


class _Effect(_Wire):
    lane: Literal["receivable", "available_credit", "prepaid_funding"]
    amount_delta: _Money


class _Allocation(_Wire):
    settlement_id: UUID
    document_id: UUID | None
    effect_kind: AllocationKind
    amount_delta: _Money
    offsets_allocation_id: UUID | None


class _Tax(_Wire):
    treatment_code: NonEmpty
    jurisdiction_code: NonEmpty
    policy_id: NonEmpty
    policy_version: NonEmpty
    rate: Decimal
    taxable_basis: _Money
    tax_amount: _Money

    @field_validator("rate")
    @classmethod
    def finite_rate(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("tax rate must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def tax_currency(self) -> _Tax:
        if (self.taxable_basis.currency, self.taxable_basis.minor_units) != (
            self.tax_amount.currency,
            self.tax_amount.minor_units,
        ):
            raise ValueError("tax basis and tax amount currencies must agree")
        return self

    @field_serializer("rate")
    def canonical_rate(self, value: Decimal) -> str:
        return _decimal_text(value)


class _Fx(_Wire):
    observation_id: NonEmpty
    observation_version: NonEmpty
    base_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$", strict=True)]
    quote_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$", strict=True)]
    rate: Decimal
    rate_purpose: NonEmpty
    observed_at: Timestamp
    effective_at: Timestamp
    rounding_policy: NonEmpty
    provenance: NonEmpty

    @field_validator("rate")
    @classmethod
    def positive_rate(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("FX rate must be finite and positive")
        return value

    @field_serializer("rate")
    def canonical_rate(self, value: Decimal) -> str:
        return _decimal_text(value)

    @field_validator("observed_at", "effective_at")
    @classmethod
    def canonical_instant(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class _Fact(_Wire):
    scope: _Scope
    source_system: Annotated[str, Field(min_length=1, strict=True)]
    fact_id: UUID
    fact_version: Annotated[int, Field(ge=1, strict=True)]
    billing_account_id: UUID
    posting_group_id: UUID
    source_ref: NonEmpty
    source_authority: Literal["internal", "provider_owned", "external_finance"]
    effect_kind: EffectKind
    occurred_at: Timestamp
    committed_at: Timestamp
    effects: tuple[_Effect, ...]
    allocations: tuple[_Allocation, ...]
    reverses_fact_id: UUID | None
    tax_snapshots: tuple[_Tax, ...]
    fx_snapshot: _Fx | None

    @field_validator("occurred_at", "committed_at")
    @classmethod
    def canonical_instant(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    def fact(self) -> AccountingFactV1:
        scope = (
            TenantScope(self.scope.tenant_id)
            if self.scope.tenant_id is not None
            else PlatformScope()
        )
        return AccountingFactV1(
            scope=scope,
            source_system=self.source_system,
            fact_id=self.fact_id,
            fact_version=self.fact_version,
            billing_account_id=self.billing_account_id,
            posting_group_id=self.posting_group_id,
            source_ref=self.source_ref,
            source_authority=self.source_authority,
            effect_kind=self.effect_kind,
            occurred_at=self.occurred_at,
            committed_at=self.committed_at,
            effects=tuple(
                AccountingEffectV1(row.lane, row.amount_delta.money())
                for row in self.effects
            ),
            allocations=tuple(
                AccountingAllocationEffectV1(
                    settlement_id=row.settlement_id,
                    document_id=row.document_id,
                    effect_kind=row.effect_kind,
                    amount_delta=row.amount_delta.money(),
                    offsets_allocation_id=row.offsets_allocation_id,
                )
                for row in self.allocations
            ),
            reverses_fact_id=self.reverses_fact_id,
            tax_snapshots=tuple(
                AppliedTaxSnapshotV1(
                    treatment_code=row.treatment_code,
                    jurisdiction_code=row.jurisdiction_code,
                    policy_id=row.policy_id,
                    policy_version=row.policy_version,
                    rate=row.rate,
                    taxable_basis=row.taxable_basis.money(),
                    tax_amount=row.tax_amount.money(),
                )
                for row in self.tax_snapshots
            ),
            fx_snapshot=(
                AppliedFxSnapshotV1(
                    observation_id=self.fx_snapshot.observation_id,
                    observation_version=self.fx_snapshot.observation_version,
                    base_currency=self.fx_snapshot.base_currency,
                    quote_currency=self.fx_snapshot.quote_currency,
                    rate=self.fx_snapshot.rate,
                    rate_purpose=self.fx_snapshot.rate_purpose,
                    observed_at=self.fx_snapshot.observed_at,
                    effective_at=self.fx_snapshot.effective_at,
                    rounding_policy=self.fx_snapshot.rounding_policy,
                    provenance=self.fx_snapshot.provenance,
                )
                if self.fx_snapshot
                else None
            ),
        )


def _money(value: Money) -> _Money:
    if not isinstance(value, Money):
        raise _invalid("Accounting money requires the released Money value object.")
    return _Money(
        amount=value.amount,
        currency=value.currency.code,
        minor_units=value.currency.minor_units,
    )


def _wire(fact: AccountingFactV1) -> _Fact:
    if isinstance(fact.scope, TenantScope):
        scope = _Scope(plane="tenant", tenant_id=fact.scope.tenant_id)
    elif isinstance(fact.scope, PlatformScope):
        scope = _Scope(plane="platform", tenant_id=None)
    else:
        raise _invalid("An explicit tenant or platform scope is required.")
    fx = fact.fx_snapshot
    return _Fact(
        scope=scope,
        source_system=fact.source_system,
        fact_id=fact.fact_id,
        fact_version=fact.fact_version,
        billing_account_id=fact.billing_account_id,
        posting_group_id=fact.posting_group_id,
        source_ref=fact.source_ref,
        source_authority=fact.source_authority,
        effect_kind=fact.effect_kind,
        occurred_at=fact.occurred_at,
        committed_at=fact.committed_at,
        effects=tuple(
            _Effect(lane=row.lane, amount_delta=_money(row.amount_delta))
            for row in fact.effects
        ),
        allocations=tuple(
            _Allocation(
                settlement_id=row.settlement_id,
                document_id=row.document_id,
                effect_kind=row.effect_kind,
                amount_delta=_money(row.amount_delta),
                offsets_allocation_id=row.offsets_allocation_id,
            )
            for row in fact.allocations
        ),
        reverses_fact_id=fact.reverses_fact_id,
        tax_snapshots=tuple(
            _Tax(
                treatment_code=row.treatment_code,
                jurisdiction_code=row.jurisdiction_code,
                policy_id=row.policy_id,
                policy_version=row.policy_version,
                rate=row.rate,
                taxable_basis=_money(row.taxable_basis),
                tax_amount=_money(row.tax_amount),
            )
            for row in fact.tax_snapshots
        ),
        fx_snapshot=_Fx(
            observation_id=fx.observation_id,
            observation_version=fx.observation_version,
            base_currency=fx.base_currency,
            quote_currency=fx.quote_currency,
            rate=fx.rate,
            rate_purpose=fx.rate_purpose,
            observed_at=fx.observed_at,
            effective_at=fx.effective_at,
            rounding_policy=fx.rounding_policy,
            provenance=fx.provenance,
        )
        if fx
        else None,
    )


@dataclass(frozen=True, slots=True)
class BillingAccountingExportV1:
    """Source-fact transport only; carries no ERP accounting classification."""

    fact: AccountingFactV1
    fact_digest: str

    @property
    def idempotency_key(self) -> str:
        identity = {
            "source_system": self.fact.source_system,
            "scope": _wire(self.fact).scope.model_dump(mode="json"),
            "fact_id": str(self.fact.fact_id),
            "fact_version": self.fact.fact_version,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return f"billing-accounting-fact:v1:{digest}"

    def to_payload(self) -> Payload:
        return {
            "contract": EXPORT_CONTRACT,
            "fact_contract": ACCOUNTING_FACT_CONTRACT,
            "fact_digest": self.fact_digest,
            "fact": _PAYLOAD.validate_json(_wire(self.fact).model_dump_json()),
        }


def map_billing_accounting_fact(fact: AccountingFactV1) -> BillingAccountingExportV1:
    """Validate and map a released fact without inference, persistence or I/O."""
    if not isinstance(fact, AccountingFactV1):
        raise _invalid("A released AccountingFactV1 is required.")
    try:
        wire = _wire(fact)
        payload = _PAYLOAD.validate_json(wire.model_dump_json())
        digest = hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode()
        ).hexdigest()
        return BillingAccountingExportV1(fact=wire.fact(), fact_digest=digest)
    except (ValidationError, ValueError, ArithmeticError) as exc:
        raise _invalid("AccountingFactV1 is malformed or incomplete.") from exc


def parse_billing_accounting_fact(captured_json: str) -> AccountingFactV1:
    """Parse untrusted captured evidence for preview; this confers no source trust."""
    try:
        return _Fact.model_validate_json(captured_json).fact()
    except (ValidationError, ValueError, ArithmeticError) as exc:
        raise _invalid("Captured AccountingFactV1 is malformed or incomplete.") from exc
