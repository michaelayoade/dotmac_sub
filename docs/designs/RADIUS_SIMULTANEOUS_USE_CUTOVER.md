# RADIUS Simultaneous-Use Cutover

Status: implementation ready; production cutover requires operator approval and
preflight evidence.

## Ownership

`access.radius_projection` is the sole owner and writer of the per-login
FreeRADIUS authentication projection. Subscriptions, active access credentials,
effective RADIUS profiles, and the canonical access-mode plan are its
authoritative inputs.

The external `radcheck`, `radreply`, and `radusergroup` tables are rebuildable
projections. Open `radacct` rows are session observations used by FreeRADIUS to
count concurrency; they do not establish customer, service, site, or credential
ownership.

## Defect and corrected contract

The legacy writer put `Simultaneous-Use := 1` in `radreply`. FreeRADIUS returns
that table to the NAS, but its `session { sql }` check only enforces the limit
when `Simultaneous-Use` is loaded as a check/control item from `radcheck`.
Consequently, one valid credential could authenticate from two CPEs at once.

After cutover:

- active and captive logins receive their password and `Simultaneous-Use` in
  `radcheck`;
- hard-rejected logins receive only `Auth-Type := Reject`;
- `radreply` contains NAS reply attributes and no `Simultaneous-Use`;
- the permanent access reconciler detects missing or stale check rows and
  misplaced reply rows, then requests the same idempotent projection writer.

The database setting
`radius.simultaneous_use_enforcement_enabled` owns the cutover decision and
defaults to `false`. While it is false, the writer retains the legacy reply row
and the drift detector does not trigger a fleet-wide rewrite.

## Preflight and cutover

Complete these steps in order on the explicitly named production environment:

1. Confirm the scheduled external `radacct` ghost reaper is enabled and healthy.
2. Run the ghost reaper and record its result. It only closes open rows whose
   accounting updates are older than the safety threshold.
3. Review every username with more than one open `radacct` row. Same-device NAS
   transition ghosts may be closed only with evidence. Different device/MAC or
   site pairs require customer/service ownership adjudication and credential
   rotation; do not treat them as accounting cleanup.
4. Verify active PPPoE services have one intended credential and that no old CPE
   retains a credential being moved to another site.
5. Set `radius.simultaneous_use_enforcement_enabled=true` through the canonical
   database settings owner.
6. Let the permanent access reconciler detect placement drift and enqueue one
   full `access.radius_projection` rebuild, or invoke that same writer through
   its approved operator path.
7. Verify representative active and captive users have
   `Simultaneous-Use` in `radcheck`, no user has it in `radreply`, rejects still
   contain only `Auth-Type := Reject`, and a second authentication for a test
   login is rejected while a normal reconnect succeeds.

Do not change customer IPs as part of this cutover. An IP is a projected session
attribute, not the credential or customer owner.

## Rollback

Set the database cutover setting back to `false` and request one full canonical
projection rebuild. The writer will remove the check rows and restore the legacy
reply rows. Investigate the failed gate before another cutover attempt; do not
introduce a second writer or edit RADIUS tables by hand.

## Verification evidence

The implementation is pinned by:

- `tests/test_radius_population.py`
- `tests/test_radius_projection_targets.py`
- `tests/test_radius_scoped_reconcile.py`
- `tests/test_radius_projection_drift.py`
- `tests/test_freeradius_subscriber_config.py`
- `tests/architecture/test_radius_projection_ownership.py`

The incident-shaped acceptance case is one login retained on an old CPE and
installed on a new CPE. The corrected control must keep the first valid session
and reject a concurrent second authentication; service ownership and credential
cutover remain separate operational controls.
