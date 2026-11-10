"""Customer report reads stay on the registered usage and growth owners."""

from pathlib import Path

from app.services.sot_manifest import contract_validation_errors
from app.services.sot_registry.registry import all_services


def test_customer_report_owners_have_complete_typed_contracts():
    services = all_services()
    names = {service.name for service in services}
    owners = {
        service.name: service
        for service in services
        if service.name in {"customer.usage_summary", "subscriber.growth_reports"}
    }
    assert set(owners) == {"customer.usage_summary", "subscriber.growth_reports"}
    for owner in owners.values():
        assert owner.contract is not None
        assert contract_validation_errors(owner, service_names=names) == ()


def test_customer_report_adapter_only_composes_owner_reads():
    route = Path("app/web/admin/reports.py").read_text(encoding="utf-8")
    report = Path("app/services/web_reports.py").read_text(encoding="utf-8")
    assert "get_customer_report_overview(" in route
    assert "get_subscribers_report_data(" in route
    assert "customer_report_growth_series(" in report
    assert "period_usage_by_subscriber(" in report
    assert "monthly_customer_growth_series(db)" not in report
