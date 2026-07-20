"""Wiring tests for the NCC ①/pack routes, the weekly beat, and the on-demand
AI insight route. These exercise the route handlers directly (as functions
with a db_session), mirroring the repo's other report-route tests, and stub
the AI gateway the way test_ai_engine does.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from app.models.ai_insight import AIInsight
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import control_registry, ncc_report_email
from app.services.ai import engine as ai_engine
from app.services.domain_settings import notification_settings
from app.services.settings_spec import get_spec, resolve_value
from app.web.admin import reports as reports_web


def _fake_current_user():
    return SimpleNamespace(id=None, username="admin", email="admin@example.test")


def _stub_admin(monkeypatch):
    import app.web.admin as admin_pkg

    monkeypatch.setattr(
        admin_pkg, "get_current_user", lambda request: _fake_current_user()
    )
    monkeypatch.setattr(admin_pkg, "get_sidebar_stats", lambda db: {})


def _request():
    # A TemplateResponse only needs request.scope for url_for; a minimal
    # Starlette-style stand-in is enough for these render smoke tests.
    return SimpleNamespace(
        scope={"type": "http"},
        query_params={},
        url=SimpleNamespace(path="/admin/reports"),
    )


# ── NCC complaints + pack ────────────────────────────────────────────────────


def test_ncc_complaints_export_streams_a_valid_xlsx(db_session):
    resp = reports_web.reports_ncc_complaints_export(db=db_session)
    assert resp.media_type.endswith("spreadsheetml.sheet")
    # The body is a zip (xlsx). Its magic bytes are PK\x03\x04.
    assert resp.body[:4] == b"PK\x03\x04"
    assert "attachment; filename=" in resp.headers["Content-Disposition"]


def test_ncc_regulatory_pack_json_has_all_three_returns(db_session):
    import json

    resp = reports_web.reports_ncc_regulatory_pack(db=db_session)
    pack = json.loads(resp.body)
    assert set(pack) >= {"meta", "complaints", "subscribers", "financials", "staff"}
    # Every section reports its own availability; nothing fabricates.
    for key in ("complaints", "subscribers", "financials", "staff"):
        assert "available" in pack[key]


def test_ncc_pack_pdf_route_returns_a_document(db_session):
    resp = reports_web.reports_ncc_regulatory_pack_pdf(db=db_session)
    # weasyprint may be unavailable in CI; either a real PDF or the honest
    # HTML fallback is acceptable — never an empty/fake document.
    assert resp.media_type in (
        "application/pdf",
        "text/html; charset=utf-8",
    )
    assert resp.body


# ── the weekly beat ──────────────────────────────────────────────────────────


def test_ncc_report_email_beat_is_registered_and_default_off(db_session):
    from app.services import ncc_report_email

    # Default OFF: no setting row means disabled.
    assert ncc_report_email.is_enabled(db_session) is False
    result = ncc_report_email.run_scheduled_ncc_report_email(db_session)
    assert result == {"sent": False, "reason": "disabled"}


def test_ncc_report_email_task_is_importable():
    from app.tasks import send_scheduled_ncc_report

    assert callable(send_scheduled_ncc_report)


def test_ncc_report_email_marker_is_registered_and_prevents_a_second_send(
    db_session, monkeypatch
):
    assert (
        get_spec(SettingDomain.notification, "ncc_report_email_last_sent_local_date")
        is not None
    )
    notification_settings.upsert_by_key(
        db_session,
        "ncc_report_email_enabled",
        DomainSettingUpdate(
            value_type=SettingValueType.boolean,
            value_text="true",
        ),
    )
    notification_settings.upsert_by_key(
        db_session,
        "ncc_report_email_to",
        DomainSettingUpdate(value_text="compliance@example.test"),
    )
    now = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)
    sent_bodies: list[str] = []
    monkeypatch.setattr(ncc_report_email, "_local_now", lambda db: now)
    monkeypatch.setattr(
        ncc_report_email.ncc_complaints_report,
        "build_report",
        lambda db, **kwargs: {"records": []},
    )
    monkeypatch.setattr(
        ncc_report_email,
        "get_brand",
        lambda: {"app_url": "https://selfcare.dotmac.io"},
    )
    monkeypatch.setattr(
        ncc_report_email,
        "send_email",
        lambda db, recipient, subject, body_html, **kwargs: (
            sent_bodies.append(body_html) or True
        ),
    )

    first = ncc_report_email.run_scheduled_ncc_report_email(db_session)
    second = ncc_report_email.run_scheduled_ncc_report_email(db_session)

    assert first["sent"] is True
    assert second == {
        "sent": False,
        "reason": "already_sent",
        "local_date": "2026-07-18",
    }
    assert len(sent_bodies) == 1
    assert (
        "https://selfcare.dotmac.io/admin/reports/ncc-complaints/export"
        in sent_bodies[0]
    )
    assert (
        resolve_value(
            db_session,
            SettingDomain.notification,
            "ncc_report_email_last_sent_local_date",
        )
        == "2026-07-18"
    )


# ── AI insight route ─────────────────────────────────────────────────────────


def _enable_generation(db):
    control = control_registry._CONTROLS["ai.generation"]
    db.add(
        DomainSetting(
            domain=SettingDomain.modules,
            key=control_registry.canonical_setting_key(control),
            value_type=SettingValueType.boolean,
            value_text="true",
            is_active=True,
        )
    )
    db.flush()


class _Gateway:
    def enabled(self, db):
        return True

    def generate_with_fallback(self, db, **kwargs):
        return (
            SimpleNamespace(
                content='{"title": "Breaches cluster", "summary": "s",'
                ' "risk_level": "high", "recommended_actions": ["look here"]}',
                provider="vllm",
                model="qwen2.5",
                tokens_in=100,
                tokens_out=50,
            ),
            {"endpoint": "primary"},
        )


def test_insight_route_generates_and_renders(db_session, monkeypatch):
    _stub_admin(monkeypatch)
    _enable_generation(db_session)
    with (
        patch.object(ai_engine, "_gateway", lambda: _Gateway()),
        patch.object(
            reports_web.ticket_sla_reports_service,
            "summary",
            lambda db, a, b: {
                "total_clocks": 40,
                "total_breaches": 12,
                "breach_rate": 0.3,
            },
        ),
    ):
        resp = reports_web.reports_generate_insight(
            _request(), "ticket_sla_advisor", db=db_session
        )
    assert resp.status_code == 200
    # Exactly one insight persisted, through the single writer.
    assert db_session.query(AIInsight).count() == 1


def test_insight_route_degrades_gracefully_when_disabled(db_session, monkeypatch):
    _stub_admin(monkeypatch)
    # ai.generation OFF (no row) → advise() raises AIEngineError → graceful msg.
    with patch.object(
        reports_web.ticket_sla_reports_service,
        "summary",
        lambda db, a, b: {"total_clocks": 0, "total_breaches": 0, "breach_rate": 0.0},
    ):
        resp = reports_web.reports_generate_insight(
            _request(), "ticket_sla_advisor", db=db_session
        )
    # Not a 500 — a rendered partial with the disabled message.
    assert resp.status_code == 200
    assert db_session.query(AIInsight).count() == 0


def test_insight_route_unknown_advisor_is_404(db_session, monkeypatch):
    _stub_admin(monkeypatch)
    resp = reports_web.reports_generate_insight(
        _request(), "no_such_advisor", db=db_session
    )
    assert resp.status_code == 404


def test_ai_engine_declared_and_out_of_writer_baseline():
    from pathlib import Path

    from app.services import sot_relationships as sr

    names = {s.name for d in sr.DOMAIN_SOT_RELATIONSHIPS for s in d.services}
    assert "ai.generation" in names
    baseline = Path("tests/architecture/sot_writer_baseline.txt").read_text()
    assert "app.services.ai.engine" not in baseline.split()
