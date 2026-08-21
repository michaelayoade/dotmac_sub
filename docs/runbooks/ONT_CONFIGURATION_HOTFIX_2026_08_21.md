# ONT Configuration Hotfix - 2026-08-21

## Scope

This note records the production hotfixes applied while configuring ONT
`c59db9be-00e2-483d-bc5c-52c631409595` for customer internet service recovery.
The source PR ports the fixes back to `dev`; it does not promote or deploy them.

## Production Symptoms

- ONT configuration form submissions could fail before reaching the typed
  configuration owner when the UI did not submit `idempotency_key`.
- ONT service configuration operation
  `afecbe50-956b-45be-b4f8-6f0ad2734ca1` remained queued because the apply task
  was routed to Celery queue `network`, but the live worker was consuming
  `celery,nin,crm`.
- The Service Recovery Checklist disabled Bind Internet WAN when ACS did not
  expose a `WANPPPConnection`, even though the backend bind action could use the
  Huawei OLT policy-route fallback when ONT OLT identity was available.

## Production Hotfixes

- `/app/app/web/admin/network_onts.py`
  - Backup: `/app/app/web/admin/network_onts.py.hotfix-20260821-idempotency.bak`
  - Made `idempotency_key` optional for ONT configure submit and generated a
    fallback key with the shape `ont-config:{ont_id}:{section}:{uuid}`.
- `/root/dotmac_sub/docker-compose.override.yml`
  - Backup:
    `/root/dotmac_sub/docker-compose.override.yml.hotfix-20260821-network-queue.bak`
  - Temporarily added `network` to the live worker queues so queued operations
    could drain.
- `/app/app/services/web_network_ont_actions/context_builders.py`
  - Backups:
    `context_builders.py.hotfix-20260821-bind-olt-fallback.bak`,
    `context_builders.py.hotfix-20260821-bind-olt-fallback-v2.bak`, and
    `context_builders.py.hotfix-20260821-bind-olt-fallback-v3.bak`
  - Enabled Bind Internet WAN when desired WAN mode is PPPoE and the ONT has
    resolvable Huawei OLT fallback context, even if ACS does not show the PPP
    WAN object.
  - The corrected patch checks both `wan_mode` and `mode`; the first attempt
    only checked `mode`, while the page context used `wan_mode`.

## Source Fix

- ONT configure submit now tolerates missing or blank UI idempotency keys and
  still passes a typed `CommandContext` to the owner command.
- ONT service configuration dispatch no longer hard-routes apply tasks to the
  `network` queue. The default queue is the permanent source fix because the
  dispatcher comment already documents unrouted apply tasks as default-worker
  work.
- The Service Recovery Checklist reports
  `olt_fallback_bind_available=true` and enables Bind Internet WAN when the
  Huawei fallback path is available.

## Verification Evidence

- Production compile checks passed for edited Python files after each hotfix.
- The queued operation moved from `queued` to executed after the worker consumed
  the task.
- The final service recovery context for the affected ONT returned
  `bind_action_enabled=True`, `olt_fallback_bind_available=True`,
  `wan_vlan=203`, and PPPoE username `100000010`.
- A later ACS inform still did not expose the PPP object, confirming the bind UI
  needed the backend OLT fallback path instead of waiting only on ACS refresh.
