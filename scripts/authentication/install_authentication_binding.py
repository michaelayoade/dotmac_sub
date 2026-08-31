"""Install one reviewed authentication verifier binding through its owner."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from sqlalchemy.exc import SQLAlchemyError

from app.services.credential_party_binding import (
    AUTHENTICATION_BINDING_INSTALL_SCOPE,
    AuthenticationBindingInstallation,
    install_authentication_binding,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding-key", required=True)
    parser.add_argument("--mechanism-code", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--description")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with db_session_adapter.owner_command_session() as db:
            outcome = install_authentication_binding(
                db,
                AuthenticationBindingInstallation(
                    context=CommandContext.system(
                        actor=args.actor,
                        scope=AUTHENTICATION_BINDING_INSTALL_SCOPE,
                        reason=args.reason,
                        idempotency_key=(
                            f"authentication-binding:{args.binding_key.strip()}"
                        ),
                    ),
                    binding_key=args.binding_key,
                    mechanism_code=args.mechanism_code,
                    name=args.name,
                    description=args.description,
                ),
            )
    except (DomainError, SQLAlchemyError) as exc:
        code = exc.code if isinstance(exc, DomainError) else "database_error"
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": code,
                    "message": "authentication binding installation failed",
                },
                sort_keys=True,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "status": "replayed" if outcome.replayed else "installed",
                "binding_id": str(outcome.binding_id),
                "binding_key": outcome.binding_key,
                "mechanism_code": outcome.mechanism_code,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
