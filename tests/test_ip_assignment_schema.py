"""IPAssignment schema must accept subscriber_id by field name.

Regression: IPAssignmentBase/Update set validation_alias="account_id" but were
missing populate_by_name=True. Constructing IPAssignmentCreate(subscriber_id=...)
by field name (as provisioning_helpers does on subscription activation) silently
dropped the value, producing an IP assignment with subscriber_id=None and a
NOT NULL violation that poisoned the event-dispatch session.
"""

from uuid import uuid4

from app.models.network import IPVersion
from app.schemas.network import IPAssignmentCreate, IPAssignmentUpdate


def test_create_accepts_subscriber_id_by_field_name():
    sid = uuid4()
    payload = IPAssignmentCreate(
        subscriber_id=sid, ip_version=IPVersion.ipv4, ipv4_address_id=uuid4()
    )
    assert payload.subscriber_id == sid
    # serialized model_dump keeps the value under the field name for the ORM
    assert payload.model_dump()["subscriber_id"] == sid


def test_create_still_accepts_account_id_alias():
    sid = uuid4()
    payload = IPAssignmentCreate(
        account_id=sid, ip_version=IPVersion.ipv4, ipv4_address_id=uuid4()
    )
    assert payload.subscriber_id == sid


def test_update_accepts_subscriber_id_by_field_name():
    sid = uuid4()
    assert IPAssignmentUpdate(subscriber_id=sid).subscriber_id == sid
