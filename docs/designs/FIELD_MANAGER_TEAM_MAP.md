# Field Manager Team Map

Status: approved implementation contract

Owner: field operations UI

## Page contract

- **Screen identifier and type:** `mobile.manager.team_map`, operational map
  with a synchronized technician list.
- **Audience and permission:** authenticated staff managers holding
  `operations:dispatch:read`. Navigation and the API route use the same gate.
- **Operational job and decision:** locate sharing technicians, distinguish
  current from stale observations, and open dispatch context for the selected
  technician.
- **Authoritative read owner:** `ui.field_live_map_projection`, implemented by
  `app.services.field_maps.list_technician_positions`. The mobile adapter does
  not recalculate sharing visibility or freshness.
- **Primary entities:** sharing-authorized technician positions, keyed by typed
  technician and person identifiers. The separate manager roster provides
  profile and current-work context without precise coordinates.
- **First viewport:** geographic map, live/stale counts, feed freshness, and a
  fit-all control. Search and live/stale/not-sharing filters lead the
  synchronized roster immediately below it.
- **Actions:** tap a marker or roster row to bring the map into view, focus the
  exact shared coordinate, and inspect status, nearest address, observation
  time, accuracy, and current work. When current work has an authoritative
  public work-order identifier, the one workflow action opens that exact
  dispatch detail; a title alone never creates a navigation target.
- **Sensitivity:** exact coordinates are private operational data. The map
  owner excludes technicians with sharing disabled before serialization, and
  the roster contract never contains latitude, longitude, accuracy, or the
  location-observation timestamp. The selected-detail owner rechecks sharing
  before sending that one current coordinate to the configured geocoding
  provider; bulk and refresh-time reverse geocoding are forbidden.
- **Freshness:** the owner supplies `is_live` and `stale_after_seconds`; the
  client shows both live and stale positions distinctly and labels last-known
  observation time. It refreshes the feed every 30 seconds only while the app
  and route are active, with pull-to-refresh as an explicit fallback.
- **Ordering:** live technicians first, then stale sharing technicians,
  technicians waiting for a first fix, and non-sharing technicians; names are
  the stable tie-breaker.
- **States:** map and roster independently preserve stable loading, empty,
  partial-refresh failure, initial error with retry, invalid-coordinate, and
  unauthorized presentations. Unknown, stale, off-shift, and not-sharing are
  never collapsed into `Idle`.
- **Responsive and accessibility:** markers use semantic button labels and
  text status in addition to color. Controls remain horizontally scrollable,
  the roster is lazily built, and the map retains a stable mobile height.

## Manager dispatch detail page contract

- **Screen identifier and type:** `mobile.manager.dispatch_detail`, read-only
  operational work-order detail.
- **Audience and permission:** authenticated staff managers holding
  `operations:work_order:read` or `operations:technician:read`, matching the
  manager jobs API. The mobile route fails closed when the manager profile or
  permission is absent. Entry through Team Map additionally requires that
  surface's `operations:dispatch:read` permission.
- **Operational job and decision:** confirm which exact open work order is
  being dispatched, its current authoritative status and priority, when and
  where it is scheduled, who is assigned, and the scope to be performed.
- **Authoritative read owner:** `operations.work_orders`, projected through the
  typed field-manager jobs contract. The mobile client selects the exact public
  identifier from that feed and does not derive status, assignment eligibility,
  or execution state.
- **Primary entity:** one native work order keyed by its public work-order
  identifier. Retained CRM identifiers and mirror UUIDs are not shown.
- **First viewport:** work-order title, owner-supplied status presentation,
  work type, priority, schedule, and current technician assignment.
- **Later sections:** subscriber/site context, full address and coordinates
  when supplied by the owner, followed by the scope of work.
- **Actions:** the detail is read-only. Assignment and unassignment remain the
  dispatch queue's existing row action; technician execution actions are not
  exposed to managers by this screen.
- **Freshness and states:** pull-to-refresh reloads the authoritative manager
  jobs feed. Loading, retryable read failure, permission denial, and a job that
  has left the open dispatch queue remain distinct states.
- **Drill-down entry points:** tapping any dispatch queue card and tapping
  `View dispatch` for a selected technician both open the same route with the
  exact public work-order identifier. Team Map also supplies the authoritative
  person identifier so the existing manager query can retrieve that assigned
  work independently of the bounded general queue page.
- **Responsive and accessibility:** the page uses a single scrollable column,
  text labels in addition to status color/icon, wrapping content, and no raw
  horizontal evidence table.

## Validation contract

- Backend tests prove disabled sharing and invalid coordinates fail closed.
- Architecture tests prove the mobile adapter calls the typed owner and uses
  `operations:dispatch:read`.
- Flutter widget tests cover live and stale geographic markers, marker detail,
  roster focus, live/last-known address detail, filtering, invalid coordinates,
  retryable error state, exact-dispatch navigation, detail permission and
  availability states, and narrow-screen layout.
- The standard mobile viewport must be checked for overflow with one, many,
  and no mapped technicians before release.
