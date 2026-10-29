"""Every ``out_of_band_evidence`` service is bound to its own approving ADR.

``TransactionMode.OUT_OF_BAND_EVIDENCE`` exempts an observation collector from
the "join the caller's transaction, declare domain errors and events" writer
rules. ``contract_validation_errors`` only checks that SOME ``docs/adr/`` path
is cited, which any service could satisfy by citing an unrelated ADR. This
ratchet makes the premise enforceable (ADR-0018: an exemption states an
enforceable premise): the set of services using the mode must equal the
approved list below, in both directions, and each approving ADR must be cited by
that service and must itself name the service and the mode.

Adding a new out-of-band evidence writer therefore requires, in one reviewed
change: an ADR that names it, a contract citing that ADR, and an entry here.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from app.services.sot_manifest import TransactionMode

ROOT = Path(__file__).resolve().parents[2]

#: Service name -> the ADR that approves its out-of-band evidence write.
APPROVED_OUT_OF_BAND_EVIDENCE: Mapping[str, str] = {
    "access.enforcement_evidence": "docs/adr/0017-enforcement-application-evidence.md",
    "network.coa_outcome_evidence": "docs/adr/0018-coa-outcome-evidence.md",
    "network.olt_ssh_host_key_evidence": "docs/adr/0033-olt-ssh-host-key-evidence.md",
}

#: Older out-of-band writers that predate the mode and are NOT migrated to it
#: (ADR 0017 follow-up): the prepaid review item re-raises on write failure and
#: is a work item rather than an observation; the MikroTik auth attempt writes
#: the shared ProvisioningLog. Listed so they are known, not silently exempt.
KNOWN_LEGACY_OUT_OF_BAND_WRITERS: tuple[str, ...] = (
    "app/services/prepaid_service_renewals.py::_record_review_item_out_of_band",
    "app/services/nas/_mikrotik.py::_record_mikrotik_auth_attempt",
    # Found by tests/architecture/test_undeclared_out_of_band_writers.py: a
    # retried, independent-connection staff-alert summary write that copies the
    # review-item technique. Not an observation collector; listed, not migrated.
    "app/services/prepaid_service_renewals.py::_finalize_scheduled_renewal_summary",
    # A nested ONT/CPE helper that commits the detected TR-069 data-model root
    # on its own session while its caller may hold the same device row.
    "app/services/network/ont_action_common.py::persist_data_model_root",
    # Notification delivery: called from
    # app/services/operational_escalation_delivery.py's _send_to_target(db, ...)
    # chain, which holds `db: Session` open across the call. Each opens its
    # OWN session via db_session_adapter.session() and commits a delivery
    # record mid-flow while that caller session may still be open.
    "app/services/notification_adapter.py::EmailProvider.send",
    "app/services/notification_adapter.py::SmsProvider.send",
)

_MODE = TransactionMode.OUT_OF_BAND_EVIDENCE.value


@dataclass(frozen=True)
class ServiceView:
    name: str
    mode: str | None
    design_refs: tuple[str, ...]


def ratchet_violations(
    services: Iterable[ServiceView],
    approved: Mapping[str, str],
    read_text: Callable[[str], str | None],
) -> list[str]:
    """Pure check, so the planted proofs below run the same logic."""
    violations: list[str] = []
    using = {service.name: service for service in services if service.mode == _MODE}

    for name in sorted(set(using) - set(approved)):
        violations.append(
            f"{name} uses {_MODE} but has no approving ADR in "
            "APPROVED_OUT_OF_BAND_EVIDENCE"
        )
    for name in sorted(set(approved) - set(using)):
        violations.append(
            f"{name} is approved for {_MODE} but does not use it; remove the entry"
        )
    for name in sorted(set(using) & set(approved)):
        adr = approved[name]
        if adr not in using[name].design_refs:
            violations.append(f"{name} does not cite its approving ADR {adr}")
        text = read_text(adr)
        if text is None:
            violations.append(f"approving ADR {adr} for {name} does not exist")
            continue
        if name not in text or _MODE not in text:
            violations.append(
                f"approving ADR {adr} must name both {name!r} and {_MODE!r}"
            )
    return violations


def _read_repo_text(relative_path: str) -> str | None:
    path = ROOT / relative_path
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _declares_qualified_function(source: str, qualified_name: str) -> bool:
    """Return whether ``source`` declares a function or class method by name."""
    nodes: list[ast.stmt] = ast.parse(source).body
    *class_names, function_name = qualified_name.split(".")
    for class_name in class_names:
        matching_class = next(
            (
                node
                for node in nodes
                if isinstance(node, ast.ClassDef) and node.name == class_name
            ),
            None,
        )
        if matching_class is None:
            return False
        nodes = matching_class.body
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
        for node in nodes
    )


def _registry_views() -> list[ServiceView]:
    from app.services.sot_registry.registry import all_services

    views = []
    for service in all_services():
        contract = service.contract
        views.append(
            ServiceView(
                name=service.name,
                mode=contract.transaction.mode.value if contract else None,
                design_refs=tuple(contract.design_refs) if contract else (),
            )
        )
    return views


def test_every_out_of_band_evidence_service_is_bound_to_its_approving_adr() -> None:
    violations = ratchet_violations(
        _registry_views(), APPROVED_OUT_OF_BAND_EVIDENCE, _read_repo_text
    )
    assert not violations, "\n".join(violations)


def test_known_legacy_out_of_band_writers_still_exist() -> None:
    """If a legacy writer is migrated or removed, update the list.

    One-directional by design: this does not detect a NEW out-of-band writer
    that skips the mode. Partial detection is
    ``tests/architecture/test_undeclared_out_of_band_writers.py``: an AST
    scan across all of app/, ratcheted two-directionally against an approved
    and a classified baseline set, for a function that in its own body opens
    and commits a session via ``create_session()``, a module-level alias of
    it, ``from app.db import SessionLocal`` / any module-level
    ``sessionmaker(...)``-bound factory, or that enters one of the
    auto-committing ``with db_session_adapter.session()`` /
    ``advisory_lock()`` / ``task_session()`` context managers (no visible
    ``.commit()`` needed -- they commit on exit; ``task_session`` is
    recognised whether imported at module level or, as at every real call
    site, function-locally). ``owner_command_session()`` and
    ``read_session()`` never commit and are correctly never flagged. It does
    NOT yet recognise a function-local (rather than module-level)
    ``SessionLocal``/``sessionmaker`` alias, or a session opened in one
    function and committed in another; see that module's stated limitations.
    """
    for entry in KNOWN_LEGACY_OUT_OF_BAND_WRITERS:
        module, function = entry.split("::")
        text = _read_repo_text(module)
        assert text is not None, f"{module} no longer exists; update the list"
        assert _declares_qualified_function(text, function), (
            f"{entry} no longer exists; update the list"
        )


class TestRatchetSensitivity:
    """A guard that never fires proves nothing: plant each violation."""

    _ADR = "docs/adr/9999-example.md"

    def _reader(self, text: str | None) -> Callable[[str], str | None]:
        return lambda path: text if path == self._ADR else None

    def test_an_unlisted_service_using_the_mode_is_flagged(self) -> None:
        services = [ServiceView("example.sneaky", _MODE, (self._ADR,))]
        violations = ratchet_violations(services, {}, self._reader("x"))
        assert any("has no approving ADR" in v for v in violations)

    def test_a_listed_service_that_stopped_using_the_mode_is_flagged(self) -> None:
        services = [ServiceView("example.gone", "owner_managed", (self._ADR,))]
        violations = ratchet_violations(
            services, {"example.gone": self._ADR}, self._reader("x")
        )
        assert any("does not use it" in v for v in violations)

    def test_citing_an_unrelated_adr_is_flagged(self) -> None:
        services = [ServiceView("example.evidence", _MODE, ("docs/adr/0002-other.md",))]
        violations = ratchet_violations(
            services,
            {"example.evidence": self._ADR},
            self._reader(f"example.evidence uses {_MODE}"),
        )
        assert any("does not cite its approving ADR" in v for v in violations)

    def test_an_adr_that_does_not_name_the_service_and_mode_is_flagged(self) -> None:
        services = [ServiceView("example.evidence", _MODE, (self._ADR,))]
        violations = ratchet_violations(
            services, {"example.evidence": self._ADR}, self._reader("unrelated text")
        )
        assert any("must name both" in v for v in violations)

    def test_a_correct_binding_passes(self) -> None:
        services = [ServiceView("example.evidence", _MODE, (self._ADR,))]
        violations = ratchet_violations(
            services,
            {"example.evidence": self._ADR},
            self._reader(f"The example.evidence service uses {_MODE}."),
        )
        assert violations == []
