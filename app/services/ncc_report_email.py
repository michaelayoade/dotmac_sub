"""Weekly NCC report email — the scheduled digest behind the beat.

Mirrors the CRM ``ncc_report_email`` beat: a weekly summary of the NCC
complaints return (①), sent to the compliance recipients. It is
**default-off** and idempotent per local send-date.

Deliberate divergence from CRM: Sub's ``email`` service does not carry file
attachments, so this sends a **digest with a link** to the export route
rather than an attached workbook. The digest states the row count and how
many rows are not yet filable (the ``[FAIL]`` count), so a compliance officer
knows at a glance whether the return is ready — and follows the link to
download the workbook. Nothing here fabricates: an empty or gap-ridden window
is reported honestly, not padded.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import ncc_complaints_report, ncc_workbook
from app.services.branding_config import get_brand
from app.services.email import send_email
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

_ENABLED_KEY = "ncc_report_email_enabled"
_TO_KEY = "ncc_report_email_to"
_SUBJECT_KEY = "ncc_report_email_subject"
_LOOKBACK_KEY = "ncc_report_email_lookback_days"
_TIMEZONE_KEY = "ncc_report_email_timezone"
_LAST_SENT_KEY = "ncc_report_email_last_sent_local_date"

_DEFAULT_SUBJECT = "Weekly NCC Report"
_DEFAULT_LOOKBACK_DAYS = 7
_DEFAULT_TIMEZONE = "Africa/Lagos"
_EXPORT_PATH = "/admin/reports/ncc-complaints/export"


def _export_url() -> str:
    base_url = str(get_brand().get("app_url") or "").rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("brand app_url must be an absolute HTTP(S) URL")
    return f"{base_url}{_EXPORT_PATH}"


def _str_setting(db: Session, key: str, default: str | None = None) -> str | None:
    value = resolve_value(db, SettingDomain.notification, key)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _int_setting(db: Session, key: str, default: int) -> int:
    value = resolve_value(db, SettingDomain.notification, key)
    if value is None:
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _bool_setting(db: Session, key: str, default: bool) -> bool:
    value = resolve_value(db, SettingDomain.notification, key)
    return value if isinstance(value, bool) else default


def is_enabled(db: Session) -> bool:
    return _bool_setting(db, _ENABLED_KEY, False)


def _local_now(db: Session) -> datetime:
    tz_name = _str_setting(db, _TIMEZONE_KEY, _DEFAULT_TIMEZONE) or _DEFAULT_TIMEZONE
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(_DEFAULT_TIMEZONE)
    return datetime.now(UTC).astimezone(tz)


def _digest_html(
    *, row_count: int, fail_count: int, start: datetime, end: datetime
) -> str:
    window = f"{start:%Y-%m-%d} — {end:%Y-%m-%d}"
    if fail_count:
        readiness = (
            f"<p><strong>{fail_count}</strong> of {row_count} rows are not yet "
            "filable (missing required fields such as LGA/Town). The return is "
            "<strong>not ready to submit</strong> until those are captured.</p>"
        )
    elif row_count:
        readiness = (
            f"<p>All <strong>{row_count}</strong> rows pass validation — the "
            "return is ready to submit.</p>"
        )
    else:
        readiness = "<p>No complaints in this window.</p>"
    return (
        f"<h2>Weekly NCC Complaints Report</h2>"
        f"<p>Reporting window: {window}</p>"
        f"<p>Complaints: <strong>{row_count}</strong></p>"
        f"{readiness}"
        f'<p><a href="{_export_url()}">Download the filing workbook</a> '
        "(admin login required).</p>"
    )


def run_scheduled_ncc_report_email(db: Session) -> dict:
    """Send the weekly digest if due. Idempotent per local send-date.

    Returns a small status dict for the task log; never raises into the beat.
    """
    if not is_enabled(db):
        return {"sent": False, "reason": "disabled"}

    recipient = _str_setting(db, _TO_KEY)
    if not recipient:
        return {"sent": False, "reason": "missing_recipient"}

    local_now = _local_now(db)
    local_date = local_now.date().isoformat()
    if _str_setting(db, _LAST_SENT_KEY) == local_date:
        return {"sent": False, "reason": "already_sent", "local_date": local_date}

    lookback = max(_int_setting(db, _LOOKBACK_KEY, _DEFAULT_LOOKBACK_DAYS), 1)
    end = datetime.now(UTC)
    start = end - timedelta(days=lookback)

    report = ncc_complaints_report.build_report(db, start=start, end=end)
    records = report.get("records", [])
    rows = ncc_workbook.export_rows(records)
    fail_count = sum(
        1 for row in rows if not ncc_workbook.validation_status(row).startswith("[OK]")
    )

    subject = _str_setting(db, _SUBJECT_KEY, _DEFAULT_SUBJECT) or _DEFAULT_SUBJECT
    body_html = _digest_html(
        row_count=len(records), fail_count=fail_count, start=start, end=end
    )

    sent = send_email(
        db,
        recipient,
        subject,
        body_html,
        track=True,
        activity="ncc_report_email",
    )
    if sent:
        _mark_sent(db, local_date)
    return {
        "sent": bool(sent),
        "recipient": recipient,
        "rows": len(records),
        "not_filable": fail_count,
        "local_date": local_date,
    }


def _mark_sent(db: Session, local_date: str) -> None:
    """Record the send date so a same-day re-run is a no-op."""
    from app.schemas.settings import DomainSettingUpdate
    from app.services.domain_settings import notification_settings

    notification_settings.upsert_by_key(
        db,
        _LAST_SENT_KEY,
        DomainSettingUpdate(value_text=local_date),
    )
