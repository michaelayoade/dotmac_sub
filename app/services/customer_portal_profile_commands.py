"""Owned customer portal profile update commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber, SubscriberCategory
from app.schemas.subscriber import (
    SubscriberNotificationPreferencesUpdate,
    SubscriberUpdate,
)
from app.services import customer_profile_location, ncc_location, settings_spec
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.customer_identity_normalization import normalize_phone_identifier
from app.services.customer_identity_resolution import (
    rebuild_identity_index_for_subscriber,
)
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.timezone import APP_TIMEZONE

PORTAL_PROFILE_WRITE_SCOPE = "customer:profile:write"
CUSTOMER_MINIMUM_AGE_SETTING_KEY = "customer_minimum_age_years"

_UPDATE_COMMAND = OwnerCommandDefinition(
    owner="customer.portal_profile_commands",
    concern="customer portal profile update",
    name="update_customer_profile",
)


class CustomerPortalProfileCommandError(DomainError):
    """Stable customer-portal profile command failure."""


@dataclass(frozen=True, slots=True)
class UpdateCustomerProfileCommand:
    context: CommandContext
    subscriber_id: UUID
    first_name: str
    last_name: str
    email: str
    billing_notifications: bool
    sms_updates: bool
    display_name: str | None = None
    phone: str | None = None
    nin: str | None = None
    date_of_birth: str | None = None
    gender: str | None = None
    preferred_contact_method: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    region: str | None = None
    lga: str | None = None
    postal_code: str | None = None
    country_code: str | None = None
    push_notifications: bool = True
    service_notifications: bool = True
    account_notifications: bool = True
    usage_notifications: bool = True
    general_notifications: bool = True
    locale: str | None = None
    enforce_biodata: bool = False


@dataclass(frozen=True, slots=True)
class CustomerProfileUpdateOutcome:
    subscriber_id: UUID
    changed_fields: tuple[str, ...]
    email_changed: bool


@dataclass(frozen=True, slots=True)
class CustomerMinimumAgePolicy:
    """Typed customer DOB policy projected from the authoritative setting."""

    minimum_age_years: int
    as_of_date: date
    latest_allowed_date_of_birth: date


def _fail(code: str, message: str) -> CustomerPortalProfileCommandError:
    return CustomerPortalProfileCommandError(
        code=f"customer.portal_profile_commands.{code}",
        message=message,
    )


def _latest_allowed_date_of_birth(as_of_date: date, minimum_age_years: int) -> date:
    try:
        return as_of_date.replace(year=as_of_date.year - minimum_age_years)
    except ValueError:
        # A 29 February policy date maps to 28 February when the cutoff year
        # is not a leap year.
        return as_of_date.replace(
            year=as_of_date.year - minimum_age_years,
            day=28,
        )


def resolve_customer_minimum_age_policy(
    db: Session,
    *,
    as_of_date: date | None = None,
) -> CustomerMinimumAgePolicy:
    """Resolve the UI and command owner's single minimum-age interpretation."""

    try:
        raw_value = settings_spec.resolve_value(
            db,
            SettingDomain.subscriber,
            CUSTOMER_MINIMUM_AGE_SETTING_KEY,
        )
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise TypeError("minimum customer age must resolve to an integer")
        minimum_age_years = raw_value
    except (TypeError, ValueError) as exc:
        raise _fail(
            "invalid_age_policy",
            "The minimum customer age policy is invalid.",
        ) from exc
    except Exception as exc:
        raise _fail(
            "invalid_age_policy",
            "The minimum customer age policy is unavailable.",
        ) from exc
    if not 0 <= minimum_age_years <= 120:
        raise _fail(
            "invalid_age_policy",
            "The minimum customer age policy is invalid.",
        )
    effective_date = as_of_date or datetime.now(APP_TIMEZONE).date()
    return CustomerMinimumAgePolicy(
        minimum_age_years=minimum_age_years,
        as_of_date=effective_date,
        latest_allowed_date_of_birth=_latest_allowed_date_of_birth(
            effective_date,
            minimum_age_years,
        ),
    )


def _validate_date_of_birth(db: Session, birth_date: date | None) -> None:
    if birth_date is None:
        return
    age_policy = resolve_customer_minimum_age_policy(db)
    if birth_date > age_policy.as_of_date:
        raise _fail(
            "invalid_date_of_birth",
            "Date of birth cannot be in the future.",
        )
    if birth_date > age_policy.latest_allowed_date_of_birth:
        raise _fail(
            "minimum_age_not_met",
            f"Customer must be at least {age_policy.minimum_age_years} years old.",
        )


