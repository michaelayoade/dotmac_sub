"""Tests for reseller portal services."""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services import reseller_portal


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def clear_sessions():
    """Clear sessions before and after each test."""
    reseller_portal._RESELLER_SESSIONS.clear()
    yield
    reseller_portal._RESELLER_SESSIONS.clear()


@pytest.fixture()
def reseller(db_session):
    """Create a test reseller."""
    from app.models.subscriber import Reseller

    reseller = Reseller(
        name="Test Reseller Co",
        code="TESTRES",
    )
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)
    return reseller


@pytest.fixture()
def reseller_user(db_session, person, reseller):
    """Create a test reseller user."""
    from app.models.subscriber import ResellerUser

    reseller_user = ResellerUser(
        person_id=person.id,
        reseller_id=reseller.id,
        is_active=True,
    )
    db_session.add(reseller_user)
    db_session.commit()
    db_session.refresh(reseller_user)
    return reseller_user


@pytest.fixture()
def reseller_account(db_session, subscriber, reseller):
    """Create a subscriber account linked to a reseller."""
    from app.models.subscriber import SubscriberAccount

    account = SubscriberAccount(
        subscriber_id=subscriber.id,
        reseller_id=reseller.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


# =============================================================================
# Helper Function Tests
# =============================================================================


def test_initials_full_name():
    """Test _initials with full name."""
    person = MagicMock()
    person.first_name = "John"
    person.last_name = "Doe"

    result = reseller_portal._initials(person)
    assert result == "JD"


def test_initials_first_name_only():
    """Test _initials with first name only."""
    person = MagicMock()
    person.first_name = "Jane"
    person.last_name = None

    result = reseller_portal._initials(person)
    assert result == "J"


def test_initials_last_name_only():
    """Test _initials with last name only."""
    person = MagicMock()
    person.first_name = None
    person.last_name = "Smith"

    result = reseller_portal._initials(person)
    assert result == "S"


def test_initials_empty_names():
    """Test _initials with empty names returns default."""
    person = MagicMock()
    person.first_name = ""
    person.last_name = ""

    result = reseller_portal._initials(person)
    assert result == "RS"


def test_initials_whitespace_names():
    """Test _initials with whitespace names returns default."""
    person = MagicMock()
    person.first_name = "   "
    person.last_name = "   "

    result = reseller_portal._initials(person)
    assert result == "RS"


def test_subscriber_label_no_subscriber():
    """Test _subscriber_label with None subscriber."""
    result = reseller_portal._subscriber_label(None)
    assert result == "Account"


def test_subscriber_label_with_person():
    """Test _subscriber_label with person subscriber."""
    person = MagicMock()
    person.first_name = "John"
    person.last_name = "Doe"
    person.display_name = None
    person.organization = None

    subscriber = MagicMock()
    subscriber.person = person

    result = reseller_portal._subscriber_label(subscriber)
    assert result == "John Doe"


def test_subscriber_label_with_person_display_name():
    """Test _subscriber_label falls back to display_name."""
    person = MagicMock()
    person.first_name = ""
    person.last_name = ""
    person.display_name = "Johnny D"
    person.organization = None

    subscriber = MagicMock()
    subscriber.person = person

    result = reseller_portal._subscriber_label(subscriber)
    assert result == "Johnny D"


def test_subscriber_label_with_person_no_names():
    """Test _subscriber_label with person but no names."""
    person = MagicMock()
    person.first_name = None
    person.last_name = None
    person.display_name = None
    person.organization = None

    subscriber = MagicMock()
    subscriber.person = person

    result = reseller_portal._subscriber_label(subscriber)
    assert result == "Customer"


def test_subscriber_label_with_organization_legal_name():
    """Test _subscriber_label with organization legal name (B2B)."""
    org = MagicMock()
    org.legal_name = "ACME Corporation Inc."
    org.name = "ACME Corp"

    person = MagicMock()
    person.organization = org

    subscriber = MagicMock()
    subscriber.person = person

    result = reseller_portal._subscriber_label(subscriber)
    assert result == "ACME Corporation Inc."


def test_subscriber_label_with_organization_name():
    """Test _subscriber_label with organization name (B2B)."""
    org = MagicMock()
    org.legal_name = None
    org.name = "ACME Corp"

    person = MagicMock()
    person.organization = org

    subscriber = MagicMock()
    subscriber.person = person

    result = reseller_portal._subscriber_label(subscriber)
    assert result == "ACME Corp"


def test_subscriber_label_with_empty_organization():
    """Test _subscriber_label with empty organization (B2B with missing names)."""
    org = MagicMock()
    org.legal_name = None
    org.name = None

    person = MagicMock()
    person.first_name = None
    person.last_name = None
    person.display_name = None
    person.organization = org

    subscriber = MagicMock()
    subscriber.person = person

    result = reseller_portal._subscriber_label(subscriber)
    assert result == "Customer"


def test_subscriber_label_no_person_or_org():
    """Test _subscriber_label with no person."""
    subscriber = MagicMock()
    subscriber.person = None

    result = reseller_portal._subscriber_label(subscriber)
    assert result == "Customer"


# =============================================================================
# Session Management Tests
# =============================================================================


def test_create_session():
    """Test _create_session creates valid session."""
    token = reseller_portal._create_session(
        username="reseller@example.com",
        person_id="person-123",
        reseller_id="reseller-456",
        remember=False,
    )

    assert token is not None
    assert len(token) > 20
    assert token in reseller_portal._RESELLER_SESSIONS

    session = reseller_portal._RESELLER_SESSIONS[token]
    assert session["username"] == "reseller@example.com"
    assert session["person_id"] == "person-123"
    assert session["reseller_id"] == "reseller-456"
    assert "created_at" in session
    assert "expires_at" in session


def test_get_session_valid():
    """Test _get_session with valid session."""
    token = reseller_portal._create_session(
        username="test@example.com",
        person_id="person-1",
        reseller_id="reseller-1",
        remember=False,
    )

    session = reseller_portal._get_session(token)
    assert session is not None
    assert session["username"] == "test@example.com"


def test_get_session_not_found():
    """Test _get_session with non-existent token."""
    session = reseller_portal._get_session("invalid-token")
    assert session is None


def test_get_session_expired():
    """Test _get_session with expired session."""
    token = reseller_portal._create_session(
        username="test@example.com",
        person_id="person-1",
        reseller_id="reseller-1",
        remember=False,
    )

    # Manually expire the session (use naive datetime as SQLite does)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    reseller_portal._RESELLER_SESSIONS[token]["expires_at"] = past

    session = reseller_portal._get_session(token)
    assert session is None
    assert token not in reseller_portal._RESELLER_SESSIONS


def test_invalidate_session():
    """Test invalidate_session removes session."""
    token = reseller_portal._create_session(
        username="test@example.com",
        person_id="person-1",
        reseller_id="reseller-1",
        remember=False,
    )
    assert token in reseller_portal._RESELLER_SESSIONS

    reseller_portal.invalidate_session(token)
    assert token not in reseller_portal._RESELLER_SESSIONS


def test_invalidate_session_not_found():
    """Test invalidate_session with non-existent token does nothing."""
    # Should not raise
    reseller_portal.invalidate_session("non-existent-token")


# =============================================================================
# Get Reseller User Tests
# =============================================================================


def test_get_reseller_user_found(db_session, reseller_user, person):
    """Test _get_reseller_user finds active reseller user."""
    result = reseller_portal._get_reseller_user(db_session, str(person.id))
    assert result is not None
    assert result.id == reseller_user.id


def test_get_reseller_user_not_found(db_session):
    """Test _get_reseller_user returns None for unknown person."""
    result = reseller_portal._get_reseller_user(db_session, str(uuid.uuid4()))
    assert result is None


def test_get_reseller_user_inactive(db_session, reseller_user, person):
    """Test _get_reseller_user returns None for inactive user."""
    reseller_user.is_active = False
    db_session.commit()

    result = reseller_portal._get_reseller_user(db_session, str(person.id))
    assert result is None


# =============================================================================
# Login Tests
# =============================================================================


def test_login_mfa_required(db_session):
    """Test login when MFA is required."""
    mock_request = MagicMock()

    with patch.object(reseller_portal, "auth_flow_service") as mock_auth:
        mock_auth.auth_flow.login.return_value = {"mfa_required": True, "mfa_token": "mfa-123"}

        result = reseller_portal.login(
            db_session,
            username="reseller@example.com",
            password="password",
            request=mock_request,
            remember=False,
        )

        assert result["mfa_required"] is True
        assert result["mfa_token"] == "mfa-123"


def test_login_no_access_token(db_session):
    """Test login when no access token returned."""
    mock_request = MagicMock()

    with patch.object(reseller_portal, "auth_flow_service") as mock_auth:
        mock_auth.auth_flow.login.return_value = {}  # No access_token

        with pytest.raises(HTTPException) as exc_info:
            reseller_portal.login(
                db_session,
                username="reseller@example.com",
                password="wrong_password",
                request=mock_request,
                remember=False,
            )

        assert exc_info.value.status_code == 401
        assert "Invalid credentials" in exc_info.value.detail


def test_login_success(db_session, person, reseller_user):
    """Test successful login."""
    from app.models.auth import Session as AuthSession, SessionStatus

    # Create auth session
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    expires_naive = now_naive + timedelta(hours=24)
    token_hash = hashlib.sha256(b"test-token").hexdigest()

    auth_session = AuthSession(
        person_id=person.id,
        status=SessionStatus.active,
        token_hash=token_hash,
        expires_at=expires_naive,
    )
    db_session.add(auth_session)
    db_session.commit()

    mock_request = MagicMock()

    with patch.object(reseller_portal, "auth_flow_service") as mock_auth:
        mock_auth.auth_flow.login.return_value = {"access_token": "test-jwt-token"}
        mock_auth.decode_access_token.return_value = {
            "sub": str(person.id),
            "session_id": str(auth_session.id),
        }
        with patch.object(reseller_portal, "_now", return_value=now_naive):
            result = reseller_portal.login(
                db_session,
                username="reseller@example.com",
                password="password",
                request=mock_request,
                remember=False,
            )

    assert "session_token" in result
    assert "reseller_id" in result
    assert result["reseller_id"] == str(reseller_user.reseller_id)


# =============================================================================
# Verify MFA Tests
# =============================================================================


def test_verify_mfa_no_access_token(db_session):
    """Test verify_mfa when no access token returned."""
    mock_request = MagicMock()

    with patch.object(reseller_portal, "auth_flow_service") as mock_auth:
        mock_auth.auth_flow.mfa_verify.return_value = {}  # No access_token

        with pytest.raises(HTTPException) as exc_info:
            reseller_portal.verify_mfa(
                db_session,
                mfa_token="mfa-token",
                code="123456",
                request=mock_request,
                remember=False,
            )

        assert exc_info.value.status_code == 401
        assert "Invalid verification code" in exc_info.value.detail


def test_verify_mfa_success(db_session, person, reseller_user):
    """Test successful MFA verification."""
    from app.models.auth import Session as AuthSession, SessionStatus

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    expires_naive = now_naive + timedelta(hours=24)
    token_hash = hashlib.sha256(b"mfa-token").hexdigest()

    auth_session = AuthSession(
        person_id=person.id,
        status=SessionStatus.active,
        token_hash=token_hash,
        expires_at=expires_naive,
    )
    db_session.add(auth_session)
    db_session.commit()

    mock_request = MagicMock()

    with patch.object(reseller_portal, "auth_flow_service") as mock_auth:
        mock_auth.auth_flow.mfa_verify.return_value = {"access_token": "test-jwt-token"}
        mock_auth.decode_access_token.return_value = {
            "sub": str(person.id),
            "session_id": str(auth_session.id),
        }
        with patch.object(reseller_portal, "_now", return_value=now_naive):
            result = reseller_portal.verify_mfa(
                db_session,
                mfa_token="mfa-token",
                code="123456",
                request=mock_request,
                remember=False,
            )

    assert "session_token" in result
    assert "reseller_id" in result


# =============================================================================
# Session From Access Token Tests
# =============================================================================


def test_session_from_access_token_missing_sub(db_session):
    """Test _session_from_access_token with missing sub in payload."""
    with patch.object(reseller_portal, "auth_flow_service") as mock_auth:
        mock_auth.decode_access_token.return_value = {"session_id": "sess-123"}

        with pytest.raises(HTTPException) as exc_info:
            reseller_portal._session_from_access_token(db_session, "token", "user@example.com", False)

        assert exc_info.value.status_code == 401
        assert "Invalid session" in exc_info.value.detail


def test_session_from_access_token_missing_session_id(db_session):
    """Test _session_from_access_token with missing session_id."""
    with patch.object(reseller_portal, "auth_flow_service") as mock_auth:
        mock_auth.decode_access_token.return_value = {"sub": "person-123"}

        with pytest.raises(HTTPException) as exc_info:
            reseller_portal._session_from_access_token(db_session, "token", "user@example.com", False)

        assert exc_info.value.status_code == 401
        assert "Invalid session" in exc_info.value.detail


def test_session_from_access_token_auth_session_not_found(db_session):
    """Test _session_from_access_token with non-existent auth session."""
    with patch.object(reseller_portal, "auth_flow_service") as mock_auth:
        mock_auth.decode_access_token.return_value = {
            "sub": str(uuid.uuid4()),
            "session_id": str(uuid.uuid4()),
        }

        with pytest.raises(HTTPException) as exc_info:
            reseller_portal._session_from_access_token(db_session, "token", "user@example.com", False)

        assert exc_info.value.status_code == 401
        assert "Invalid session" in exc_info.value.detail


def test_session_from_access_token_session_inactive(db_session, person):
    """Test _session_from_access_token with inactive session."""
    from app.models.auth import Session as AuthSession, SessionStatus

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    token_hash = hashlib.sha256(b"token").hexdigest()

    auth_session = AuthSession(
        person_id=person.id,
        status=SessionStatus.revoked,  # Inactive
        token_hash=token_hash,
        expires_at=now_naive + timedelta(hours=1),
    )
    db_session.add(auth_session)
    db_session.commit()

    with patch.object(reseller_portal, "auth_flow_service") as mock_auth:
        mock_auth.decode_access_token.return_value = {
            "sub": str(person.id),
            "session_id": str(auth_session.id),
        }

        with pytest.raises(HTTPException) as exc_info:
            reseller_portal._session_from_access_token(db_session, "token", "user@example.com", False)

        assert exc_info.value.status_code == 401


def test_session_from_access_token_session_expired(db_session, person):
    """Test _session_from_access_token with expired session."""
    from app.models.auth import Session as AuthSession, SessionStatus

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    expired_naive = now_naive - timedelta(hours=1)  # Already expired
    token_hash = hashlib.sha256(b"token").hexdigest()

    auth_session = AuthSession(
        person_id=person.id,
        status=SessionStatus.active,
        token_hash=token_hash,
        expires_at=expired_naive,
    )
    db_session.add(auth_session)
    db_session.commit()

    with patch.object(reseller_portal, "auth_flow_service") as mock_auth:
        mock_auth.decode_access_token.return_value = {
            "sub": str(person.id),
            "session_id": str(auth_session.id),
        }
        with patch.object(reseller_portal, "_now", return_value=now_naive):
            with pytest.raises(HTTPException) as exc_info:
                reseller_portal._session_from_access_token(
                    db_session, "token", "user@example.com", False
                )

        assert exc_info.value.status_code == 401
        assert "Session expired" in exc_info.value.detail


def test_session_from_access_token_not_reseller_user(db_session, person):
    """Test _session_from_access_token when person is not reseller user."""
    from app.models.auth import Session as AuthSession, SessionStatus

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    token_hash = hashlib.sha256(b"token").hexdigest()

    auth_session = AuthSession(
        person_id=person.id,
        status=SessionStatus.active,
        token_hash=token_hash,
        expires_at=now_naive + timedelta(hours=1),
    )
    db_session.add(auth_session)
    db_session.commit()

    # No ResellerUser created for this person

    with patch.object(reseller_portal, "auth_flow_service") as mock_auth:
        mock_auth.decode_access_token.return_value = {
            "sub": str(person.id),
            "session_id": str(auth_session.id),
        }
        with patch.object(reseller_portal, "_now", return_value=now_naive):
            with pytest.raises(HTTPException) as exc_info:
                reseller_portal._session_from_access_token(
                    db_session, "token", "user@example.com", False
                )

        assert exc_info.value.status_code == 403
        assert "Reseller access required" in exc_info.value.detail


# =============================================================================
# Get Context Tests
# =============================================================================


def test_get_context_no_token(db_session):
    """Test get_context with no token."""
    result = reseller_portal.get_context(db_session, None)
    assert result is None


def test_get_context_invalid_token(db_session):
    """Test get_context with invalid token."""
    result = reseller_portal.get_context(db_session, "invalid-token")
    assert result is None


def test_get_context_success(db_session, person, reseller, reseller_user):
    """Test get_context returns full context."""
    token = reseller_portal._create_session(
        username=person.email,
        person_id=str(person.id),
        reseller_id=str(reseller.id),
        remember=False,
    )

    result = reseller_portal.get_context(db_session, token)

    assert result is not None
    assert "session" in result
    assert "current_user" in result
    assert "person" in result
    assert "reseller" in result
    assert "reseller_user" in result
    assert result["person"].id == person.id
    assert result["reseller"].id == reseller.id


def test_get_context_person_not_found(db_session, reseller):
    """Test get_context when person not found."""
    token = reseller_portal._create_session(
        username="ghost@example.com",
        person_id=str(uuid.uuid4()),  # Non-existent person
        reseller_id=str(reseller.id),
        remember=False,
    )

    result = reseller_portal.get_context(db_session, token)
    assert result is None


def test_get_context_reseller_not_found(db_session, person):
    """Test get_context when reseller not found."""
    token = reseller_portal._create_session(
        username=person.email,
        person_id=str(person.id),
        reseller_id=str(uuid.uuid4()),  # Non-existent reseller
        remember=False,
    )

    result = reseller_portal.get_context(db_session, token)
    assert result is None


def test_get_context_reseller_user_not_found(db_session, person, reseller):
    """Test get_context when reseller_user not found."""
    # Create session but don't create reseller_user
    token = reseller_portal._create_session(
        username=person.email,
        person_id=str(person.id),
        reseller_id=str(reseller.id),
        remember=False,
    )

    result = reseller_portal.get_context(db_session, token)
    assert result is None


# =============================================================================
# List Accounts Tests
# =============================================================================


def test_list_accounts_empty(db_session, reseller):
    """Test list_accounts with no accounts."""
    result = reseller_portal.list_accounts(
        db_session,
        reseller_id=str(reseller.id),
        limit=10,
        offset=0,
    )

    assert result == []


def test_list_accounts_success(db_session, reseller_account, reseller):
    """Test list_accounts returns accounts."""
    result = reseller_portal.list_accounts(
        db_session,
        reseller_id=str(reseller.id),
        limit=10,
        offset=0,
    )

    assert len(result) == 1
    assert result[0]["id"] == str(reseller_account.id)
    assert result[0]["open_balance"] == 0
    assert result[0]["open_invoices"] == 0


def test_list_accounts_with_invoices(db_session, reseller_account, reseller):
    """Test list_accounts includes invoice balances."""
    from decimal import Decimal
    from app.models.billing import Invoice, InvoiceStatus

    invoice = Invoice(
        account_id=reseller_account.id,
        total=Decimal("100.00"),
        balance_due=Decimal("75.00"),
        status=InvoiceStatus.issued,
    )
    db_session.add(invoice)
    db_session.commit()

    result = reseller_portal.list_accounts(
        db_session,
        reseller_id=str(reseller.id),
        limit=10,
        offset=0,
    )

    assert len(result) == 1
    assert float(result[0]["open_balance"]) == 75.0
    assert result[0]["open_invoices"] == 1


def test_list_accounts_with_payments(db_session, reseller_account, reseller):
    """Test list_accounts includes last payment date."""
    from datetime import datetime, timezone
    from decimal import Decimal
    from app.models.billing import Payment, PaymentStatus

    payment = Payment(
        account_id=reseller_account.id,
        amount=Decimal("50.00"),
        status=PaymentStatus.succeeded,
        paid_at=datetime.now(timezone.utc),
    )
    db_session.add(payment)
    db_session.commit()

    result = reseller_portal.list_accounts(
        db_session,
        reseller_id=str(reseller.id),
        limit=10,
        offset=0,
    )

    assert len(result) == 1
    assert result[0]["last_payment_at"] is not None


def test_list_accounts_pagination(db_session, reseller, subscriber):
    """Test list_accounts with pagination."""
    from app.models.subscriber import SubscriberAccount

    # Create multiple accounts
    for _ in range(5):
        account = SubscriberAccount(
            subscriber_id=subscriber.id,
            reseller_id=reseller.id,
        )
        db_session.add(account)
    db_session.commit()

    # Get first page
    page1 = reseller_portal.list_accounts(
        db_session,
        reseller_id=str(reseller.id),
        limit=2,
        offset=0,
    )
    assert len(page1) == 2

    # Get second page
    page2 = reseller_portal.list_accounts(
        db_session,
        reseller_id=str(reseller.id),
        limit=2,
        offset=2,
    )
    assert len(page2) == 2

    # Verify different accounts
    page1_ids = {a["id"] for a in page1}
    page2_ids = {a["id"] for a in page2}
    assert page1_ids.isdisjoint(page2_ids)


# =============================================================================
# Dashboard Summary Tests
# =============================================================================


def test_get_dashboard_summary_empty(db_session, reseller):
    """Test get_dashboard_summary with no accounts."""
    result = reseller_portal.get_dashboard_summary(
        db_session,
        reseller_id=str(reseller.id),
        limit=10,
        offset=0,
    )

    assert result["accounts"] == []
    assert result["totals"]["accounts"] == 0
    assert result["totals"]["open_balance"] == 0
    assert result["totals"]["open_invoices"] == 0


def test_get_dashboard_summary_with_data(db_session, reseller_account, reseller):
    """Test get_dashboard_summary with account data."""
    from decimal import Decimal
    from app.models.billing import Invoice, InvoiceStatus

    invoice = Invoice(
        account_id=reseller_account.id,
        total=Decimal("200.00"),
        balance_due=Decimal("150.00"),
        status=InvoiceStatus.overdue,
    )
    db_session.add(invoice)
    db_session.commit()

    result = reseller_portal.get_dashboard_summary(
        db_session,
        reseller_id=str(reseller.id),
        limit=10,
        offset=0,
    )

    assert len(result["accounts"]) == 1
    assert result["totals"]["accounts"] == 1
    assert float(result["totals"]["open_balance"]) == 150.0
    assert result["totals"]["open_invoices"] == 1


# =============================================================================
# Create Customer Impersonation Session Tests
# =============================================================================


def test_create_impersonation_session_account_not_found(db_session, reseller):
    """Test create_customer_impersonation_session with invalid account."""
    with pytest.raises(HTTPException) as exc_info:
        reseller_portal.create_customer_impersonation_session(
            db_session,
            reseller_id=str(reseller.id),
            account_id=str(uuid.uuid4()),  # Non-existent
            return_to="/dashboard",
        )

    assert exc_info.value.status_code == 404
    assert "account not found" in exc_info.value.detail.lower()


def test_create_impersonation_session_wrong_reseller(db_session, reseller_account):
    """Test create_customer_impersonation_session with wrong reseller."""
    # Account belongs to different reseller
    with pytest.raises(HTTPException) as exc_info:
        reseller_portal.create_customer_impersonation_session(
            db_session,
            reseller_id=str(uuid.uuid4()),  # Different reseller
            account_id=str(reseller_account.id),
            return_to="/dashboard",
        )

    assert exc_info.value.status_code == 404


def test_create_impersonation_session_success_no_subscriptions(db_session, reseller_account, reseller):
    """Test create_customer_impersonation_session creates session."""
    token = reseller_portal.create_customer_impersonation_session(
        db_session,
        reseller_id=str(reseller.id),
        account_id=str(reseller_account.id),
        return_to="/dashboard",
    )

    assert token is not None
    assert len(token) > 20


def test_create_impersonation_session_with_active_subscription(db_session, reseller, subscriber):
    """Test impersonation session with active subscription."""
    from app.models.subscriber import SubscriberAccount
    from app.schemas.catalog import SubscriptionCreate
    from app.services import catalog as catalog_service

    # Create account linked to reseller
    account = SubscriberAccount(
        subscriber_id=subscriber.id,
        reseller_id=reseller.id,
    )
    db_session.add(account)
    db_session.commit()

    # Create an offer
    from app.schemas.catalog import CatalogOfferCreate
    from app.models.catalog import ServiceType, AccessType, PriceBasis

    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Test Offer",
            code="TEST-OFF",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )

    # Create subscription (defaults to pending)
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=account.id,
            offer_id=offer.id,
        ),
    )

    # Set status to active to hit the active subscription path
    from app.models.catalog import SubscriptionStatus
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    token = reseller_portal.create_customer_impersonation_session(
        db_session,
        reseller_id=str(reseller.id),
        account_id=str(account.id),
        return_to="/dashboard",
    )

    assert token is not None

    # Verify the customer session has the subscription
    from app.services import customer_portal
    session = customer_portal.get_customer_session(token)
    assert session is not None
    assert session["subscription_id"] == str(subscription.id)


def test_create_impersonation_session_with_pending_subscription(db_session, reseller, subscriber):
    """Test impersonation session with non-active subscription (fallback path)."""
    from app.models.subscriber import SubscriberAccount
    from app.schemas.catalog import SubscriptionCreate
    from app.services import catalog as catalog_service

    # Create account linked to reseller
    account = SubscriberAccount(
        subscriber_id=subscriber.id,
        reseller_id=reseller.id,
    )
    db_session.add(account)
    db_session.commit()

    # Create an offer
    from app.schemas.catalog import CatalogOfferCreate
    from app.models.catalog import ServiceType, AccessType, PriceBasis

    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Test Offer 2",
            code="TEST-OFF-2",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )

    # Create subscription - leaves status as default 'pending'
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=account.id,
            offer_id=offer.id,
        ),
    )

    # Subscription is pending (not active), so the any_subs fallback is used
    token = reseller_portal.create_customer_impersonation_session(
        db_session,
        reseller_id=str(reseller.id),
        account_id=str(account.id),
        return_to="/dashboard",
    )

    assert token is not None

    # Verify the customer session has the subscription
    from app.services import customer_portal
    session = customer_portal.get_customer_session(token)
    assert session is not None
    assert session["subscription_id"] == str(subscription.id)
