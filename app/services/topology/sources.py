"""Provenance source strings for topology rows.

``RECONCILED_SOURCE`` is the historical provenance string for topology rows
created by the retired Zabbix reconcile. The reconcile itself is gone (native
polling owns reachability now), but existing ``NetworkDevice`` rows keep this
``source`` value forever, so readers still filter on it.
"""

RECONCILED_SOURCE = "zabbix_reconcile"
