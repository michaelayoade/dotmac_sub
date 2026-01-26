# SLA Breach Detection & Ticket Auto-Assignment Implementation Plan

## Overview

Implement two workflow automation features:
1. **SLA Breach Detection Task**: A scheduled task that monitors SLA clocks and creates breach records when exceeded
2. **Ticket Auto-Assignment**: Service-layer logic to automatically assign tickets to available agents using round-robin by team/skill

---

## Current Infrastructure

### Existing Models (`app/models/workflow.py`)
- `SlaPolicy`: SLA configuration by entity type
- `SlaTarget`: Time targets per priority level (target_minutes, warning_minutes)
- `SlaClock`: Active SLA timers with status (running, paused, completed, breached)
- `SlaBreach`: Records of SLA violations

### Existing Models (`app/models/crm/team.py`)
- `CrmTeam`: Support teams
- `CrmAgent`: Support agents linked to Person
- `CrmAgentTeam`: Agent-to-team membership

### Existing Task Pattern (`app/tasks/*.py`)
```python
@celery_app.task(name="app.tasks.module.task_name")
def task_name():
    session = SessionLocal()
    try:
        service_module.do_work(session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

---

## Phase 1: SLA Breach Detection Task

### 1.1 Create Task File

**File**: `app/tasks/workflow.py` (new)

```python
"""SLA and workflow automation tasks."""

from app.celery_app import celery_app
from app.db import SessionLocal
from app.services import workflow as workflow_service