def _canonical_location(
    country_code_value: str | None,
    region_value: str | None,
    lga_value: str | None,
) -> tuple[str | None, str | None, str | None]:
    raw_country_code = (country_code_value or "").strip()
    country_code = customer_profile_location.canonical_country_code(raw_country_code)
    if raw_country_code and not country_code:
        raise _fail("invalid_country", "Select a country from the available list.")

    region = (region_value or "").strip()
    lga = (lga_value or "").strip()
    if country_code == customer_profile_location.NIGERIA_COUNTRY_CODE:
        if region:
            region = ncc_location.canonical_state(region)
            if not region or region == "INTERNATIONAL":
                raise _fail(
                    "invalid_region", "Select a valid Nigerian state or the FCT."
                )
        if lga and not region:
            raise _fail(
                "invalid_region",
                "Select a Nigerian state before selecting an LGA.",
            )
        if lga:
            lga = ncc_location.canonical_lga(region, lga)
            if not lga:
                raise _fail(
                    "invalid_lga",
                    f"{(lga_value or '').strip()!r} is not a Local Government "
                    f"Area of {region!r}.",
                )
    elif lga:
        raise _fail(
            "invalid_lga", "LGA is available only for Nigerian contact addresses."
        )
    return country_code or None, region or None, lga or None


def _validated_fields(
    command: UpdateCustomerProfileCommand,
) -> SubscriberUpdate:
    country_code, region, lga = _canonical_location(
        command.country_code, command.region, command.lga
    )
    try:
        birth_date = (
            date.fromisoformat(command.date_of_birth.strip())
            if command.date_of_birth and command.date_of_birth.strip()
            else None
        )
    except ValueError as exc:
        raise _fail(
            "invalid_date_of_birth", "Date of birth must be a valid date."
        ) from exc

    fields: dict[str, object] = {
        "first_name": command.first_name.strip(),
        "last_name": command.last_name.strip(),
        "display_name": (command.display_name or "").strip() or None,
        "email": command.email.strip(),
        "phone": normalize_phone_identifier(command.phone),
        "address_line1": (command.address_line1 or "").strip() or None,
        "address_line2": (command.address_line2 or "").strip() or None,
        "city": (command.city or "").strip() or None,
        "region": region,
        "lga": lga,
        "postal_code": (command.postal_code or "").strip() or None,
        "country_code": country_code,
        "locale": (command.locale or "").strip() or None,
        "preferred_contact_method": (
            (command.preferred_contact_method or "").strip() or None
        ),
        "date_of_birth": birth_date,
        "notification_preferences": SubscriberNotificationPreferencesUpdate(
            billing_notifications=command.billing_notifications,
            sms_updates=command.sms_updates,
            push_notifications=command.push_notifications,
            service_notifications=command.service_notifications,
            account_notifications=command.account_notifications,
            usage_notifications=command.usage_notifications,
            general_notifications=command.general_notifications,
        ),
    }
    if command.gender and command.gender.strip():
        fields["gender"] = command.gender.strip()
    fields["nin"] = (command.nin or "").strip() or None

    try:
        validated = SubscriberUpdate.model_validate(fields)
    except Exception as exc:
        raise _fail("invalid_profile", "Some profile details are invalid.") from exc
    return validated


