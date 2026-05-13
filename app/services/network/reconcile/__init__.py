"""Reconciler package for OLT + ACS state.

The reconciler is the only subsystem that writes to OLT (SSH) and ACS
(GenieACS NBI). It enforces ``OntDesiredState`` onto the live OLT and ACS, and
records the result as ``OntObservedState``.

This first commit lands the type system and the validator only. The reader,
planner, applier, and ``reconcile_ont`` entry point follow in subsequent
commits. No production code path yet imports this package.

Public surface:

* ``OntDesiredState``      — what the operator/system wants.
* ``OntObservedState``     — last-seen live values from OLT + ACS.
* ``ReconcileResult``      — outcome of one reconcile pass.
* ``ReconcileFailure``     — failure detail attached to a non-success result.
* ``ReconcileFailureReason`` — string constants enumerating failure modes.
* ``SyncStatus``           — per-ONT enum literal.
* ``validate_desired``     — boundary validation for a proposed mutation.
* ``Validation``           — validation result type.

The action types (OltAction / AcsAction subclasses), the planner, and the
applier are deliberately not re-exported here; they're implementation details
of the reconciler and consumed only within this package.
"""

from .adapters import (
    apply_proposed_change,
    desired_from_ont_unit,
    observed_from_ont_observation,
    upsert_ont_observation,
)
from .locking import LockConflict, LockError, OntNotFound, acquire_reconcile_lock
from .readers import ReadResult, read_acs_state, read_olt_state
from .state import (
    AcsObservedFields,
    AppliedAction,
    Drift,
    ObserveSurface,
    OltObservedFields,
    OntDesiredState,
    OntObservedState,
    PppoeProvisioningMethod,
    ReconcileFailure,
    ReconcileFailureReason,
    ReconcileMode,
    ReconcileResult,
    SyncStatus,
    WanMode,
    WriteSurface,
)
from .validator import Validation, validate_desired

__all__ = (
    "AcsObservedFields",
    "AppliedAction",
    "Drift",
    "LockConflict",
    "LockError",
    "ObserveSurface",
    "OltObservedFields",
    "OntDesiredState",
    "OntNotFound",
    "OntObservedState",
    "PppoeProvisioningMethod",
    "ReadResult",
    "ReconcileFailure",
    "ReconcileFailureReason",
    "ReconcileMode",
    "ReconcileResult",
    "SyncStatus",
    "Validation",
    "WanMode",
    "WriteSurface",
    "acquire_reconcile_lock",
    "apply_proposed_change",
    "desired_from_ont_unit",
    "observed_from_ont_observation",
    "read_acs_state",
    "read_olt_state",
    "upsert_ont_observation",
    "validate_desired",
)
