"""Owner of reviewed prepaid funding reconstruction and authority cutover.

Reconstruction materializes an independently proven customer position at one
timestamp. Runtime funding is that baseline plus canonical native events after
the timestamp. Source exports and bank statements are evidence inputs only;
they are never queried as runtime balances.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
)
from app.models.subscriber import Subscriber
from app.services import display_format
from app.services.common import coerce_uuid, round_money


class PrepaidFundingReconstructionError(ValueError):
    pass


class PrepaidFundingBaselineMissingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReconstructionRow:
    account_id: UUID
    amount: Decimal


@dataclass(frozen=True)
class ReconstructionManifest:
    source: str
    position_at: datetime
    currency: str
    rows: tuple[ReconstructionRow, ...]
    manifest_sha256: str


@dataclass(frozen=True)
class ReconstructionPreview:
    manifest: ReconstructionManifest
    blockers: tuple[str, ...]
    create_count: int
    replace_count: int
    unchanged_count: int
    total_amount: Decimal
    idempotent_replay: bool

    @property
    def ready(self) -> bool:
        return not self.blockers

    def report(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "source": self.manifest.source,
            "position_at": self.manifest.position_at.isoformat(),
            "currency": self.manifest.currency,
            "manifest_sha256": self.manifest.manifest_sha256,
            "account_count": len(self.manifest.rows),
            "total_amount": f"{self.total_amount:.2f}",
            "create_count": self.create_count,
            "replace_count": self.replace_count,
            "unchanged_count": self.unchanged_count,
            "idempotent_replay": self.idempotent_replay,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class ReconstructionApplyResult:
    batch: PrepaidFundingReconstructionBatch
    preview: ReconstructionPreview
    idempotent_replay: bool


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise PrepaidFundingReconstructionError(
            "reconstruction position_at must include a timezone"
        )
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _currency(value: object) -> str:
    currency = str(value or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise PrepaidFundingReconstructionError(
            "reconstruction currency must be a three-letter code"
        )
    return currency


def default_prepaid_funding_currency(db: Session) -> str:
    return _currency(display_format.default_currency(db))


def _money(value: object) -> Decimal:
    try:
        return round_money(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PrepaidFundingReconstructionError(
            f"invalid reconstructed amount: {value!s}"
        ) from exc


def _parse_position_at(payload: dict[str, Any]) -> datetime:
    raw = payload.get("position_at", payload.get("captured_at"))
    if not isinstance(raw, str) or not raw.strip():
        raise PrepaidFundingReconstructionError(
            "reconstruction manifest requires position_at or captured_at"
        )
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrepaidFundingReconstructionError(
            "reconstruction position_at is invalid"
        ) from exc
    return _as_utc(parsed)


def _normalized_manifest_payload(
    *,
    source: str,
    position_at: datetime,
    currency: str,
    rows: tuple[ReconstructionRow, ...],
) -> dict[str, Any]:
    return {
        "source": source,
        "position_at": position_at.isoformat().replace("+00:00", "Z"),
        "currency": currency,
        "accounts": [
            {
                "account_id": str(row.account_id),
                "available_balance": f"{row.amount:.2f}",
            }
            for row in rows
        ],
    }


def parse_reconstruction_manifest(payload: dict[str, Any]) -> ReconstructionManifest:
    source = str(payload.get("source") or "").strip()
    if not source:
        raise PrepaidFundingReconstructionError(
            "reconstruction source must not be empty"
        )
    position_at = _parse_position_at(payload)
    currency = _currency(payload.get("currency"))
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise PrepaidFundingReconstructionError(
            "reconstruction manifest accounts must not be empty"
        )
    by_account: dict[UUID, ReconstructionRow] = {}
    for item in accounts:
        if not isinstance(item, dict):
            raise PrepaidFundingReconstructionError(
                "reconstruction account rows must be objects"
            )
        account_id = coerce_uuid(item.get("account_id"))
        if account_id in by_account:
            raise PrepaidFundingReconstructionError(
                f"duplicate reconstruction account: {account_id}"
            )
        by_account[account_id] = ReconstructionRow(
            account_id=account_id,
            amount=_money(item.get("available_balance")),
        )
    rows = tuple(sorted(by_account.values(), key=lambda row: str(row.account_id)))
    normalized = _normalized_manifest_payload(
        source=source,
        position_at=position_at,
        currency=currency,
        rows=rows,
    )
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ReconstructionManifest(
        source=source,
        position_at=position_at,
        currency=currency,
        rows=rows,
        manifest_sha256=digest,
    )


def _active_baselines(
    db: Session, account_ids: set[UUID], currency: str
) -> dict[UUID, PrepaidFundingBaseline]:
    if not account_ids:
        return {}
    return {
        baseline.account_id: baseline
        for baseline in db.scalars(
            select(PrepaidFundingBaseline).where(
                PrepaidFundingBaseline.account_id.in_(account_ids),
                PrepaidFundingBaseline.currency == currency,
                PrepaidFundingBaseline.is_active.is_(True),
            )
        ).all()
    }


def preview_prepaid_funding_reconstruction(
    db: Session,
    payload: dict[str, Any],
    *,
    expected_account_ids: set[object],
    now: datetime | None = None,
) -> ReconstructionPreview:
    manifest = parse_reconstruction_manifest(payload)
    row_ids = {row.account_id for row in manifest.rows}
    blockers: list[str] = []
    expected = {coerce_uuid(value) for value in expected_account_ids}
    if not expected:
        blockers.append("reconstruction_expected_cohort_empty")
    blockers.extend(
        f"missing_reconstruction_account:{account_id}"
        for account_id in sorted(expected - row_ids, key=str)
    )
    blockers.extend(
        f"unexpected_reconstruction_account:{account_id}"
        for account_id in sorted(row_ids - expected, key=str)
    )
    effective_now = _as_utc(now or datetime.now(UTC))
    if manifest.position_at > effective_now:
        blockers.append("reconstruction_position_in_future")
    existing_accounts = set(
        db.scalars(select(Subscriber.id).where(Subscriber.id.in_(row_ids))).all()
    )
    blockers.extend(
        f"reconstruction_account_not_found:{account_id}"
        for account_id in sorted(row_ids - existing_accounts, key=str)
    )
    active = _active_baselines(db, row_ids, manifest.currency)
    create_count = 0
    replace_count = 0
    unchanged_count = 0
    for row in manifest.rows:
        baseline = active.get(row.account_id)
        if baseline is None:
            create_count += 1
            continue
        baseline_at = _stored_utc(baseline.position_at)
        if baseline_at == manifest.position_at and baseline.amount == row.amount:
            unchanged_count += 1
            continue
        replace_count += 1
        if baseline_at >= manifest.position_at:
            blockers.append(f"reconstruction_position_not_newer:{row.account_id}")
    existing_batch = db.scalar(
        select(PrepaidFundingReconstructionBatch).where(
            PrepaidFundingReconstructionBatch.manifest_sha256
            == manifest.manifest_sha256
        )
    )
    total = round_money(sum((row.amount for row in manifest.rows), Decimal("0.00")))
    return ReconstructionPreview(
        manifest=manifest,
        blockers=tuple(blockers),
        create_count=create_count,
        replace_count=replace_count,
        unchanged_count=unchanged_count,
        total_amount=total,
        idempotent_replay=existing_batch is not None,
    )


def apply_prepaid_funding_reconstruction(
    db: Session,
    payload: dict[str, Any],
    *,
    expected_manifest_sha256: str,
    evidence_ref: str,
    approved_by: str,
    expected_account_ids: set[object],
    now: datetime | None = None,
) -> ReconstructionApplyResult:
    evidence = evidence_ref.strip()
    actor = approved_by.strip()
    if not evidence:
        raise PrepaidFundingReconstructionError("evidence_ref is required")
    if not actor:
        raise PrepaidFundingReconstructionError("approved_by is required")
    preview = preview_prepaid_funding_reconstruction(
        db,
        payload,
        expected_account_ids=expected_account_ids,
        now=now,
    )
    if preview.manifest.manifest_sha256 != expected_manifest_sha256.strip().lower():
        raise PrepaidFundingReconstructionError(
            "reconstruction manifest hash does not match reviewed hash"
        )
    existing = db.scalar(
        select(PrepaidFundingReconstructionBatch).where(
            PrepaidFundingReconstructionBatch.manifest_sha256
            == preview.manifest.manifest_sha256
        )
    )
    if existing is not None:
        return ReconstructionApplyResult(
            batch=existing,
            preview=preview,
            idempotent_replay=True,
        )
    if preview.blockers:
        raise PrepaidFundingReconstructionError(
            "prepaid funding reconstruction blocked: " + ", ".join(preview.blockers)
        )

    row_ids = {row.account_id for row in preview.manifest.rows}
    locked_ids = set(
        db.scalars(
            select(Subscriber.id).where(Subscriber.id.in_(row_ids)).with_for_update()
        ).all()
    )
    if locked_ids != row_ids:
        raise PrepaidFundingReconstructionError(
            "reconstruction cohort changed while acquiring account locks"
        )
    applied_at = _as_utc(now or datetime.now(UTC))
    db.execute(
        update(PrepaidFundingBaseline)
        .where(
            PrepaidFundingBaseline.account_id.in_(row_ids),
            PrepaidFundingBaseline.currency == preview.manifest.currency,
            PrepaidFundingBaseline.is_active.is_(True),
        )
        .values(is_active=False, superseded_at=applied_at)
    )
    batch = PrepaidFundingReconstructionBatch(
        manifest_sha256=preview.manifest.manifest_sha256,
        source=preview.manifest.source,
        evidence_ref=evidence,
        position_at=preview.manifest.position_at,
        currency=preview.manifest.currency,
        account_count=len(preview.manifest.rows),
        total_amount=preview.total_amount,
        approved_by=actor,
        is_authority_cutover=(authority_cutover_batch(db) is None),
        approved_at=applied_at,
    )
    db.add(batch)
    db.flush()
    db.add_all(
        [
            PrepaidFundingBaseline(
                batch_id=batch.id,
                account_id=row.account_id,
                currency=preview.manifest.currency,
                amount=row.amount,
                position_at=preview.manifest.position_at,
                is_active=True,
            )
            for row in preview.manifest.rows
        ]
    )
    db.flush()
    return ReconstructionApplyResult(
        batch=batch,
        preview=preview,
        idempotent_replay=False,
    )


def active_prepaid_funding_baseline(
    db: Session, account_id: object, *, currency: str | None = None
) -> PrepaidFundingBaseline | None:
    unit = _currency(currency or default_prepaid_funding_currency(db))
    return db.scalar(
        select(PrepaidFundingBaseline).where(
            PrepaidFundingBaseline.account_id == coerce_uuid(account_id),
            PrepaidFundingBaseline.currency == unit,
            PrepaidFundingBaseline.is_active.is_(True),
        )
    )


def authority_cutover_batch(
    db: Session,
) -> PrepaidFundingReconstructionBatch | None:
    """Return the one irreversible transition away from legacy funding."""
    return db.scalar(
        select(PrepaidFundingReconstructionBatch).where(
            PrepaidFundingReconstructionBatch.is_authority_cutover.is_(True)
        )
    )


def verified_prepaid_funding_balances(
    db: Session,
    account_ids: set[object] | list[object] | tuple[object, ...],
    *,
    currency: str | None = None,
) -> dict[UUID, Decimal]:
    """Resolve opening balances plus native deltas in bounded cohort queries."""
    from app.services.customer_financial_ledger import (
        customer_financial_balances_by_currency,
    )

    unit = _currency(currency or default_prepaid_funding_currency(db))
    ids = {coerce_uuid(value) for value in account_ids}
    if not ids:
        return {}
    baselines = _active_baselines(db, ids, unit)
    cutover = authority_cutover_batch(db)
    if cutover is None:
        raise PrepaidFundingBaselineMissingError(
            "prepaid funding authority cutover has not been materialized"
        )
    accounts = {
        account.id: account
        for account in db.scalars(
            select(Subscriber).where(Subscriber.id.in_(ids))
        ).all()
    }
    unknown = sorted(ids - set(accounts), key=str)
    if unknown:
        raise PrepaidFundingBaselineMissingError(
            "prepaid funding account not found: "
            + ", ".join(str(value) for value in unknown)
        )
    opening_by_account: dict[UUID, Decimal] = {}
    accounts_by_position: dict[datetime, set[UUID]] = {}
    for account_id in sorted(ids, key=str):
        baseline = baselines.get(account_id)
        if baseline is None:
            created_at = accounts[account_id].created_at
            account_start = (
                created_at.replace(tzinfo=UTC)
                if created_at.tzinfo is None
                else created_at.astimezone(UTC)
            )
            if account_start <= _stored_utc(cutover.position_at):
                raise PrepaidFundingBaselineMissingError(
                    f"verified prepaid funding baseline missing for: {account_id}"
                )
            opening_amount = Decimal("0.00")
            position_at = _stored_utc(cutover.position_at)
        else:
            opening_amount = baseline.amount
            position_at = _stored_utc(baseline.position_at)
        opening_by_account[account_id] = opening_amount
        accounts_by_position.setdefault(position_at, set()).add(account_id)

    balances: dict[UUID, Decimal] = {}
    for position_at, position_account_ids in accounts_by_position.items():
        native_by_account = customer_financial_balances_by_currency(
            db,
            position_account_ids,
            start=position_at,
            include_legacy_mirror=False,
        )
        for account_id in position_account_ids:
            native_delta = native_by_account[account_id].get(unit, Decimal("0.00"))
            balances[account_id] = round_money(
                opening_by_account[account_id] + native_delta
            )
    return balances


def verified_prepaid_funding_balance(
    db: Session, account_id: object, *, currency: str | None = None
) -> Decimal:
    account_uuid = coerce_uuid(account_id)
    return verified_prepaid_funding_balances(db, [account_uuid], currency=currency)[
        account_uuid
    ]
