"""Dependency-light value tests; no database or deployment-schema claims."""

from datetime import date
from pathlib import Path

import pytest
from jinja2 import (
    ChoiceLoader,
    DictLoader,
    Environment,
    FileSystemLoader,
    select_autoescape,
)
from pydantic import ValidationError

from app.services.customer_occasion_contracts import CustomerOccasion, OccasionSettings
from app.services.customer_occasion_values import (
    defaults,
    occasion_years,
    render,
    validate,
)
from app.services.domain_errors import DomainError


@pytest.mark.parametrize(
    ("original", "today", "years"),
    [
        (date(2020, 2, 29), date(2026, 2, 28), 6),
        (date(2020, 2, 29), date(2024, 2, 28), None),
        (date(2020, 2, 29), date(2024, 2, 29), 4),
        (date(2027, 10, 16), date(2026, 10, 16), None),
        (date(2023, 10, 16), date(2026, 10, 16), 3),
        (date(2023, 10, 15), date(2026, 10, 16), None),
    ],
)
def test_calendar_observation(original: date, today: date, years: int | None) -> None:
    assert occasion_years(original=original, today=today) == years


@pytest.mark.parametrize("occasion", list(CustomerOccasion))
def test_defaults_are_review_only(occasion: CustomerOccasion) -> None:
    settings = defaults(occasion)
    assert not settings.enabled
    assert (settings.send_hour, settings.send_minute, settings.timezone) == (
        9,
        0,
        "Africa/Lagos",
    )
    validate(settings)


@pytest.mark.parametrize(
    "template",
    ["{first_name.__class__}", "{unknown}", "{first_name!r}", "{years:100000000}", "{"],
)
def test_invalid_placeholders_are_rejected(template: str) -> None:
    with pytest.raises(DomainError):
        validate(
            defaults(CustomerOccasion.birthday).model_copy(update={"body": template})
        )


def test_invalid_timezone_and_header_newlines_are_rejected() -> None:
    for change in (
        {"timezone": "not/a/timezone"},
        {"subject": "Hello\r\nBcc: someone"},
    ):
        with pytest.raises(DomainError):
            validate(defaults(CustomerOccasion.birthday).model_copy(update=change))


def test_render_escapes_customer_values_and_plain_text_markup() -> None:
    settings = defaults(CustomerOccasion.anniversary).model_copy(
        update={"body": "<b>{first_name}</b>\n{years} years"}
    )
    subject, body = render(settings, first_name="<script>\r\nX", years=3)
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "<b>" not in body and "<br>3 years" in body
    assert "\r" not in subject and "\n" not in subject


def test_settings_contract_rejects_out_of_range_schedule() -> None:
    values = defaults(CustomerOccasion.birthday).model_dump()
    with pytest.raises(ValidationError):
        OccasionSettings.model_validate({**values, "send_hour": 24})


def test_review_template_renders_disabled_and_read_only_without_send_actions() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.services.customer_occasion_contracts import (
        OccasionReview,
        OccasionReviewResult,
    )

    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    settings = defaults(CustomerOccasion.anniversary)
    subject, body = render(settings, first_name="Alex", years=3)
    review = OccasionReviewResult(
        now, (OccasionReview(settings, now.date(), 0, 5, (), subject, body),), ()
    )
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"layouts/admin.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
            ]
        ),
        autoescape=select_autoescape(),
    )
    output = env.get_template("admin/notifications/occasions.html").render(
        review=review,
        can_write=False,
        request=SimpleNamespace(state=SimpleNamespace()),
    )
    assert "Disabled" in output and "5 accounts missing" in output
    assert "active and suspended" in output and "No matching customers today" in output
    assert "Send saved message as test" not in output
    assert "Save anniversary settings" not in output


def test_review_forms_use_shared_csrf_contract() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "templates/admin/notifications/occasions.html"
    ).read_text(encoding="utf-8")
    assert source.count('{% include "components/forms/csrf_input.html" %}') == 2
    assert 'name="csrf_token"' not in source
