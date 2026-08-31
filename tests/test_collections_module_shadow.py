from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.billing import InvoiceDueDateBasis, InvoiceStatus
from app.services.billing._common import InvoiceSettlementAmounts
from app.services.collections_module_shadow import (
    BlockerPairCount,
    CollectionsShadowParityReport,
    EligibilityParity,
    PostpaidEligibilityInput,
    TemporalParityTransitionCount,
    compare_postpaid_eligibility,
    postpaid_eligibility_parity_report,
)

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


def _input(**overrides: object) -> PostpaidEligibilityInput:
    values: dict[str, object] = {
        "invoice_id": uuid4(),
        "account_id": uuid4(),
        "subscription_ids": (uuid4(),),
        "status": InvoiceStatus.issued,
        "currency_code": "NGN",
        "receivable": Decimal("1250.00"),
        "due_at": NOW - timedelta(days=1),
        "due_date_basis": InvoiceDueDateBasis.contract_terms,
        "collectible_ar": True,
        "legacy_reconciliation_hold": False,
    }
    values.update(overrides)
    return PostpaidEligibilityInput(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("item", "parity", "module_blocker"),
    [
        (_input(), EligibilityParity.MATCHED_ACTIONABLE, None),
        (
            _input(due_at=NOW + timedelta(days=1)),
            EligibilityParity.MATCHED_BLOCKED,
            "receivable_not_due",
        ),
        (
            _input(status=InvoiceStatus.paid, receivable=Decimal("0.00")),
            EligibilityParity.MATCHED_BLOCKED,
            "receivable_closed",
        ),
        (
            _input(status=InvoiceStatus.draft, receivable=Decimal("1250.00")),
            EligibilityParity.MATCHED_BLOCKED,
            "no_live_exposure",
        ),
        (
            _input(collectible_ar=False),
            EligibilityParity.MATCHED_BLOCKED,
            "no_live_exposure",
        ),
        (
            _input(due_date_basis=InvoiceDueDateBasis.unknown_unverified),
            EligibilityParity.MATCHED_BLOCKED,
            "due_date_unverified",
        ),
    ],
)
def test_comparison_is_total_for_matched_cohorts(
    item: PostpaidEligibilityInput,
    parity: EligibilityParity,
    module_blocker: str | None,
) -> None:
    result = compare_postpaid_eligibility(item, as_of=NOW)

    assert result.parity == parity
    assert result.module_blocker == module_blocker


def test_null_due_provenance_exposes_the_fail_closed_module_gap() -> None:
    result = compare_postpaid_eligibility(
        _input(
            status=InvoiceStatus.overdue,
            due_at=None,
            due_date_basis=None,
        ),
        as_of=NOW,
    )

    assert result.legacy_blocker is None
    assert result.module_blocker == "due_date_unverified"
    assert result.parity == EligibilityParity.MODULE_BLOCKED_LEGACY_ACTIONABLE


def test_raw_string_hold_preserves_the_incumbent_python_truthiness_quirk() -> None:
    result = compare_postpaid_eligibility(
        _input(legacy_reconciliation_hold=True),
        as_of=NOW,
    )

    assert result.legacy_blocker == "legacy_reconciliation_hold"
    assert result.module_blocker is None
    assert result.parity == EligibilityParity.MODULE_ACTIONABLE_LEGACY_BLOCKED


def test_a_naive_evaluation_instant_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        compare_postpaid_eligibility(_input(), as_of=datetime(2026, 8, 25))


def test_temporal_observation_reveals_a_latent_due_provenance_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import collections_module_shadow as shadow

    future_due_without_provenance = _input(
        due_at=NOW + timedelta(days=1),
        due_date_basis=None,
    )
    monkeypatch.setattr(
        shadow,
        "_inputs_from_snapshot",
        lambda _db: (future_due_without_provenance,),
    )

    report = postpaid_eligibility_parity_report(
        object(),  # type: ignore[arg-type]
        as_of=NOW,
        observe_at=NOW + timedelta(days=1),
    )

    assert report.blocker_pairs == (
        BlockerPairCount("receivable_not_due", "due_date_unverified", 1),
    )
    assert report.observation_blocker_pairs == (
        BlockerPairCount(None, "due_date_unverified", 1),
    )
    assert report.temporal_transitions == (
        TemporalParityTransitionCount(
            EligibilityParity.MATCHED_BLOCKED,
            EligibilityParity.MODULE_BLOCKED_LEGACY_ACTIONABLE,
            1,
        ),
    )
    assert report.latent_temporal_mismatches == 1
    assert report.observation_horizon_seconds == 86400
    assert report.is_parity_safe is False


@pytest.mark.parametrize(
    ("as_of", "observe_at", "message"),
    [
        (datetime(2026, 8, 25), NOW, "as_of must be timezone-aware"),
        (NOW, datetime(2026, 8, 26), "observe_at must be timezone-aware"),
        (NOW, NOW - timedelta(seconds=1), "must not be earlier"),
    ],
)
def test_temporal_observation_refuses_invalid_instants(
    monkeypatch: pytest.MonkeyPatch,
    as_of: datetime,
    observe_at: datetime,
    message: str,
) -> None:
    from app.services import collections_module_shadow as shadow

    monkeypatch.setattr(shadow, "_inputs_from_snapshot", lambda _db: ())

    with pytest.raises(ValueError, match=message):
        postpaid_eligibility_parity_report(
            object(),  # type: ignore[arg-type]
            as_of=as_of,
            observe_at=observe_at,
        )


