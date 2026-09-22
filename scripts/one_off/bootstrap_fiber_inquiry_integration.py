"""Idempotently install the signed Fiber website inquiry receiver.

The signing secret is referenced, never stored or printed by this command.
Provision the referenced secret first, then run with ``--apply``::

    python -m scripts.one_off.bootstrap_fiber_inquiry_integration \
      --apply --environment sandbox
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.integration_platform import IntegrationInstallation
from app.services.integrations import installations
from app.services.integrations.runtime import ValidationResult
from app.services.integrations.runtime_execution import (
    build_execution_context,
    validate_connection,
)

CONNECTOR_KEY = "fiber.inquiry.http"
CAPABILITY_ID = "communications.fiber_inquiry.receive.v1"
DEFAULT_SECRET_REF = "bao://secret/integrations/fiber_inquiry#webhook_signing_secret"
WEBHOOK_PATH_PREFIX = "/api/v1/webhooks/fiber-inquiry"
ACTOR = "one-off:bootstrap-fiber-inquiry-integration"


class BootstrapMode(StrEnum):
    prepare = "prepare"
    apply = "apply"


class InstallationEnvironment(StrEnum):
    production = "production"
    sandbox = "sandbox"
    test = "test"


@dataclass(frozen=True, slots=True)
class FiberInquiryBootstrapCommand:
    mode: BootstrapMode
    environment: InstallationEnvironment
    name: str
    secret_ref: str


@dataclass(frozen=True, slots=True)
class FiberInquiryBootstrapResult:
    installation_id: UUID
    binding_id: UUID
    installation_state: str
    binding_state: str
    connector_version: str


def fiber_inquiry_callback_path(binding_id: UUID) -> str:
    """Return the public route path for one capability binding."""

    return f"{WEBHOOK_PATH_PREFIX}/{binding_id}"


def _configure(
    db: Session,
    *,
    command: FiberInquiryBootstrapCommand,
) -> FiberInquiryBootstrapResult:
    installations_found = tuple(
        db.scalars(
            select(IntegrationInstallation)
            .where(
                IntegrationInstallation.connector_key == CONNECTOR_KEY,
                IntegrationInstallation.state != "retired",
            )
            .order_by(IntegrationInstallation.created_at.asc())
        ).all()
    )
    if len(installations_found) > 1:
        raise installations.InstallationError(
            "multiple non-retired Fiber inquiry installations require operator review"
        )
    installation = (
        installations_found[0]
        if installations_found
        else installations.create_draft(
            db,
            connector_key=CONNECTOR_KEY,
            name=command.name,
            environment=command.environment.value,
            actor=ACTOR,
        )
    )
    if installation.environment != command.environment.value:
        raise installations.InstallationError(
            "existing Fiber inquiry installation belongs to a different environment"
        )

    installations.create_config_revision(
        db,
        installation_id=installation.id,
        config={"site_id": "fiber.dotmac.ng"},
        secret_refs={"webhook_signing_secret": command.secret_ref},
        actor=ACTOR,
    )
    binding = installations.bind_capability(
        db,
        installation_id=installation.id,
        capability_id=CAPABILITY_ID,
        policy={"default": True},
        actor=ACTOR,
    )
    static_result = installations.validate_static(
        db,
        installation_id=installation.id,
        actor=ACTOR,
    )
    if not static_result.valid:
        raise installations.InstallationError(
            "Fiber inquiry static validation failed: "
            + ",".join(static_result.error_codes)
        )

    if command.mode is BootstrapMode.apply:
        runtime = build_execution_context(
            db,
            capability_binding_id=binding.id,
            allow_disabled=True,
        )
        connection_result = validate_connection(runtime)
        if not connection_result.valid:
            raise installations.InstallationError(
                "Fiber inquiry connection validation failed: "
                + ",".join(connection_result.error_codes)
            )
        installations.enable_after_connection_validation(
            db,
            installation_id=installation.id,
            connection_result=ValidationResult(valid=True),
            actor=ACTOR,
        )

    return FiberInquiryBootstrapResult(
        installation_id=installation.id,
        binding_id=binding.id,
        installation_state=installation.state,
        binding_state=binding.state,
        connector_version=installation.connector_version,
    )


def configure_fiber_inquiry_installation(
    db: Session,
    *,
    command: FiberInquiryBootstrapCommand,
) -> FiberInquiryBootstrapResult:
    """Configure the installation in one installation-owned transaction."""

    return installations.execute_command(
        db,
        lambda: _configure(db, command=command),
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--environment",
        choices=tuple(member.value for member in InstallationEnvironment),
        default=InstallationEnvironment.sandbox.value,
    )
    parser.add_argument("--name", default="Fiber Website Inquiry")
    parser.add_argument("--secret-ref", default=DEFAULT_SECRET_REF)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if not args.prepare and not args.apply:
        print(
            "DRY RUN: would configure fiber.inquiry.http 1.1.0 with "
            f"{CAPABILITY_ID}. Run again with --prepare or --apply."
        )
        return 0

    from app.db import SessionLocal

    command = FiberInquiryBootstrapCommand(
        mode=BootstrapMode.apply if args.apply else BootstrapMode.prepare,
        environment=InstallationEnvironment(args.environment),
        name=str(args.name).strip(),
        secret_ref=str(args.secret_ref).strip(),
    )
    with SessionLocal() as db:
        result = configure_fiber_inquiry_installation(db, command=command)
    print(
        f"{result.installation_state.title()} installation "
        f"{result.installation_id} ({result.connector_version})"
    )
    print(f"{CAPABILITY_ID}: {result.binding_id} [{result.binding_state}]")
    print(f"Webhook path: {fiber_inquiry_callback_path(result.binding_id)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
