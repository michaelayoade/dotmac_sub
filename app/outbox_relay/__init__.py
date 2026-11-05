"""Outbox relay dispatcher for the composed-module tenant outbox (SUB-2).

A separate process that drains ``public.outbox_events`` through the two
SECURITY DEFINER functions installed by migration ``557_outbox_relay_prereq``
(``claim_outbox_batch`` / ``settle_outbox_event``) using the kernel's retry
policy in ``dotmac_kernel.messaging.relay``.

This is NOT Sub's existing business-event outboxes (``event_store``, owner
output, notification, network-operation, field-ERP); those keep their own
dispatchers. The relay carries composed-module events only and owns no
business decision: it routes an event to a registered handler, or parks it.

Only the tenant plane is run. ``app.migration_bindings.ASSEMBLY_MODULE_PLANES``
selects ``ModulePlane.TENANT`` for every composed dual-plane module, so nothing
enqueues to ``platform_outbox_events`` and no platform dispatcher exists.
"""