def test_same_instant_temporal_observation_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import collections_module_shadow as shadow

    monkeypatch.setattr(shadow, "_inputs_from_snapshot", lambda _db: (_input(),))

    report = postpaid_eligibility_parity_report(
        object(),  # type: ignore[arg-type]
        as_of=NOW,
        observe_at=NOW,
    )

    assert report.observation_horizon_seconds == 0
    assert report.blocker_pairs == report.observation_blocker_pairs
    assert report.temporal_transitions == (
        TemporalParityTransitionCount(
            EligibilityParity.MATCHED_ACTIONABLE,
            EligibilityParity.MATCHED_ACTIONABLE,
            1,
        ),
    )
    assert report.latent_temporal_mismatches == 0


def test_cli_refuses_a_reversed_temporal_horizon_before_opening_a_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.migration import collections_module_shadow_parity as command

    monkeypatch.setattr(
        command,
        "read_only_snapshot_session",
        lambda: pytest.fail("the invalid horizon must fail before database access"),
    )

    with pytest.raises(SystemExit) as excinfo:
        command.main(
            [
                "--as-of",
                "2026-08-26T00:00:00Z",
                "--observe-at",
                "2026-08-25T00:00:00Z",
            ]
        )

    assert excinfo.value.code == 2


def test_report_serialization_is_aggregate_and_total() -> None:
    report = CollectionsShadowParityReport(
        evaluation_instant=NOW,
        observation_instant=NOW + timedelta(days=1),
        invoices=7,
        matched_actionable=2,
        matched_blocked=3,
        module_blocked_legacy_actionable=1,
        module_actionable_legacy_blocked=1,
        null_due_date_basis=1,
        explicit_unknown_due_date_basis=2,
        subject_scoped_exposures=3,
        single_service_exposures=2,
        multi_service_exposures=2,
        module_blockers=(("due_date_unverified", 3),),
        blocker_pairs=(
            BlockerPairCount("receivable_not_due", "due_date_unverified", 3),
        ),
        observation_blocker_pairs=(BlockerPairCount(None, "due_date_unverified", 3),),
        temporal_transitions=(
            TemporalParityTransitionCount(
                EligibilityParity.MATCHED_BLOCKED,
                EligibilityParity.MODULE_BLOCKED_LEGACY_ACTIONABLE,
                1,
            ),
        ),
        observation_module_blocked_legacy_actionable=1,
        observation_module_actionable_legacy_blocked=0,
        latent_temporal_mismatches=1,
    )

    payload = report.as_dict()
    assert payload["classified"] == payload["invoices"] == 7
    assert payload["blocking_reasons"] == [
        "module_blocked_legacy_actionable",
        "module_actionable_legacy_blocked",
        "latent_temporal_mismatch",
    ]
    assert payload["is_parity_safe"] is False
    assert set(payload) == {
        "invoices",
        "observation_horizon_seconds",
        "classified",
        "matched_actionable",
        "matched_blocked",
        "module_blocked_legacy_actionable",
        "module_actionable_legacy_blocked",
        "null_due_date_basis",
        "explicit_unknown_due_date_basis",
        "subject_scoped_exposures",
        "single_service_exposures",
        "multi_service_exposures",
        "module_blockers",
        "blocker_pairs",
        "observation_blocker_pairs",
        "temporal_transitions",
        "observation_module_blocked_legacy_actionable",
        "observation_module_actionable_legacy_blocked",
        "latent_temporal_mismatches",
        "blocking_reasons",
        "is_parity_safe",
    }
    serialized = json.dumps(payload, sort_keys=True)
    assert "2026-" not in serialized
    assert "invoice_id" not in serialized
    assert "account_id" not in serialized


def test_snapshot_uses_canonical_post_refund_payment_and_credit_amounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the exact incumbent settlement semantics at the module seam.

    ``payments_applied`` is the canonical resolver's post-refund allocation.
    Opening funding is intentionally not subtracted because the incumbent
    postpaid dunning snapshot excludes it; changing that belongs to Billing
    input parity, not this observer.
    """

    from app.services import collections_module_shadow as shadow

    invoice_id = uuid4()
    invoice = SimpleNamespace(
        id=invoice_id,
        account_id=uuid4(),
        status=InvoiceStatus.partially_paid,
        currency="NGN",
        total=Decimal("100.00"),
        due_at=NOW - timedelta(days=1),
        due_date_basis=InvoiceDueDateBasis.contract_terms,
        metadata_={},
    )

    class _Scalars:
        def __init__(self, rows: list[object]) -> None:
            self._rows = rows

        def all(self) -> list[object]:
            return self._rows

    class _Session:
        calls = 0

        def scalars(self, _statement: object) -> _Scalars:
            self.calls += 1
            return _Scalars([invoice] if self.calls == 1 else [invoice_id])

        def execute(self, _statement: object) -> list[tuple[object, object]]:
            return []

    monkeypatch.setattr(
        shadow,
        "resolve_invoice_settlement_amounts",
        lambda _db, _invoice_id: InvoiceSettlementAmounts(
            payments_applied=Decimal("35.00"),
            credits_applied=Decimal("15.00"),
            opening_funding_applied=Decimal("20.00"),
        ),
    )

    (item,) = shadow._inputs_from_snapshot(_Session())  # type: ignore[arg-type]

    assert item.receivable == Decimal("50.00")
    assert item.legacy_reconciliation_hold is False
    assert compare_postpaid_eligibility(item, as_of=NOW).parity == (
        EligibilityParity.MATCHED_ACTIONABLE
    )