def update_customer_profile(
    db: Session,
    *,
    command: UpdateCustomerProfileCommand,
) -> CustomerProfileUpdateOutcome:
    """Validate and atomically save a customer portal profile update."""

    if command.context.scope != PORTAL_PROFILE_WRITE_SCOPE:
        raise _fail("invalid_scope", "Customer profile write scope is required.")
    fields = _validated_fields(command)

    def operation() -> CustomerProfileUpdateOutcome:
        subscriber: Subscriber | None = db.scalar(
            select(Subscriber)
            .where(Subscriber.id == command.subscriber_id)
            .with_for_update()
        )
        if subscriber is None:
            raise _fail("subscriber_not_found", "Customer account was not found.")

        _validate_date_of_birth(db, fields.date_of_birth)

        nin_locked = bool((subscriber.metadata_ or {}).get("nin_verified"))
        if (
            command.enforce_biodata
            and subscriber.category == SubscriberCategory.residential
        ):
            if fields.date_of_birth is None or fields.gender is None:
                raise _fail(
                    "invalid_biodata",
                    "Date of birth and gender are required to complete your profile.",
                )
            nin_value = fields.nin
            if not nin_locked and not nin_value:
                raise _fail("invalid_biodata", "Enter your 11-digit NIN.")
        submitted_fields = set(fields.model_fields_set)
        if nin_locked and fields.nin != subscriber.nin:
            submitted_fields.discard("nin")

        previous_values = {
            field: getattr(subscriber, field)
            for field in (
                "first_name",
                "last_name",
                "display_name",
                "email",
                "phone",
                "nin",
                "date_of_birth",
                "gender",
                "preferred_contact_method",
                "address_line1",
                "address_line2",
                "city",
                "region",
                "lga",
                "postal_code",
                "country_code",
                "locale",
            )
        }
        email_changed = (subscriber.email or "").casefold() != str(
            fields.email or ""
        ).casefold()

        for field in (
            "first_name",
            "last_name",
            "display_name",
            "email",
            "phone",
            "date_of_birth",
            "gender",
            "preferred_contact_method",
            "address_line1",
            "address_line2",
            "city",
            "region",
            "lga",
            "postal_code",
            "country_code",
            "locale",
            "nin",
        ):
            if field in submitted_fields:
                setattr(subscriber, field, getattr(fields, field))
        if email_changed:
            subscriber.email_verified = False

        metadata = dict(subscriber.metadata_ or {})
        previous_metadata = dict(metadata)
        preference_values = {
            "billing_notifications": command.billing_notifications,
            "sms_updates": command.sms_updates,
            "push_notifications": command.push_notifications,
            "service_notifications": command.service_notifications,
            "account_notifications": command.account_notifications,
            "usage_notifications": command.usage_notifications,
            "general_notifications": command.general_notifications,
        }
        metadata["billing_notifications"] = command.billing_notifications
        metadata["sms_updates"] = command.sms_updates
        metadata["push_notifications"] = command.push_notifications
        metadata["service_notifications"] = command.service_notifications
        metadata["account_notifications"] = command.account_notifications
        metadata["usage_notifications"] = command.usage_notifications
        metadata["general_notifications"] = command.general_notifications
        subscriber.metadata_ = metadata

        changed_fields = tuple(
            sorted(
                field
                for field, previous in previous_values.items()
                if getattr(subscriber, field) != previous
            )
        )
        preferences_changed = any(
            previous_metadata.get(field) != value
            for field, value in preference_values.items()
        )
        if preferences_changed:
            changed_fields = tuple(
                sorted((*changed_fields, "notification_preferences"))
            )
        if email_changed:
            changed_fields = tuple(sorted((*changed_fields, "email_verified")))

        rebuild_identity_index_for_subscriber(db, subscriber.id)
        from app.services import customer_location_requests as location_service

        location_service.geocode_service_address(db, subscriber)
        if changed_fields:
            emit_event(
                db,
                EventType.subscriber_updated,
                {
                    "schema_version": 1,
                    "subscriber_id": str(subscriber.id),
                    "updated_fields": list(changed_fields),
                    "command_id": str(command.context.command_id),
                    "correlation_id": str(command.context.correlation_id),
                },
                actor=command.context.actor,
                subscriber_id=subscriber.id,
            )
            stage_audit_event(
                db,
                actor=AuditActor.user(str(subscriber.id), label=command.context.actor),
                action="portal_profile_update",
                entity_type="subscriber",
                entity_id=str(subscriber.id),
                status_code=200,
                is_success=True,
                metadata={
                    "changed_fields": list(changed_fields),
                    "command_id": str(command.context.command_id),
                    "correlation_id": str(command.context.correlation_id),
                    "source": "customer_portal",
                },
            )
        return CustomerProfileUpdateOutcome(
            subscriber_id=subscriber.id,
            changed_fields=changed_fields,
            email_changed=email_changed,
        )

    outcome = execute_owner_command(
        db,
        definition=_UPDATE_COMMAND,
        context=command.context,
        operation=operation,
    )
    if outcome.email_changed and fields.email:
        try:
            from app.services import auth_flow

            auth_flow.send_email_verification(db, str(command.subscriber_id))
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "profile email verification dispatch failed for subscriber %s",
                command.subscriber_id,
                exc_info=True,
            )
    return outcome
