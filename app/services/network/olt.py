"""OLT infrastructure services.

Transaction Policy:
- Service methods commit their own transactions via db.commit()
- Use db.flush() when creating entities that need IDs for related operations
- Use db.begin_nested() for operations requiring partial rollback capability
- Routes must NOT call db.commit() (per CLAUDE.md)
"""

from __future__ import annotations

from app.services.network._common import SubscriberValidator
from app.services.network.olt_device_crud import OLTDevices
from app.services.network.olt_hardware_crud import (
    OltCardPorts,
    OltCards,
    OltPowerUnits,
    OltSfpModules,
    OltShelves,
)
from app.services.network.ont_assignment_crud import OntAssignments
from app.services.network.ont_crud import OntUnits
from app.services.network.pon_crud import PonPorts


def _default_subscriber_validator() -> SubscriberValidator | None:
    """Soft-import the subscriber bridge validator.

    The network package must not import the subscriber model directly, but
    callers running in the full subscription-enabled deployment want the
    validator wired in automatically. We import the bridge lazily so the
    network package stays importable in standalone deployments where the
    bridge (or the subscriber model) is absent.
    """
    try:
        from app.services.network_subscriber_bridge import (
            default_subscriber_validator,
        )
    except ImportError:  # pragma: no cover - standalone deployments
        return None
    return default_subscriber_validator


_validator = _default_subscriber_validator()

olt_devices = OLTDevices()
pon_ports = PonPorts()
ont_units = OntUnits(subscriber_validator=_validator)
ont_assignments = OntAssignments(subscriber_validator=_validator)
olt_shelves = OltShelves()
olt_cards = OltCards()
olt_card_ports = OltCardPorts()
olt_power_units = OltPowerUnits()
olt_sfp_modules = OltSfpModules()
