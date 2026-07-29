# Composable service-team source of truth

Status: additive schema, consumer cutover, and typed legacy-column shadow
verification implemented; restored-production evidence and later contract
migration pending.

## Ownership

`operations.service_team_lifecycle` owns stable native team identity, soft
lifecycle, capability assignments, geographic scope bindings, membership,
member responsibilities, team relationships, external-reference observations,
and the corresponding administration and scope projections.

`party.registry` owns Person Party identity.
`auth.staff_provisioning` owns staff authentication principals and RBAC.
Membership references a Person Party. A responsibility narrows operational
scope but never grants permission; an action requires both RBAC authorization
and the applicable team scope.

External CRM, workforce, ERP, and directory identifiers are observations in
`service_team_external_references`. No external department or imported
identifier defines team identity or writes native membership.

## Composable model

- `ServiceTeam` is stable identity and lifecycle only.
- `ServiceTeamCapabilityDefinition` is governed vocabulary. Definitions name
  the consuming contract owner; deployments seed vocabulary, never teams,
  memberships, routing assignments, or access grants.
- `ServiceTeamCapability` assigns many capabilities to one team.
- `ServiceTeamMember` records belonging only.
- `ServiceTeamMemberResponsibility` assigns zero or many governed operational
  responsibilities to one membership.
- `ServiceTeamScopeBinding` binds a team to typed geographic scope. The first
  registered type is a foreign key to authoritative `GeoArea`.
- `ServiceTeamRelationship` records explicit parent/child topology.
- `ServiceTeamExternalReference` records many provider-neutral observations
  with provider, entity type, value, observation time, and lifecycle.

Capability and responsibility keys consumed by code are closed enums backed by
active definition rows. Arbitrary metadata cannot introduce executable
vocabulary.

## Routing and consumer rules

- Staff resolution is set-valued. Multiple memberships are valid and never
  become an `ambiguous` error.
- A consumer that needs one team must use the work or domain routing owner that
  selected that exact team.
- Outage ownership consumes active `OutageTeamRoutingPolicy` rows and verifies
  their registered required capability. It never selects the oldest team of a
  type.
- Field-job conversation assignment consumes the exact assigned dispatch queue
  and dispatch-rule team for that work order and technician. It never guesses
  from general membership.
- Outbound email activity is supplied by the calling delivery domain or an
  explicit route/team override. It is never derived from team identity.
- Workqueue first requires its RBAC permission. Team audience is then narrowed
  to memberships carrying `queue_lead` or `accountable_manager`, unless an
  explicit RBAC audience scope authorizes all of the principal's memberships.

## Command and lifecycle rules

- Public writes enter `execute_owner_command` once on a transaction-free
  session.
- Team and membership rows are locked; team names are unique
  case-insensitively.
- Create and update require at least one active registered capability and
  validate every GeoArea scope.
- Member commands accept a set of registered responsibilities. The legacy
  scalar role is written only as the inert `member` shadow value.
- New member selections require an active SystemUser, an active Person Party,
  and a reviewed binding between them.
- Membership removal deactivates the membership and every active
  responsibility. Team or membership hard-delete is not exposed.
- A team with active members cannot be deactivated.
- Audit and versioned domain events are staged in the owner transaction.

## Admin page contract

- Screen: `admin.system.service-teams`; list, detail, and composable editor.
- Audience/job: operations administrators maintain team identity,
  capabilities, GeoArea scope, membership, and responsibilities.
- First viewport: team identity, active state, capabilities, geographic scope,
  accountable managers, active member count, legacy-shadow drift signal, and
  the applicable action.
- Actions: create or edit composition; activate/deactivate; add member; replace
  a member's responsibility set; remove member. No hard delete or bulk
  mutation is exposed.
- List search covers team name, capability key, and GeoArea name. Active state
  remains the common filter.
- Staff display name and work email remain internal identity data behind
  service-team read/membership permissions. Raw Party and external identifiers
  remain evidence-depth information.
- The UI consumes lifecycle projections and eligibility; templates do not
  infer capability, responsibility, routing, scope, or authorization.

## Migration and retirement

Migration 426 remains immutable. Before it runs,
`operations.service_team_pointer_retirement` may clear only the complete exact,
separately approved set of unresolved legacy manager UUIDs. It does not read
CRM or import CRM teams, People, or memberships. Duplicate team names, invalid
native memberships, and malformed workflow settings remain blocking.

Migration 437 is the additive expand step:

1. creates governed capability and responsibility definitions;
2. creates capability, responsibility, GeoArea scope, relationship, external
   reference, and outage-routing tables;
3. projects existing scalar team types and membership roles into the new
   structures once for behavioral continuity;
4. retains valid legacy manager pointers as shadow evidence only and never
   creates membership or operational scope from the scalar pointer;
5. records legacy workforce department pairs as external observations; and
6. leaves scalar columns intact for shadow comparison.

No string region is automatically mapped to a GeoArea because that would
invent geographic authority. Such rows surface shadow drift until an operator
binds the authoritative GeoArea.

`team_type`, `region`, `manager_person_id`, and
`service_team_members.role` are legacy shadow fields after migration 437.
Runtime decisions no longer consume them. A later contract migration may drop
them only after restored-production rehearsal and production shadow evidence
show:

- every active team has registered capabilities;
- every operational geography has authoritative scope bindings;
- every operational responsibility exists in the composed rows;
- every migrated consumer matches expected behavior;
- no writer or reader uses the scalar fields; and
- rollback requirements for the expand release have expired.

A non-null legacy manager pointer remains a drift blocker until an
administrator explicitly assigns the matching active Person Party the
`accountable_manager` responsibility through
`operations.service_team_lifecycle`, or separately approves retirement of the
obsolete pointer. Migration 437 does not translate the pointer into membership:
doing so would grant operational scope from a legacy scalar outside the command
owner.

`operations.service_team_lifecycle.audit_legacy_service_team_shadow` is the
read-only, transaction-current verification owner. It reports total drifted
teams and typed counts for:

- legacy team-type to active-capability mismatch;
- legacy region text requiring explicit GeoArea review;
- a manager pointer without matching explicit `accountable_manager`
  composition; and
- an active legacy `lead` or `manager` role without its projected active
  responsibility.

The audit is ready only when no team has any issue. Its classifications are
evidence for operator repair and the later contract-migration decision; they
never authorize a repair or change runtime scope.
