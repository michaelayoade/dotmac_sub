# Connector manifest adoption

This runbook governs explicit movement of an integration installation from one
deployed connector manifest pin to another. It does not authorize production
access or deployment; the operator must use the explicitly named target and
normal change approval.

## Authority and invariants

- `integration.registry` owns current and bounded historical deployed
  definitions.
- `integration.installations` is the sole writer of installation
  `connector_version` and `manifest_digest`.
- Deployment is an adapter and readiness gate. It never silently re-pins an
  installation.
- The expected installed and target version/digest pairs are reviewed together
  and changed atomically.
- Secret values never enter the request, response, event, logs, or evidence.
- Direct SQL is not an adoption path.

## Expand

1. Add a new semantic connector version and its manifest digest to
   `tests/architecture/connector_manifest_pins.json`.
2. Retain every definition still pinned by an enabled installation, with a
   compatible runtime, for the adoption and rollback window.
3. Deploy the expand release. The deployment runs:

   ```bash
   docker compose -f docker-compose.yml run --rm --no-deps app \
     python -m scripts.integrations.verify_manifest_pins
   ```

   `unavailable_count` must be zero. `supported_historical_count` identifies
   installations requiring adoption; it is not permission to remove their
   definitions.

## Preview

Use the authenticated operator API to read the exact review evidence:

```text
GET /api/v1/integrations/installations/{installation_id}/manifest-adoption
```

Review:

- installation identity, connector key, environment, and lifecycle state;
- installed version and digest;
- target version and digest from the running image;
- `ready=true`;
- an empty `blocking_errors` list;
- configuration revision and capability/secret-reference compatibility through
  the installation detail.

Resolve every blocker through normal configuration and validation owners. Do
not edit the pin to bypass incompatibility.

## Adopt

Submit the previewed pairs unchanged:

```text
POST /api/v1/integrations/installations/{installation_id}/manifest-adoption

{
  "expected_installed_pin": {
    "connector_version": "<reviewed-installed-version>",
    "manifest_digest": "<reviewed-installed-digest>"
  },
  "target_pin": {
    "connector_version": "<reviewed-target-version>",
    "manifest_digest": "<reviewed-target-digest>"
  },
  "reason": "<approved change reference and reason>",
  "idempotency_key": "<stable approved operation key>"
}
```

The owner locks the installation row, rejects stale expected state or a target
not present in the running image, revalidates manifest compatibility, updates
both fields, preserves installation and binding lifecycle state, and records
`integration.installation.manifest_adopted`. Repeating the same target is an
idempotent replay and creates no second event.

## Verify and rollback

After adoption:

1. Repeat the preview and require `pin_state=current` for a forward adoption.
2. Run the manifest-pin deployment report and confirm the installation is
   current.
3. Exercise a non-charging runtime check and the affected customer read page.
4. Monitor bounded runtime, HTTP, and payment-intent evidence before closing
   the adoption window.

If the new definition must be rolled back, submit the exact prior pin recorded
in the adoption event as the reviewed target while that historical definition
remains deployed. Use the same owner and verification steps. Do not roll back
to an application image that cannot execute the installation's current pin.

## Contract

Remove a historical definition only in a later reviewed release when:

- no enabled installation pins it;
- the deployment report has zero unavailable pins;
- runtime and customer-flow verification is accepted;
- the rollback window is explicitly closed; and
- the manifest-pin ledger and this runbook remain consistent.
