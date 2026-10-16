"""Pure calendar and template value operations used by the occasion owner."""

from calendar import isleap
from datetime import date
from html import escape
from string import Formatter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.customer_occasion_contracts import CustomerOccasion, OccasionSettings
from app.services.domain_errors import DomainError


def defaults(occasion: CustomerOccasion) -> OccasionSettings:
    birthday = occasion == CustomerOccasion.birthday
    return OccasionSettings(
        occasion=occasion,
        subject="Happy birthday, {first_name}!"
        if birthday
        else "Celebrating {years} years with you",
        body=(
            "Happy birthday, {first_name}! Everyone at Dotmac wishes you a wonderful day."
            if birthday
            else "Hello {first_name}, thank you for being with Dotmac for {years} years. We appreciate you!"
        ),
    )


def validate(settings: OccasionSettings) -> None:
    try:
        ZoneInfo(settings.timezone)
        for template in (settings.subject, settings.body):
            if not template.strip():
                raise ValueError("Message cannot be empty")
            for _, name, spec, conversion in Formatter().parse(template):
                if name is not None and (
                    name not in {"first_name", "years"} or spec or conversion
                ):
                    raise ValueError("Use only {first_name} and {years} placeholders")
        if "\n" in settings.subject or "\r" in settings.subject:
            raise ValueError("Subject must be a single line")
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise DomainError(
            code="communications.customer_occasions.invalid_command",
            message="Invalid timezone or message placeholders.",
            retryable=False,
        ) from exc


def occasion_years(*, original: date, today: date) -> int | None:
    if original > today:
        return None
    day = (
        28
        if original.month == 2 and original.day == 29 and not isleap(today.year)
        else original.day
    )
    return (
        today.year - original.year
        if (original.month, day) == (today.month, today.day)
        else None
    )


def render(
    settings: OccasionSettings, *, first_name: str, years: int
) -> tuple[str, str]:
    validate(settings)
    values = {"first_name": first_name or "there", "years": str(years)}
    subject = settings.subject.format(**values).replace("\r", " ").replace("\n", " ")
    body = "<p>" + escape(settings.body.format(**values)).replace("\n", "<br>") + "</p>"
    return subject[:200], body
