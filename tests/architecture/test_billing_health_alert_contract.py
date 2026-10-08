from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "deploy" / "observability" / "billing_health.rules.yml"
PREPAID_RULES = ROOT / "deploy" / "observability" / "prepaid_enforcement.rules.yml"
HEALTH = ROOT / "app" / "services" / "billing_health.py"


def test_aged_draft_stock_cannot_page_as_new_leakage() -> None:
    source = RULES.read_text(encoding="utf-8")
    health_source = HEALTH.read_text(encoding="utf-8")

    assert "SubAgedDraftInvoiceBacklogGrowing" not in source
    assert 'signal="aged_draft_invoices"' not in source
    assert "SubRecentDraftInvoiceCohortStalled" in source
    assert 'signal="stalled_draft_invoice_cohort",scope="all"} > 25' in source
    assert "STALLED_DRAFT_ALERT_COUNT = 25" in health_source


def test_money_path_prevention_alerts_use_owner_observations() -> None:
    source = RULES.read_text(encoding="utf-8")
    prepaid_source = PREPAID_RULES.read_text(encoding="utf-8")

    assert "SubPaymentReceiptEmailTemplateUnavailable" in source
    assert 'signal="payment_receipt_email_template_ready",scope="all"} == 0' in source
    assert "SubPrepaidFundingQuarantineGrowing" in source
    assert 'signal="prepaid_funding_quarantined",scope="all"}[24h]) > 0' in source
    assert "PrepaidFundingQuarantineActive" not in prepaid_source


def test_prepaid_lock_contention_alert_uses_the_bounded_sweep_observation() -> None:
    source = PREPAID_RULES.read_text(encoding="utf-8")

    assert "PrepaidSweepLockContentionPersistent" in source
    assert 'signal="lock_deferred"} > 0' in source
    assert 'runbook: "docs/runbooks/DATABASE_TRANSACTION_PRESSURE.md"' in source


def test_prepaid_work_item_sla_alert_uses_the_overdue_observation() -> None:
    source = PREPAID_RULES.read_text(encoding="utf-8")
    scheduled_source = (
        ROOT / "app" / "services" / "collections" / "scheduled.py"
    ).read_text(encoding="utf-8")

    assert "PrepaidWorkItemsOverdue" in source
    assert 'signal="work_items_overdue"} > 0' in source
    assert '"work_items_overdue"' in scheduled_source
    assert '"renewal_terms_work_items_open"' in scheduled_source


def _alert_block(source: str, name: str) -> str:
    start = source.index(f"- alert: {name}\n")
    end = source.find("- alert: ", start + 10)
    return source[start : end if end != -1 else len(source)]


def test_finance_work_item_alerts_share_the_work_item_owner_and_runbook() -> None:
    """Alerts and the work items they summarise name one owner and runbook."""
    from app.services.collections.scheduled import (
        PREPAID_COVERAGE_QUARANTINE_RUNBOOK,
    )
    from app.services.prepaid_coverage_quarantine_review import (
        RUNBOOK as QUARANTINE_REVIEW_RUNBOOK,
    )
    from app.services.prepaid_renewal_terms_backfill import (
        RENEWAL_TERMS_RUNBOOK,
        RENEWAL_TERMS_WORK_ITEM_OWNER,
    )

    source = PREPAID_RULES.read_text(encoding="utf-8")
    assert (ROOT / RENEWAL_TERMS_RUNBOOK).is_file()
    for name in ("PrepaidRenewalTermsUnresolved", "PrepaidWorkItemsOverdue"):
        block = _alert_block(source, name)
        assert f"owner: {RENEWAL_TERMS_WORK_ITEM_OWNER}" in block
        assert f'runbook: "{RENEWAL_TERMS_RUNBOOK}"' in block
    quarantine = _alert_block(source, "PrepaidCoverageQuarantinedEvidence")
    assert f"owner: {RENEWAL_TERMS_WORK_ITEM_OWNER}" in quarantine
    assert f'runbook: "{PREPAID_COVERAGE_QUARANTINE_RUNBOOK}"' in quarantine
    assert (ROOT / PREPAID_COVERAGE_QUARANTINE_RUNBOOK).is_file()
    # The quarantine work item names the same owner and runbook as its alert.
    scheduled_source = (
        ROOT / "app" / "services" / "collections" / "scheduled.py"
    ).read_text(encoding="utf-8")
    assert f'"owner": "{RENEWAL_TERMS_WORK_ITEM_OWNER}"' in scheduled_source
    assert QUARANTINE_REVIEW_RUNBOOK == PREPAID_COVERAGE_QUARANTINE_RUNBOOK

    # One label vocabulary: work items never use the retired spelling.
    for path in (ROOT / "app").rglob("*.py"):
        assert '"finance-billing"' not in path.read_text(encoding="utf-8"), path
    for path in (ROOT / "docs" / "runbooks").glob("*.md"):
        assert "`finance-billing`" not in path.read_text(encoding="utf-8"), path
    # Every alert runbook annotation points at a real file.
    for rules in (ROOT / "deploy" / "observability").glob("*.rules.yml"):
        for line in rules.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("runbook:"):
                target = stripped.split(":", 1)[1].strip().strip('"')
                if not target.startswith("http"):
                    assert (ROOT / target).is_file(), (rules.name, target)


def test_prepaid_coverage_quarantine_alert_links_the_finance_runbook() -> None:
    source = PREPAID_RULES.read_text(encoding="utf-8")
    runbook = "docs/runbooks/PREPAID_COVERAGE_QUARANTINE_FINANCE_REVIEW.md"
    alert = source.split("- alert: PrepaidCoverageQuarantinedEvidence", 1)[1]
    alert = alert.split("- alert:", 1)[0]

    assert f'runbook: "{runbook}"' in alert
    assert (ROOT / runbook).is_file()
    assert (
        ROOT / "scripts" / "billing" / "diagnose_prepaid_coverage_quarantine.py"
    ).is_file()
