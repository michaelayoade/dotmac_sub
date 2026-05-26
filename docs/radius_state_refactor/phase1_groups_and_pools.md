# Phase 1 — Groups and Pools

**Status**: ready to execute
**Owner**: TBD
**Last updated**: 2026-05-26
**Prerequisites**: phase 0 design approved
([phase0_state_model.md](./phase0_state_model.md))
**Risk**: low — purely additive; nothing in app code reads the new state yet

## Goal

Provision the three RADIUS groups (`dotmac-active`, `dotmac-suspended`,
`dotmac-captive`) on the external FreeRADIUS DB and the supporting NAS
pool + standing firewall rules on each Mikrotik in the fleet. After
phase 1, the infrastructure is in place but nothing routes through it.
Phases 2-3 add the app-side write paths.

## What changes

| Layer | Change | How |
|---|---|---|
| External RADIUS DB | New `radgroupcheck` + `radgroupreply` rows for the 3 groups | `config/freeradius/upgrade_002_access_state_groups.sql` |
| Each Mikrotik NAS | New `dotmac-captive-pool` IP pool + standing firewall rules for that pool | Manual ops per the runbook below |
| App code | None | This phase is infra-only |

## Step 1 — Apply the RADIUS migration

```bash
# Staging first:
docker exec dotmac_sub_radius_db psql -U radius -d radius \
    -v ON_ERROR_STOP=1 \
    -f /opt/dotmac_sub/config/freeradius/upgrade_002_access_state_groups.sql

# Verify groups landed:
docker exec dotmac_sub_radius_db psql -U radius -d radius -c \
    "SELECT groupname, attribute, op, value FROM radgroupcheck WHERE groupname LIKE 'dotmac-%' ORDER BY groupname, attribute;"
docker exec dotmac_sub_radius_db psql -U radius -d radius -c \
    "SELECT groupname, attribute, op, value FROM radgroupreply WHERE groupname LIKE 'dotmac-%' ORDER BY groupname, attribute;"
```

Expected output:

```
 groupname        | attribute | op |  value
------------------+-----------+----+--------
 dotmac-suspended | Auth-Type | := | Reject

 groupname       | attribute              | op |        value
-----------------+------------------------+----+---------------------
 dotmac-active   | Framed-Protocol        | := | PPP
 dotmac-active   | Service-Type           | := | Framed-User
 dotmac-captive  | Framed-Pool            | := | dotmac-captive-pool
 dotmac-captive  | Framed-Protocol        | := | PPP
 dotmac-captive  | Mikrotik-Address-List  | := | dotmac-captive
 dotmac-captive  | Mikrotik-Rate-Limit    | := | 1M/1M
 dotmac-captive  | Service-Type           | := | Framed-User
```

No FreeRADIUS reload needed — `radusergroup` membership is empty
(nothing routes through these yet), so the group definitions are inert
until phase 3 starts writing user→group rows.

## Step 2 — Verify groups compile via radclient

Pick any active customer username (e.g., `100025610`) and add a
temporary `radusergroup` row pointing them at `dotmac-suspended` to
confirm the group machinery works end-to-end. **Remove the row
immediately after.**

```bash
# Add temporary group membership
docker exec dotmac_sub_radius_db psql -U radius -d radius -c \
    "INSERT INTO radusergroup (username, groupname, priority) VALUES ('100025610', 'dotmac-suspended', 0);"

# Auth — must be Access-Reject because of dotmac-suspended's Auth-Type Reject
docker exec dotmac_sub_freeradius bash -c \
    "echo 'User-Name=100025610,User-Password=D0tm@cTest_2026' | radclient -x 127.0.0.1:1812 auth testing123" \
    | grep -E "Received"
# Expected: Received Access-Reject

# Remove the test membership
docker exec dotmac_sub_radius_db psql -U radius -d radius -c \
    "DELETE FROM radusergroup WHERE username = '100025610' AND groupname = 'dotmac-suspended';"

# Auth — must be Access-Accept again, original behavior
docker exec dotmac_sub_freeradius bash -c \
    "echo 'User-Name=100025610,User-Password=D0tm@cTest_2026' | radclient -x 127.0.0.1:1812 auth testing123" \
    | grep -E "Received"
# Expected: Received Access-Accept
```

If both probes return the expected packet types, the group machinery is
working. If suspended-group probe returns Access-Accept anyway,
investigate FreeRADIUS group handling (`read_groups = yes` in
`mods-enabled/sql`, group_membership_query, etc.).

## Step 3 — NAS-side pool provisioning (per NAS, manual)

Run these commands on each active Mikrotik that serves customer
traffic. The pool is only needed for the `dotmac-captive` group; the
`dotmac-suspended` group rejects auth entirely and `dotmac-active` uses
the existing IP-assignment scheme.

**Per-NAS RouterOS commands**:

