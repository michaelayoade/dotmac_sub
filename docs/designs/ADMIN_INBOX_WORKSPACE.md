# Admin Inbox Workspace

Status: implementation contract

Screen identifier: `admin.inbox.workspace`

Route: `/admin/inbox`

## Operational job

Support staff use one workspace to find, prioritise, read, reply to, assign, and
resolve customer conversations without losing subscriber and ticket context.
The first viewport must expose the active queue, the selected thread, and the
next valid action.

## Ownership

| Concern | Owner |
| --- | --- |
| Conversation, message, assignment, delivery, and read facts | Contracted `communications.team_inbox_*` owner family |
| List/detail/count/action projection | `communications.team_inbox_projection` |
| Operator mutations | `communications.team_inbox_commands` |
| Routing and assignment decisions | `communications.team_inbox_routing` |
| Outbound reply intent | `communications.team_inbox_outbound_intents` |
| Realtime delivery hints | `communications.team_inbox_realtime` through the shared realtime platform |
| Subscriber context | Subscriber summary and customer-experience owners |
| Ticket creation | Support ticket command owner |
| Responsive layout, progressive disclosure, draft state, and keyboard interaction | `admin.inbox.workspace` UI contract |

Templates, browser code, routes, WebSocket handlers, and demo fixtures are
adapters. They do not reinterpret conversation state or become business
writers.

## First viewport

- Inbox identity, unread total, realtime connection state, search, and primary
  new-conversation action.
- Common status and assignment cohorts with authoritative counts where the
  backend currently exposes them.
- A paginated conversation queue ordered by the canonical list projection.
- Either a selected conversation or an empty state linking to common cohorts.
- When selected: contact identity, channel, status, ownership, recent messages,
  and a reply/private-note composer.

## Actions

Primary page action: start a conversation.

Common conversation actions: reply, add private note, assign, change status,
change priority, snooze, mute, apply labels, retry a failed message, open
contact context, and start a ticket handoff.

The UI projection owns the labels for the existing lower-is-more-urgent numeric
priority: `100` none, `75` low, `50` medium, `25` high, and `0` urgent. Unknown
legacy values remain visible as their exact numeric priority.

Bulk actions appear only after selection and delegate to the existing bulk
command owner. Destructive actions require explicit confirmation.

## Page state

Server state is projected by `team_inbox_projection`. URL query parameters own
shareable list filters and the directly selected `conversation_id`. Local
storage owns only device-local preferences: sidebar width, filter disclosure,
notification sound, and unsent drafts.

Missing backend capabilities may be represented by the isolated browser demo
adapter. Demo controls must be labelled, remain non-authoritative, and be
replaceable without changing the page's presentational components.

## Loading and failure behaviour

- List, thread, and contact context load independently.
- A request already in flight for the same resource is not repeated.
- Realtime events update safe surfaces in place. A focused composer is never
  replaced; the UI shows a new-activity banner instead.
- When realtime is disconnected, the visible page polls the list projection.
- Partial failures remain inside the affected pane and do not blank the
  workspace.

## Responsive contract

- Desktop: resizable 288–448px list, flexible thread, optional 320px context.
- Tablet: fixed list and thread; context is an overlay drawer.
- Mobile: list or thread, never squeezed columns. Thread provides a back action.
- The document does not scroll; list, timeline, context, and long overlays
  scroll independently.

## Accessibility

All actions use semantic controls and visible focus styles. Dialogs trap focus,
menus expose expanded state, status is not represented by colour alone, touch
targets are at least 40px, and motion respects `prefers-reduced-motion`.
Keyboard shortcuts are disabled while focus is in an editable control.

## Validation

Focused tests cover direct conversation selection, search/filter URL state,
duplicate-send prevention, draft restoration, status and assignment controls,
attachment staging, realtime activity handling, mobile navigation, dark-mode
classes, and keyboard navigation. Repository formatter, linter, type checker,
architecture tests, and relevant service tests remain the merge gate.