@celery_app.task(name="app.tasks.workflow.check_sla_breaches")
def check_sla_breaches():
    """
    Check all running SLA clocks for breaches.
    Creates SlaBreach records when due_at is exceeded.
    Run every 30 minutes.
    """
    session = SessionLocal()
    try:
        workflow_service.detect_sla_breaches(session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

### 1.2 Add Service Function

**File**: `app/services/workflow.py` (add to existing)

```python
from datetime import datetime, timezone
from sqlalchemy import and_
from app.models.workflow import SlaClock, SlaBreach, SlaClockStatus, SlaBreachStatus

def detect_sla_breaches(db: Session) -> list[SlaBreach]:
    """
    Find all running SLA clocks past their due_at and create breach records.

    Returns:
        List of newly created SlaBreach records
    """
    now = datetime.now(timezone.utc)

    # Find running clocks past due
    overdue_clocks = (
        db.query(SlaClock)
        .filter(
            and_(
                SlaClock.status == SlaClockStatus.running,
                SlaClock.due_at < now,
                SlaClock.breached_at.is_(None),
            )
        )
        .all()
    )

    breaches = []
    for clock in overdue_clocks:
        # Update clock status
        clock.status = SlaClockStatus.breached
        clock.breached_at = now

        # Create breach record
        breach = SlaBreach(
            clock_id=clock.id,
            status=SlaBreachStatus.open,
            breached_at=now,
            notes=f"SLA breached: {clock.entity_type.value} {clock.entity_id}",
        )
        db.add(breach)
        breaches.append(breach)

    db.commit()
    return breaches
```

### 1.3 Register Task with Scheduler

**Option A**: Add to celery beat schedule in `app/celery_app.py`
```python
celery_app.conf.beat_schedule = {
    'check-sla-breaches-every-30-minutes': {
        'task': 'app.tasks.workflow.check_sla_breaches',
        'schedule': 1800.0,  # 30 minutes in seconds
    },
    # ... existing schedules
}
```

**Option B**: Add to database-driven scheduler via `ScheduledTask` model

### 1.4 Register Task in `app/tasks/__init__.py`

```python
from app.tasks.workflow import check_sla_breaches
```

---

## Phase 2: Ticket Auto-Assignment

### 2.1 Add Auto-Assignment Configuration

**File**: `app/models/workflow.py` (add)

```python
class TicketAssignmentRule(Base):
    """Rules for automatic ticket assignment."""
    __tablename__ = "ticket_assignment_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    priority = Column(Integer, default=0)  # Higher = checked first

    # Matching criteria
    channel = Column(String(50), nullable=True)  # email, web, phone, etc
    ticket_priority = Column(String(50), nullable=True)  # low, normal, high, urgent

    # Assignment target
    team_id = Column(UUID(as_uuid=True), ForeignKey("crm_teams.id"), nullable=True)
    skill_ids = Column(JSON, nullable=True)  # List of required skill IDs

    # Assignment method
    assignment_method = Column(String(50), default="round_robin")  # round_robin, least_loaded

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
```

### 2.2 Track Assignment Round-Robin

**File**: `app/models/workflow.py` (add)

```python
class TeamAssignmentCounter(Base):
    """Tracks round-robin position per team."""
    __tablename__ = "team_assignment_counters"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("crm_teams.id"), unique=True, nullable=False)
    last_assigned_agent_id = Column(UUID(as_uuid=True), ForeignKey("crm_agents.id"), nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
```

### 2.3 Add Auto-Assignment Service

**File**: `app/services/workflow.py` (add to existing)

```python
from app.models.crm.team import CrmTeam, CrmAgent, CrmAgentTeam
from app.models.tickets import Ticket

def auto_assign_ticket(db: Session, ticket: Ticket) -> Ticket | None:
    """
    Automatically assign a ticket to an available agent.
    Uses round-robin by team/skill.

    Args:
        db: Database session
        ticket: Ticket to assign

    Returns:
        Updated ticket if assigned, None if no agent available
    """
    if ticket.assigned_to_person_id:
        return ticket  # Already assigned

    # Find matching rule
    rule = _find_matching_rule(db, ticket)
    if not rule:
        return None

    # Get next agent using round-robin
    agent = _get_next_agent(db, rule)
    if not agent:
        return None

    # Assign ticket
    ticket.assigned_to_person_id = agent.person_id
    ticket.status = TicketStatus.open  # Auto-open when assigned

    # Update round-robin counter
    _update_assignment_counter(db, rule.team_id, agent.id)

    db.commit()
    return ticket


def _find_matching_rule(db: Session, ticket: Ticket) -> TicketAssignmentRule | None:
    """Find the highest priority rule matching the ticket."""
    query = (
        db.query(TicketAssignmentRule)
        .filter(TicketAssignmentRule.is_active == True)
        .order_by(TicketAssignmentRule.priority.desc())
    )

    for rule in query.all():
        if rule.channel and rule.channel != ticket.channel.value:
            continue
        if rule.ticket_priority and rule.ticket_priority != ticket.priority.value:
            continue
        return rule

    return None


def _get_next_agent(db: Session, rule: TicketAssignmentRule) -> CrmAgent | None:
    """Get next available agent using round-robin."""
    if not rule.team_id:
        return None

    # Get all active agents in team
    agents = (
        db.query(CrmAgent)
        .join(CrmAgentTeam, CrmAgent.id == CrmAgentTeam.agent_id)
        .filter(
            CrmAgentTeam.team_id == rule.team_id,
            CrmAgent.is_active == True,
        )
        .order_by(CrmAgent.id)  # Consistent ordering
        .all()
    )

    if not agents:
        return None

    # Get last assigned agent for round-robin
    counter = (
        db.query(TeamAssignmentCounter)
        .filter(TeamAssignmentCounter.team_id == rule.team_id)
        .first()
    )

    if not counter or not counter.last_assigned_agent_id:
        return agents[0]

    # Find next agent after last assigned
    agent_ids = [a.id for a in agents]
    try:
        last_idx = agent_ids.index(counter.last_assigned_agent_id)
        next_idx = (last_idx + 1) % len(agents)
    except ValueError:
        next_idx = 0

    return agents[next_idx]


def _update_assignment_counter(db: Session, team_id, agent_id):
    """Update round-robin counter for team."""
    counter = (
        db.query(TeamAssignmentCounter)
        .filter(TeamAssignmentCounter.team_id == team_id)
        .first()
    )

    if counter:
        counter.last_assigned_agent_id = agent_id
    else:
        counter = TeamAssignmentCounter(
            team_id=team_id,
            last_assigned_agent_id=agent_id,
        )
        db.add(counter)
```

### 2.4 Integrate with Ticket Creation

**File**: `app/services/tickets.py` (modify `create` method)

```python
from app.services import workflow as workflow_service

class Tickets(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: TicketCreate) -> Ticket:
        # ... existing validation ...

        ticket = Ticket(**payload.model_dump())
        db.add(ticket)
        db.flush()  # Get ID before auto-assign

        # Auto-assign if no assignee specified
        if not ticket.assigned_to_person_id:
            workflow_service.auto_assign_ticket(db, ticket)

        db.commit()
        db.refresh(ticket)
        return ticket
```

---

## Phase 3: Database Migration

**File**: `alembic/versions/xxx_add_ticket_assignment_tables.py`

```python
def upgrade():
    # Create ticket_assignment_rules table
    op.create_table(
        'ticket_assignment_rules',
        sa.Column('id', sa.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('priority', sa.Integer, default=0),
        sa.Column('channel', sa.String(50), nullable=True),
        sa.Column('ticket_priority', sa.String(50), nullable=True),
        sa.Column('team_id', sa.UUID(as_uuid=True), sa.ForeignKey('crm_teams.id'), nullable=True),
        sa.Column('skill_ids', sa.JSON, nullable=True),
        sa.Column('assignment_method', sa.String(50), default='round_robin'),
        sa.Column('is_active', sa.Boolean, default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now()),
    )

    # Create team_assignment_counters table
    op.create_table(
        'team_assignment_counters',
        sa.Column('id', sa.UUID(as_uuid=True), primary_key=True),
        sa.Column('team_id', sa.UUID(as_uuid=True), sa.ForeignKey('crm_teams.id'), unique=True, nullable=False),
        sa.Column('last_assigned_agent_id', sa.UUID(as_uuid=True), sa.ForeignKey('crm_agents.id'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now()),
    )

def downgrade():
    op.drop_table('team_assignment_counters')
    op.drop_table('ticket_assignment_rules')
```

---

## Files Summary

### New Files
| File | Description |
|------|-------------|
| `app/tasks/workflow.py` | SLA breach detection task |
| `alembic/versions/xxx_add_ticket_assignment_tables.py` | Database migration |

### Modified Files
| File | Changes |
|------|---------|
| `app/models/workflow.py` | Add `TicketAssignmentRule`, `TeamAssignmentCounter` models |
| `app/services/workflow.py` | Add `detect_sla_breaches()`, `auto_assign_ticket()` |
| `app/services/tickets.py` | Integrate auto-assignment in `create()` |
| `app/tasks/__init__.py` | Register workflow task |
| `app/celery_app.py` | Add beat schedule for SLA check |

---

## Implementation Order

1. Add models to `app/models/workflow.py`
2. Create database migration
3. Run migration: `alembic upgrade head`
4. Add service functions to `app/services/workflow.py`
5. Create `app/tasks/workflow.py`
6. Register task in `app/tasks/__init__.py`
7. Add celery beat schedule
8. Modify ticket creation to call auto-assign

---

## Testing Checklist

### SLA Breach Detection
- [ ] Running clocks past due_at get status changed to breached
- [ ] SlaBreach records created with correct clock_id
- [ ] Paused clocks are not checked
- [ ] Already breached clocks are not re-processed
- [ ] Task runs without error on empty database

### Ticket Auto-Assignment
- [ ] New tickets without assignee get auto-assigned
- [ ] Tickets with pre-set assignee are not modified
- [ ] Round-robin cycles through all team agents
- [ ] Channel/priority filters work correctly
- [ ] Missing team returns None (no assignment)
- [ ] Empty team returns None (no assignment)
