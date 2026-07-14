from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELD_MOBILE = ROOT / "field_mobile" / "lib"


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text()


def test_field_mobile_has_no_work_order_status_label_or_color_dictionary() -> None:
    theme = _read("field_mobile/lib/app/theme.dart")
    models = _read("field_mobile/lib/features/jobs/job_models.dart")
    field_sources = "\n".join(path.read_text() for path in FIELD_MOBILE.rglob("*.dart"))

    assert "statusColors" not in theme
    assert "_statusLabels" not in theme
    assert "String statusLabel(" not in models
    assert "AppColors.status(" not in field_sources


def test_field_mobile_job_surfaces_consume_server_status_presentation() -> None:
    job_card = _read("field_mobile/lib/features/jobs/widgets/job_card.dart")
    detail = _read("field_mobile/lib/features/jobs/job_detail_screen.dart")
    manager = _read("field_mobile/lib/features/manager/manager_screen.dart")
    map_models = _read("field_mobile/lib/features/today/map_models.dart")

    assert "StatusPill(job.statusPresentation)" in job_card
    assert "job.statusPresentation.label" in detail
    assert "StatusPill(job.statusPresentation)" in manager
    assert "statusPresentation: job.statusPresentation" in map_models
