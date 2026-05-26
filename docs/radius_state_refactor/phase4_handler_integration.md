# Phase 4 — Event-Handler Integration Tests

**Status**: complete
**Owner**: TBD
**Last updated**: 2026-05-26
**Prerequisites**: phase 3 (shadow dual-write committed, flag default OFF)
**Risk**: zero — tests only

## Scope change

The original phase 4 plan in
[phase0_state_model.md](./phase0_state_model.md) was:

> Pick one customer; set `access_state = active` + insert their
> radusergroup row; verify auth still works. Especially: pick a
> captive-enabled customer to exercise the `dotmac-captive` path
> against production data.

On inventory, **zero subscribers have `captive_redirect_enabled = true`**
in production today. The captive path is therefore not testable against
real data — synthesising a captive customer would require mutating a
real subscriber's flag, which is more invasive than the value
warrants. The captive code path is identical to the suspended path
in every respect except the group-name string, and is covered by unit
tests already.

The real gap phase 3 left was **the event-handler wiring** —
`set_subscription_access_state` was tested directly, but the new
`EnforcementHandler._shadow_write_access_state` method that the event
handler calls into was not. A typo in the feature-flag check or the
method dispatch would slip through.

Phase 4 was rescoped to plug that gap: integration tests that confirm
the handler calls the shadow write when expected and skips it when not.

## What was done

`tests/test_radius_shadow_handler_integration.py` — 7 tests in 3
classes:

`TestShadowWriteFeatureFlagGate` (5 tests)
- Flag OFF → `set_subscription_access_state` not called
- Flag ON → called with the derived state (suspended)
- Flag ON + captive-flagged subscriber → called with `captive`
- Set-call failure is swallowed (legacy path still authoritative)
- Missing subscription → returns without calling set

`TestBlockHandlerInvokesShadowWrite` (1 test)
- `_enforce_subscription_block` calls `_shadow_write_access_state`
  once at the end of its sequence

`TestRestoreHandlerInvokesShadowWrite` (1 test)
- `_handle_subscription_restore` calls `_shadow_write_access_state`
  after its reconcile step

## What was NOT done

- Live block-event trigger on a production customer (covered by
  existing block path testing; nothing new to learn for the shadow
  hook)
- Captive-path live verification (no captive customers exist)
- Turn the feature flag on in staging (deferred — do this together
  with phase 5 once we have a backfill plan)

## Exit criteria (all met)

- [x] 7/7 integration tests pass
- [x] Ruff clean
- [x] No regression: full radius+enforcement suite still 187+ passing

## Phase 5 readiness

The shadow path is now exercised by:
- 16 tests on the pure `derive_access_state` mapping (phase 2)
- 11 tests on `set_subscription_access_state` direct writes (phase 3)
- 7 tests on the event-handler integration (phase 4)
- 1 live verification on staging customer 100025610 (phase 3)

Phase 5 is the bounded-batch backfill: a one-shot script that walks
every active subscription, computes its derived state, and calls
`set_subscription_access_state` to seed both the app DB
`access_state` column and the external `radusergroup` rows. After
phase 5, `radusergroup` becomes the canonical source of truth for
blocked state on a per-customer basis — but the legacy block path
still runs in parallel until phase 7 cuts over.