```
# 1. Pick a /24 (or whatever size suits your captive customer count) from
#    private space. MUST NOT overlap any existing customer pool or the
#    existing dotmac-reject-negative CIDR.
/ip pool add name=dotmac-captive-pool ranges=10.255.250.1-10.255.250.254

# 2. Standing firewall rules for the captive pool. We re-use the same
#    pattern as the existing dotmac-reject-negative chain — allow DNS,
#    allow the portal IP on OSS ports, dst-nat HTTP to the portal,
#    drop everything else.
/ip firewall address-list add list=dotmac-captive address=10.255.250.0/24 \
    comment="dotmac access-state captive pool"

# Tip: if you've already pushed reject rules via the existing
# push_reject_rules_to_radius_nas path, the dotmac-reject-negative
# chain already exists. You can either:
#   (a) Reuse that chain by pointing dotmac-captive at it; or
#   (b) Stand up a parallel dotmac-captive chain identical to the
#       negative one. Recommended (b) for phase 1 so the new path is
#       independent and can be ripped out cleanly if we reverse course.

/ip firewall filter add chain=forward src-address-list=dotmac-captive \
    protocol=udp dst-port=53 action=accept \
    comment="dotmac-captive-allow-dns"

/ip firewall filter add chain=forward src-address-list=dotmac-captive \
    dst-address=<PORTAL_IP> protocol=tcp dst-port=80,443,8101,8102,8103,8104 \
    action=accept comment="dotmac-captive-allow-oss"

/ip firewall nat add chain=dstnat src-address-list=dotmac-captive \
    protocol=tcp dst-port=80 action=dst-nat \
    to-addresses=<PORTAL_IP> to-ports=80 \
    comment="dotmac-captive-redirect-http"

/ip firewall filter add chain=forward src-address-list=dotmac-captive \
    action=drop comment="dotmac-captive-drop-other"
```

Replace `<PORTAL_IP>` with the customer-portal IP (same value as the
existing `captive_portal_ip` DomainSetting in the app — check via
`/admin/system/settings`).

**Verify per NAS**:

```
/ip pool print where name=dotmac-captive-pool
/ip firewall address-list print where list=dotmac-captive
/ip firewall filter print where comment~"dotmac-captive"
/ip firewall nat print where comment~"dotmac-captive"
```

You should see one pool, one address-list entry (the CIDR itself), four
filter rules, and one NAT rule per NAS.

## Step 4 — Mark phase 1 complete

Once steps 1-3 are done in both staging and production, update the
`Status:` line in this doc to `complete` and announce in the team
channel that phase 2 (add `access_state` column) can begin.

## Rollback

If anything goes wrong during or after phase 1:

```bash
# Roll back RADIUS groups
docker exec dotmac_sub_radius_db psql -U radius -d radius -c \
    "DELETE FROM radgroupcheck WHERE groupname LIKE 'dotmac-%';
     DELETE FROM radgroupreply WHERE groupname LIKE 'dotmac-%';"
```

```
# Roll back NAS-side (per NAS)
/ip firewall nat remove [find comment~"dotmac-captive"]
/ip firewall filter remove [find comment~"dotmac-captive"]
/ip firewall address-list remove [find list=dotmac-captive]
/ip pool remove [find name=dotmac-captive-pool]
```

Nothing in app code references any of these yet, so rollback has no
behavioral consequences — the system reverts to its pre-phase-1 state.

## Exit criteria (must all be true to move to phase 2)

- [ ] `radgroupcheck` and `radgroupreply` rows present and match expected output in staging
- [ ] `radgroupcheck` and `radgroupreply` rows present and match expected output in production
- [ ] radclient probe with temporary `dotmac-suspended` membership returns Access-Reject (staging)
- [ ] `dotmac-captive-pool` IP pool present on every active Mikrotik (verified via `/ip pool print`)
- [ ] `dotmac-captive` firewall + NAT chain rules present on every active Mikrotik
- [ ] No customer-visible behavior change observed for 24h after deployment
- [ ] Owner + rollback plan reviewed by a second engineer

## Watch-outs

- **Existing rules**: if you re-use the `dotmac-reject-negative` chain
  for captive (option (a) in step 3), be aware that ripping out the old
  reject-pool system in phase 9+ would also rip out captive routing. Use
  option (b) for cleaner phase separation.
- **Pool CIDR collisions**: the example uses `10.255.250.0/24`. Pick
  something that's not already in use on any NAS — check existing
  `/ip pool print` and existing `dotmac-reject-*` CIDRs first.
- **Phase 2 dependency**: phase 2 adds `subscription.access_state` but
  doesn't populate it. Phase 3 starts shadow-writing radusergroup. If
  you skip phase 1 verification and roll straight into phase 2, you
  won't catch group-machinery bugs until phase 3 — much harder to debug.
