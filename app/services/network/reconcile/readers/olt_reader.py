"""OLT-side reader — SSH-driven observation of a single ONT.

Composes existing OLT helpers in ``app.services.network.olt_ssh_ont`` to fill
the ``OltObservedFields`` dataclass. Each call opens (typically) one SSH
session per query; batching multiple ``display`` commands over a shared
session is a follow-up optimisation.

Current scope:

* Presence + run/match state via ``adapter.find_ont_by_serial`` and
  ``status.get_ont_status``. These are enough for the planner to decide
  whether the ONT exists in the OLT's table and is alive on the PON.
* Other observed fields (description, profile bindings, optical levels,
  mgmt IP, service-ports) are stubbed ``None`` for now — they need richer
  parsing of ``display ont info`` / ``display ont optical-info`` /
  ``display service-port port`` output. The planner can still produce a
  useful plan with the partial observation: missing fields generate drift,
  which the applier writes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.services.network.olt_ssh_ont.status import (
    get_ont_info_detail,
    get_ont_status,
)

from ..state import OltObservedFields, OntDesiredState
from ._types import ReadResult

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)


# Reachability errors we treat as "couldn't contact the OLT" — anything other
# than these counts as a parseable-but-bad reply.
_UNREACHABLE_FRAGMENTS = (
    "connection failed",
    "timeout",
    "no route to host",
    "connection refused",
    "unable to connect",
)


def read_olt_state(
    adapter: Any,
    desired: OntDesiredState,
    *,
    deadline: datetime | None = None,
) -> ReadResult[OltObservedFields]:
    """Read the OLT-observed fields for one ONT.

    Args:
        adapter: An ``OltProtocolAdapter`` (or any object with the same
            ``find_ont_by_serial`` + ``olt`` attribute surface). The reader
            uses its ``.olt`` for the SSH parameters and its
            ``find_ont_by_serial`` for the presence check.
        desired: The ONT's desired state. Used to resolve ``fsp`` and
            ``olt_ont_id`` for the per-ONT queries.
        deadline: Optional cutoff. Honored at the entry point only — the
            existing SSH helpers don't accept a deadline themselves, so a
            single late call may still take its full configured timeout.
            ``reconcile_ont`` enforces the outer budget.

    Returns:
        A ``ReadResult[OltObservedFields]``.
    """
    olt = getattr(adapter, "olt", None) or getattr(adapter, "_olt", None)
    if olt is None:
        return ReadResult(
            success=False,
            unreachable=False,
            observed=None,
            error="OLT adapter has no .olt attribute",
        )

    # 1. Presence check via serial. Cheaper than display-ont-info and works
    # regardless of whether we already know the ONT-ID.
    find = adapter.find_ont_by_serial(desired.serial_number)
    if not find.success:
        if _looks_unreachable(find.message):
            return ReadResult(
                success=False,
                unreachable=True,
                observed=None,
                error=find.message,
            )
        return ReadResult(
            success=False,
            unreachable=False,
            observed=None,
            error=find.message,
        )

    registration = (find.data or {}).get("registration") if find.data else None
    if registration is None:
        # OLT confirms the ONT is not registered. That's a clean read with
        # ``olt_present=False`` — the planner will plan to add it.
        return ReadResult(
            success=True,
            unreachable=False,
            observed=_absent_fields(),
            error=None,
        )

    # 2. Fetch run/match state for the registered ONT. Some details
    # (description, profiles, optical) require richer parsing not yet wired.
    ok, msg, status_entry = get_ont_status(
        olt, desired.fsp, desired.olt_ont_id
    )
    if not ok:
        if _looks_unreachable(msg):
            return ReadResult(
                success=False,
                unreachable=True,
                observed=None,
                error=msg,
            )
        # Treat as present-but-status-unknown rather than a hard failure: the
        # planner can still reason about presence even when run-state is dark.
        logger.debug("olt_reader_status_unavailable", extra={"message": msg})
        return ReadResult(
            success=True,
            unreachable=False,
            observed=_present_with_unknown_state(),
            error=None,
        )

    # 3. Fetch richer ONT info — description, profile ids, mgmt IP, mgmt VLAN,
    # distance. This is a second SSH session (a follow-up could batch with the
    # status query). If it fails, fall back to status-only observation rather
    # than the whole read failing — partial data is still useful to the planner.
    detail_ok, detail_msg, detail = get_ont_info_detail(
        olt, desired.fsp, desired.olt_ont_id
    )
    if not detail_ok and _looks_unreachable(detail_msg):
        return ReadResult(
            success=False,
            unreachable=True,
            observed=None,
            error=detail_msg,
        )
    detail = detail or {}

    return ReadResult(
        success=True,
        unreachable=False,
        observed=OltObservedFields(
            olt_present=True,
            olt_match_state=_normalise_state(
                status_entry.match_state if status_entry else None,
                allowed={"match", "mismatch", "initial"},
            ),
            olt_run_state=_normalise_state(
                status_entry.run_state if status_entry else None,
                allowed={"online", "offline", "los"},
            ),
            olt_distance_m=_int_or_none(detail.get("distance_m")),
            # Optical levels (rx_dbm/tx_dbm/temp) come from
            # ``display ont optical-info`` — separate SSH function, deferred.
            olt_rx_dbm=None,
            olt_tx_dbm=None,
            olt_temperature_c=None,
            olt_description=_str_or_none(detail.get("description")),
            olt_mgmt_ip=_str_or_none(detail.get("mgmt_ip")),
            olt_mgmt_vlan=_int_or_none(detail.get("mgmt_vlan")),
            olt_line_profile_id=_int_or_none(detail.get("line_profile_id")),
            olt_service_profile_id=_int_or_none(
                detail.get("service_profile_id")
            ),
            # Service-port enumeration is a third SSH function
            # (``display service-port port <fsp>`` + per-row filtering on
            # ONT-ID), deferred.
            olt_service_ports=(),
        ),
        error=None,
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _absent_fields() -> OltObservedFields:
    """Field set for an ONT that the OLT does not have registered."""
    return OltObservedFields(
        olt_present=False,
        olt_match_state=None,
        olt_run_state=None,
        olt_distance_m=None,
        olt_rx_dbm=None,
        olt_tx_dbm=None,
        olt_temperature_c=None,
        olt_description=None,
        olt_mgmt_ip=None,
        olt_mgmt_vlan=None,
        olt_line_profile_id=None,
        olt_service_profile_id=None,
        olt_service_ports=(),
    )


def _present_with_unknown_state() -> OltObservedFields:
    """ONT is registered but we couldn't fetch run/match state.

    Conservative: report present=True so the planner doesn't try to re-add
    the ONT, but leave run/match as None so the planner avoids inferring
    anything about its liveness.
    """
    return OltObservedFields(
        olt_present=True,
        olt_match_state=None,
        olt_run_state=None,
        olt_distance_m=None,
        olt_rx_dbm=None,
        olt_tx_dbm=None,
        olt_temperature_c=None,
        olt_description=None,
        olt_mgmt_ip=None,
        olt_mgmt_vlan=None,
        olt_line_profile_id=None,
        olt_service_profile_id=None,
        olt_service_ports=(),
    )


def _int_or_none(value: Any) -> int | None:
    """Coerce a parser dict value to ``int``, tolerating None / empty / strings."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    """Filter out empty/dash placeholders Huawei emits for "no value"."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == "-":
        return None
    return text


def _normalise_state(raw: str | None, *, allowed: set[str]) -> str | None:
    """Reduce vendor state strings to the canonical set we record.

    Huawei sometimes reports ``"online"`` lowercase, sometimes ``"ONLINE"``;
    ``"normal"`` is treated as ``"online"`` (legacy) ... but be conservative —
    if we don't recognise the value, return None rather than guessing.
    """
    if raw is None:
        return None
    text = raw.strip().lower()
    if text in allowed:
        return text
    if text == "normal" and "online" in allowed:
        # Some Huawei firmwares report "normal" for a healthy ONT.
        return "online"
    return None


def _looks_unreachable(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return any(frag in lowered for frag in _UNREACHABLE_FRAGMENTS)
