"""Every staff-facing API route must declare an authorization guard.

The router-mount mode "user" only attaches ``require_user_auth`` — it proves
the caller is *some* authenticated principal, INCLUDING a customer subscriber.
A staff/admin endpoint mounted that way with no ``require_permission`` is open
to any logged-in customer. This test reconstructs the FULL production API
surface — both the core routers (mounted at import) and the DEFERRED routers
(mounted in the lifespan startup, e.g. settings, qualification) — and fails
the build on any ``/api/v1`` route that lacks a permission/role guard, unless
its path is on the self-scoped allowlist below.

Building from the spec lists (not the live ``app.routes``) is deliberate: the
deferred routers are NOT on ``app`` until startup runs, so an ``app.routes``
walk would silently skip exactly the routers most likely to be misconfigured.

RBAC-coverage counterpart of ``test_thin_wrappers``: makes the "forgot the
permission dependency" class of bug build-failing rather than shippable.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.main import (
    _CORE_ROUTER_SPECS,
    _DEFERRED_API_ROUTER_SPECS,
    _load_router_object,
    _mount_router,
)

# Dependency callables that constitute a real authorization guard.
_GUARD_NAMES = {
    "_require_permission",
    "_require_role",
    "_require_any_permission",
    "_require_method_permission",
    "require_audit_auth",
    "require_scoped_permission",  # forward-compat (P1)
    "_require_scoped_permission",
}

# Self-scoped / public surfaces that legitimately need no staff permission.
_ALLOWLIST_PREFIXES = (
    "/api/v1/me",
    "/api/v1/reseller",
    "/api/v1/payment-proofs/me",
    "/api/v1/payment-proofs/reseller",
    "/api/v1/service-requests",
    "/api/v1/auth",
    "/api/v1/health",
    "/api/v1/tables",
)

# Quarantine: routes already unguarded when this rule was introduced
# (2026-06-10). The test fails on any route NOT in this set, so NEW holes are
# build-failing immediately. This list is the burn-down backlog — guarding a
# router removes its paths from here. Do not ADD to it.
_KNOWN_UNGUARDED = {
    "DELETE /api/v1/alert-notification-policies/{policy_id}",
    "DELETE /api/v1/alert-notification-policy-steps/{step_id}",
    "DELETE /api/v1/analytics/kpi-configs/{config_id}",
    "DELETE /api/v1/comms/surveys/{survey_id}",
    "DELETE /api/v1/connectors/configs/{config_id}",
    "DELETE /api/v1/external-references/{ref_id}",
    "DELETE /api/v1/gis/areas/{area_id}",
    "DELETE /api/v1/gis/layers/{layer_id}",
    "DELETE /api/v1/gis/locations/{location_id}",
    "DELETE /api/v1/integrations/jobs/{job_id}",
    "DELETE /api/v1/integrations/targets/{target_id}",
    "DELETE /api/v1/nas/devices/{device_id}",
    "DELETE /api/v1/nas/templates/{template_id}",
    "DELETE /api/v1/notification-deliveries/{delivery_id}",
    "DELETE /api/v1/notification-templates/{template_id}",
    "DELETE /api/v1/notifications/{notification_id}",
    "DELETE /api/v1/on-call-rotation-members/{member_id}",
    "DELETE /api/v1/on-call-rotations/{rotation_id}",
    "DELETE /api/v1/qualification/buildout-milestones/{milestone_id}",
    "DELETE /api/v1/qualification/buildout-projects/{project_id}",
    "DELETE /api/v1/qualification/coverage-areas/{area_id}",
    "DELETE /api/v1/webhooks/endpoints/{endpoint_id}",
    "DELETE /api/v1/webhooks/subscriptions/{subscription_id}",
    "DELETE /api/v1/wireguard/peers/{peer_id}",
    "DELETE /api/v1/wireguard/servers/{server_id}",
    "GET /api/v1/alert-notification-logs",
    "GET /api/v1/alert-notification-policies",
    "GET /api/v1/alert-notification-policies/{policy_id}",
    "GET /api/v1/alert-notification-policy-steps",
    "GET /api/v1/alert-notification-policy-steps/{step_id}",
    "GET /api/v1/analytics/kpi-aggregates",
    "GET /api/v1/analytics/kpi-aggregates/{aggregate_id}",
    "GET /api/v1/analytics/kpi-configs",
    "GET /api/v1/analytics/kpi-configs/{config_id}",
    "GET /api/v1/analytics/kpis",
    "GET /api/v1/bandwidth/live/{subscription_id}",
    "GET /api/v1/bandwidth/my/live",
    "GET /api/v1/bandwidth/my/series",
    "GET /api/v1/bandwidth/my/stats",
    "GET /api/v1/bandwidth/series/{subscription_id}",
    "GET /api/v1/bandwidth/stats/{subscription_id}",
    "GET /api/v1/bandwidth/top-users",
    "GET /api/v1/comms/customer-notifications",
    "GET /api/v1/comms/customer-notifications/{event_id}",
    "GET /api/v1/comms/eta-updates",
    "GET /api/v1/comms/eta-updates/{update_id}",
    "GET /api/v1/comms/survey-responses",
    "GET /api/v1/comms/survey-responses/{response_id}",
    "GET /api/v1/comms/surveys",
    "GET /api/v1/comms/surveys/{survey_id}",
    "GET /api/v1/connectors/configs",
    "GET /api/v1/connectors/configs/{config_id}",
    "GET /api/v1/defaults/currency",
    "GET /api/v1/defaults/customer/{customer_type}",
    "GET /api/v1/defaults/invoice",
    "GET /api/v1/defaults/subscription",
    "GET /api/v1/external-references",
    "GET /api/v1/external-references/{ref_id}",
    "GET /api/v1/fiber-plant/closures/{closure_id}/splices",
    "GET /api/v1/fiber-plant/fdh-cabinets/{fdh_id}/splitters",
    "GET /api/v1/fiber-plant/geojson",
    "GET /api/v1/fiber-plant/stats",
    "GET /api/v1/files/{file_id}/download",
    "GET /api/v1/gis/areas",
    "GET /api/v1/gis/areas/containing-point",
    "GET /api/v1/gis/areas/{area_id}",
    "GET /api/v1/gis/areas/{area_id}/contains-point",
    "GET /api/v1/gis/coverage-check",
    "GET /api/v1/gis/elevation",
    "GET /api/v1/gis/layers",
    "GET /api/v1/gis/layers/{layer_id}",
    "GET /api/v1/gis/layers/{layer_key}/feature-collection",
    "GET /api/v1/gis/layers/{layer_key}/features",
    "GET /api/v1/gis/locations",
    "GET /api/v1/gis/locations/in-area/{area_id}",
    "GET /api/v1/gis/locations/nearby",
    "GET /api/v1/gis/locations/{location_id}",
    "GET /api/v1/gis/subscriber-locations",
    "GET /api/v1/integrations/jobs",
    "GET /api/v1/integrations/jobs/{job_id}",
    "GET /api/v1/integrations/runs",
    "GET /api/v1/integrations/runs/{run_id}",
    "GET /api/v1/integrations/targets",
    "GET /api/v1/integrations/targets/{target_id}",
    "GET /api/v1/nas/backup-methods",
    "GET /api/v1/nas/backups/compare",
    "GET /api/v1/nas/backups/{backup_id}",
    "GET /api/v1/nas/backups/{backup_id}/content",
    "GET /api/v1/nas/connection-types",
    "GET /api/v1/nas/devices",
    "GET /api/v1/nas/devices/stats",
    "GET /api/v1/nas/devices/{device_id}",
    "GET /api/v1/nas/devices/{device_id}/backups",
    "GET /api/v1/nas/devices/{device_id}/logs",
    "GET /api/v1/nas/logs",
    "GET /api/v1/nas/logs/{log_id}",
    "GET /api/v1/nas/provisioning-actions",
    "GET /api/v1/nas/templates",
    "GET /api/v1/nas/templates/{template_id}",
    "GET /api/v1/nas/vendors",
    "GET /api/v1/notification-deliveries",
    "GET /api/v1/notification-deliveries/{delivery_id}",
    "GET /api/v1/notification-templates",
    "GET /api/v1/notification-templates/{template_id}",
    "GET /api/v1/notifications",
    "GET /api/v1/notifications/{notification_id}",
    "GET /api/v1/on-call-rotation-members",
    "GET /api/v1/on-call-rotation-members/{member_id}",
    "GET /api/v1/on-call-rotations",
    "GET /api/v1/on-call-rotations/{rotation_id}",
    "GET /api/v1/qualification/buildout-milestones",
    "GET /api/v1/qualification/buildout-milestones/{milestone_id}",
    "GET /api/v1/qualification/buildout-projects",
    "GET /api/v1/qualification/buildout-projects/{project_id}",
    "GET /api/v1/qualification/buildout-requests",
    "GET /api/v1/qualification/buildout-requests/{request_id}",
    "GET /api/v1/qualification/buildout-updates",
    "GET /api/v1/qualification/checks",
    "GET /api/v1/qualification/checks/{qualification_id}",
    "GET /api/v1/qualification/coverage-areas",
    "GET /api/v1/qualification/coverage-areas/{area_id}",
    "GET /api/v1/search/accounts",
    "GET /api/v1/search/business-accounts",
    "GET /api/v1/search/catalog-offers",
    "GET /api/v1/search/contacts",
    "GET /api/v1/search/customers",
    "GET /api/v1/search/global",
    "GET /api/v1/search/invoices",
    "GET /api/v1/search/nas-devices",
    "GET /api/v1/search/network-devices",
    "GET /api/v1/search/people",
    "GET /api/v1/search/pop-sites",
    "GET /api/v1/search/reseller-subscribers",
    "GET /api/v1/search/resellers",
    "GET /api/v1/search/subscribers",
    "GET /api/v1/search/subscriptions",
    "GET /api/v1/settings/audit",
    "GET /api/v1/settings/audit/{key}",
    "GET /api/v1/settings/catalog",
    "GET /api/v1/settings/catalog/{key}",
    "GET /api/v1/settings/collections",
    "GET /api/v1/settings/collections/{key}",
    "GET /api/v1/settings/comms",
    "GET /api/v1/settings/comms/{key}",
    "GET /api/v1/settings/geocoding",
    "GET /api/v1/settings/geocoding/{key}",
    "GET /api/v1/settings/gis",
    "GET /api/v1/settings/gis/{key}",
    "GET /api/v1/settings/imports",
    "GET /api/v1/settings/imports/{key}",
    "GET /api/v1/settings/inventory",
    "GET /api/v1/settings/inventory/{key}",
    "GET /api/v1/settings/lifecycle",
    "GET /api/v1/settings/lifecycle/{key}",
    "GET /api/v1/settings/network",
    "GET /api/v1/settings/network/{key}",
    "GET /api/v1/settings/notification",
    "GET /api/v1/settings/notification/{key}",
    "GET /api/v1/settings/provisioning",
    "GET /api/v1/settings/provisioning/{key}",
    "GET /api/v1/settings/radius",
    "GET /api/v1/settings/radius/{key}",
    "GET /api/v1/settings/scheduler",
    "GET /api/v1/settings/scheduler/{key}",
    "GET /api/v1/settings/subscriber",
    "GET /api/v1/settings/subscriber/{key}",
    "GET /api/v1/settings/usage",
    "GET /api/v1/settings/usage/{key}",
    "GET /api/v1/webhooks/deliveries",
    "GET /api/v1/webhooks/deliveries/{delivery_id}",
    "GET /api/v1/webhooks/endpoints",
    "GET /api/v1/webhooks/endpoints/{endpoint_id}",
    "GET /api/v1/webhooks/subscriptions",
    "GET /api/v1/webhooks/subscriptions/{subscription_id}",
    "GET /api/v1/wireguard/peers",
    "GET /api/v1/wireguard/peers/{peer_id}",
    "GET /api/v1/wireguard/peers/{peer_id}/config",
    "GET /api/v1/wireguard/peers/{peer_id}/config/download",
    "GET /api/v1/wireguard/peers/{peer_id}/connection-logs",
    "GET /api/v1/wireguard/peers/{peer_id}/mikrotik-script",
    "GET /api/v1/wireguard/peers/{peer_id}/mikrotik-script/download",
    "GET /api/v1/wireguard/servers",
    "GET /api/v1/wireguard/servers/{server_id}",
    "GET /api/v1/wireguard/servers/{server_id}/status",
    "GET /api/v1/zabbix/alerts",
    "GET /api/v1/zabbix/hosts",
    "GET /api/v1/zabbix/metrics",
    "PATCH /api/v1/alert-notification-policies/{policy_id}",
    "PATCH /api/v1/alert-notification-policy-steps/{step_id}",
    "PATCH /api/v1/analytics/kpi-configs/{config_id}",
    "PATCH /api/v1/comms/customer-notifications/{event_id}",
    "PATCH /api/v1/comms/surveys/{survey_id}",
    "PATCH /api/v1/connectors/configs/{config_id}",
    "PATCH /api/v1/external-references/{ref_id}",
    "PATCH /api/v1/gis/areas/{area_id}",
    "PATCH /api/v1/gis/layers/{layer_id}",
    "PATCH /api/v1/gis/locations/{location_id}",
    "PATCH /api/v1/integrations/jobs/{job_id}",
    "PATCH /api/v1/integrations/targets/{target_id}",
    "PATCH /api/v1/nas/devices/{device_id}",
    "PATCH /api/v1/nas/templates/{template_id}",
    "PATCH /api/v1/notification-deliveries/{delivery_id}",
    "PATCH /api/v1/notification-templates/{template_id}",
    "PATCH /api/v1/notifications/{notification_id}",
    "PATCH /api/v1/on-call-rotation-members/{member_id}",
    "PATCH /api/v1/on-call-rotations/{rotation_id}",
    "PATCH /api/v1/qualification/buildout-milestones/{milestone_id}",
    "PATCH /api/v1/qualification/buildout-projects/{project_id}",
    "PATCH /api/v1/qualification/buildout-requests/{request_id}",
    "PATCH /api/v1/qualification/coverage-areas/{area_id}",
    "PATCH /api/v1/webhooks/deliveries/{delivery_id}",
    "PATCH /api/v1/webhooks/endpoints/{endpoint_id}",
    "PATCH /api/v1/webhooks/subscriptions/{subscription_id}",
    "PATCH /api/v1/wireguard/peers/{peer_id}",
    "PATCH /api/v1/wireguard/servers/{server_id}",
    "POST /api/v1/alert-notification-policies",
    "POST /api/v1/alert-notification-policy-steps",
    "POST /api/v1/analytics/kpi-aggregates",
    "POST /api/v1/analytics/kpi-configs",
    "POST /api/v1/comms/customer-notifications",
    "POST /api/v1/comms/eta-updates",
    "POST /api/v1/comms/survey-responses",
    "POST /api/v1/comms/surveys",
    "POST /api/v1/connectors/configs",
    "POST /api/v1/defaults/calculate-due-date",
    "POST /api/v1/external-references",
    "POST /api/v1/external-references/sync",
    "POST /api/v1/geocode/preview",
    "POST /api/v1/gis/areas",
    "POST /api/v1/gis/layers",
    "POST /api/v1/gis/locations",
    "POST /api/v1/gis/sync",
    "POST /api/v1/imports/subscriber-custom-fields",
    "POST /api/v1/integrations/jobs",
    "POST /api/v1/integrations/jobs/refresh-schedule",
    "POST /api/v1/integrations/jobs/{job_id}/run",
    "POST /api/v1/integrations/targets",
    "POST /api/v1/nas/devices",
    "POST /api/v1/nas/devices/{device_id}/backups",
    "POST /api/v1/nas/devices/{device_id}/backups/manual",
    "POST /api/v1/nas/devices/{device_id}/ping",
    "POST /api/v1/nas/devices/{device_id}/provision",
    "POST /api/v1/nas/templates",
    "POST /api/v1/nas/templates/{template_id}/preview",
    "POST /api/v1/nextcloud-talk/rooms",
    "POST /api/v1/nextcloud-talk/rooms/list",
    "POST /api/v1/nextcloud-talk/rooms/{room_token}/messages",
    "POST /api/v1/notification-deliveries",
    "POST /api/v1/notification-deliveries/bulk",
    "POST /api/v1/notification-templates",
    "POST /api/v1/notifications",
    "POST /api/v1/notifications/bulk",
    "POST /api/v1/on-call-rotation-members",
    "POST /api/v1/on-call-rotations",
    "POST /api/v1/payments/initiate",
    "POST /api/v1/payments/verify",
    "POST /api/v1/qualification/buildout-milestones",
    "POST /api/v1/qualification/buildout-projects",
    "POST /api/v1/qualification/buildout-requests",
    "POST /api/v1/qualification/buildout-requests/{request_id}/approve",
    "POST /api/v1/qualification/buildout-updates",
    "POST /api/v1/qualification/check",
    "POST /api/v1/qualification/coverage-areas",
    "POST /api/v1/validation/field",
    "POST /api/v1/validation/form/{form_type}",
    "POST /api/v1/webhooks/deliveries",
    "POST /api/v1/webhooks/endpoints",
    "POST /api/v1/webhooks/subscriptions",
    "POST /api/v1/wireguard/peers",
    "POST /api/v1/wireguard/peers/{peer_id}/disable",
    "POST /api/v1/wireguard/peers/{peer_id}/enable",
    "POST /api/v1/wireguard/peers/{peer_id}/provision-token",
    "POST /api/v1/wireguard/servers",
    "POST /api/v1/wireguard/servers/{server_id}/regenerate-keys",
    "PUT /api/v1/settings/audit/{key}",
    "PUT /api/v1/settings/catalog/{key}",
    "PUT /api/v1/settings/collections/{key}",
    "PUT /api/v1/settings/comms/{key}",
    "PUT /api/v1/settings/geocoding/{key}",
    "PUT /api/v1/settings/gis/{key}",
    "PUT /api/v1/settings/imports/{key}",
    "PUT /api/v1/settings/inventory/{key}",
    "PUT /api/v1/settings/lifecycle/{key}",
    "PUT /api/v1/settings/network/{key}",
    "PUT /api/v1/settings/notification/{key}",
    "PUT /api/v1/settings/provisioning/{key}",
    "PUT /api/v1/settings/radius/{key}",
    "PUT /api/v1/settings/scheduler/{key}",
    "PUT /api/v1/settings/subscriber/{key}",
    "PUT /api/v1/settings/usage/{key}",
}


def _build_full_api() -> FastAPI:
    """Mount every API router from both spec lists onto a throwaway app, so
    the audit covers the true production surface deterministically."""
    test_app = FastAPI()
    for module_name, attr_name, mount_kind, mode in (
        _CORE_ROUTER_SPECS + _DEFERRED_API_ROUTER_SPECS
    ):
        if mount_kind != "api":
            continue
        router = _load_router_object(module_name, attr_name)
        _mount_router(test_app, router, mount_kind, mode)
    return test_app


def _dependency_calls(dependant) -> set[str]:
    names: set[str] = set()
    call = getattr(dependant, "call", None)
    if call is not None:
        names.add(getattr(call, "__name__", ""))
    for sub in getattr(dependant, "dependencies", []) or []:
        names |= _dependency_calls(sub)
    return names


def _is_allowlisted(path: str) -> bool:
    return any(path.startswith(p) for p in _ALLOWLIST_PREFIXES)


def test_all_api_routes_declare_an_authorization_guard():
    app = _build_full_api()
    unguarded: list[str] = []
    seen_quarantined: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/v1"):
            continue
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        names = _dependency_calls(dependant)
        # "none"-mode routers carry no require_user_auth: intentional public
        # callbacks (webhooks, tr069). This test targets the authenticated-
        # but-unguarded gap, so only audit routes that DO require auth.
        if "require_user_auth" not in names:
            continue
        if names & _GUARD_NAMES:
            continue
        if _is_allowlisted(path):
            continue
        methods = ",".join(sorted(getattr(route, "methods", []) or []))
        entry = f"{methods} {path}"
        if entry in _KNOWN_UNGUARDED:
            seen_quarantined.add(entry)
            continue
        unguarded.append(entry)

    assert not unguarded, (
        "These authenticated /api/v1 routes have no permission/role guard "
        "(any logged-in customer can call them). Add a require_permission "
        "dependency, or allowlist the path if it is genuinely self-scoped:\n  "
        + "\n  ".join(sorted(unguarded))
    )

    # Burn-down hygiene: once a quarantined route is guarded (or removed), its
    # entry must be deleted from _KNOWN_UNGUARDED so the list shrinks honestly.
    stale = _KNOWN_UNGUARDED - seen_quarantined
    assert not stale, (
        "These routes are in the unguarded quarantine but are now guarded or "
        "gone — remove them from _KNOWN_UNGUARDED:\n  " + "\n  ".join(sorted(stale))
    )
