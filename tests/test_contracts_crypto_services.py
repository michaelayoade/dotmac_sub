"""Tests for contracts, credential_crypto, numbering, audit_helpers,
and subscription_changes services.

Payment arrangement tests live in tests/test_payment_arrangements.py."""

import uuid
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.models.contracts import ContractSignature
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.services.audit_helpers import (
    build_changes_metadata,
    diff_dicts,
    extract_changes,
    format_changes,
    humanize_action,
    humanize_entity,
    model_to_dict,
)
from app.services.contracts import contract_signatures
from app.services.credential_crypto import (
    ENCRYPTED_CREDENTIAL_FIELDS,
    decrypt_credential,
    decrypt_credential_with_key,
    encrypt_credential,
    encrypt_nas_credentials,
    generate_encryption_key,
    get_encryption_key,
    is_encrypted,
)
from app.services.subscription_changes import subscription_change_requests

# ============================================================================
# credential_crypto tests
# ============================================================================


class TestCredentialCrypto:
    """Tests for credential encryption/decryption utilities."""

    def test_generate_encryption_key_returns_valid_fernet_key(self):
        key = generate_encryption_key()
        assert isinstance(key, str)
        # Verify it's a valid Fernet key by constructing a Fernet instance
        f = Fernet(key.encode("ascii"))
        assert f is not None

    def test_generate_encryption_key_is_unique(self):
        key1 = generate_encryption_key()
        key2 = generate_encryption_key()
        assert key1 != key2

    def test_get_encryption_key_prefers_env_without_openbao_probe(self, monkeypatch):
        key = Fernet.generate_key().decode("ascii")
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key)
        monkeypatch.setattr(
            "app.services.secrets.get_secret",
            lambda *_args, **_kwargs: pytest.fail(
                "OpenBao fallback should not be probed"
            ),
        )

        assert get_encryption_key() == key.encode("ascii")

    def test_encrypt_decrypt_round_trip(self):
        key = Fernet.generate_key().decode("ascii")
        with patch(
            "app.services.credential_crypto.get_encryption_key",
            return_value=key.encode("ascii"),
        ):
            plaintext = "my-secret-password"
            encrypted = encrypt_credential(plaintext)
            assert encrypted is not None
            assert encrypted.startswith("enc:")
            assert plaintext not in encrypted
            decrypted = decrypt_credential(encrypted)
            assert decrypted == plaintext

    def test_encrypt_decrypt_round_trip_unicode(self):
        key = Fernet.generate_key().decode("ascii")
        with patch(
            "app.services.credential_crypto.get_encryption_key",
            return_value=key.encode("ascii"),
        ):
            plaintext = "p@ssw\u00f6rd-\u00e9ncrypt\u00e9d"
            encrypted = encrypt_credential(plaintext)
            decrypted = decrypt_credential(encrypted)
            assert decrypted == plaintext

    def test_encrypt_none_returns_none(self):
        assert encrypt_credential(None) is None

    def test_encrypt_empty_string_returns_empty(self):
        assert encrypt_credential("") == ""

    def test_decrypt_none_returns_none(self):
        assert decrypt_credential(None) is None

    def test_decrypt_empty_string_returns_empty(self):
        assert decrypt_credential("") == ""

    def test_encrypt_no_key_returns_plain_prefix(self):
        with patch(
            "app.services.credential_crypto.get_encryption_key",
            return_value=None,
        ):
            result = encrypt_credential("my-password")
            assert result == "plain:my-password"

    def test_decrypt_plain_prefix(self):
        result = decrypt_credential("plain:my-password")
        assert result == "my-password"

    def test_encrypt_preserves_secret_ref(self):
        key = Fernet.generate_key().decode("ascii")
        ref = "bao://secret/network/olts/olt-1/ssh#password"
        with patch(
            "app.services.credential_crypto.get_encryption_key",
            return_value=key.encode("ascii"),
        ):
            assert encrypt_credential(ref) == ref

    def test_decrypt_resolves_secret_ref(self):
        ref = "bao://secret/network/olts/olt-1/ssh#password"
        with patch("app.services.secrets.resolve_secret", return_value="resolved"):
            assert decrypt_credential(ref) == "resolved"

    def test_encrypt_plain_prefix_reencrypts_when_enforced(self):
        key = Fernet.generate_key()
        with (
            patch(
                "app.services.credential_crypto.get_encryption_key", return_value=key
            ),
            patch("app.services.credential_crypto._encryption_key_required", True),
        ):
            result = encrypt_credential("plain:my-password")

        assert result.startswith("enc:")
        assert decrypt_credential_with_key(result, key) == "my-password"

    def test_decrypt_legacy_no_prefix(self):
        result = decrypt_credential("legacy-password-no-prefix")
        assert result == "legacy-password-no-prefix"

    def test_decrypt_enc_without_key_raises(self):
        with patch(
            "app.services.credential_crypto.get_encryption_key",
            return_value=None,
        ):
            with pytest.raises(ValueError, match="CREDENTIAL_ENCRYPTION_KEY not set"):
                decrypt_credential("enc:some-encrypted-data")

    def test_decrypt_enc_invalid_token_raises(self):
        key = Fernet.generate_key().decode("ascii")
        with patch(
            "app.services.credential_crypto.get_encryption_key",
            return_value=key.encode("ascii"),
        ):
            with pytest.raises(ValueError, match="invalid token"):
                decrypt_credential("enc:not-valid-encrypted-data")

    def test_is_encrypted_enc_prefix(self):
        assert is_encrypted("enc:somedata") is True

    def test_is_encrypted_plain_prefix(self):
        assert is_encrypted("plain:somedata") is True

    def test_is_encrypted_no_prefix(self):
        assert is_encrypted("raw-value") is False

    def test_is_encrypted_none(self):
        assert is_encrypted(None) is False

    def test_is_encrypted_empty(self):
        assert is_encrypted("") is False

    def test_double_encrypt_prevention(self):
        key = Fernet.generate_key().decode("ascii")
        with patch(
            "app.services.credential_crypto.get_encryption_key",
            return_value=key.encode("ascii"),
        ):
            first = encrypt_credential("secret")
            second = encrypt_credential(first)
            # Should not double-encrypt
            assert first == second

    def test_encrypt_nas_credentials(self):
        with patch(
            "app.services.credential_crypto.get_encryption_key",
            return_value=None,
        ):
            data = {
                "name": "NAS-1",
                "shared_secret": "mysecret",
                "ssh_password": "sshpass",
                "hostname": "nas.local",
                "api_password": None,
            }
            result = encrypt_nas_credentials(data)
            assert result["name"] == "NAS-1"
            assert result["hostname"] == "nas.local"
            assert result["shared_secret"] == "plain:mysecret"
            assert result["ssh_password"] == "plain:sshpass"
            # None values should remain None
            assert result["api_password"] is None

    def test_encrypted_credential_fields_frozen(self):
        assert isinstance(ENCRYPTED_CREDENTIAL_FIELDS, frozenset)
        assert "shared_secret" in ENCRYPTED_CREDENTIAL_FIELDS
        assert "ssh_password" in ENCRYPTED_CREDENTIAL_FIELDS
        assert "api_token" in ENCRYPTED_CREDENTIAL_FIELDS


# ============================================================================
# audit_helpers tests
# ============================================================================


class TestAuditHelpers:
    """Tests for audit helper utility functions."""

    def test_model_to_dict_with_subscriber(self, db_session, subscriber):
        result = model_to_dict(subscriber)
        assert "id" in result
        assert "first_name" in result
        assert result["first_name"] == "Test"
        # Sensitive fields should be excluded
        assert "password" not in result
        assert "password_hash" not in result

    def test_model_to_dict_none(self):
        result = model_to_dict(None)
        assert result == {}

    def test_model_to_dict_include_filter(self, db_session, subscriber):
        result = model_to_dict(subscriber, include={"first_name", "last_name"})
        assert "first_name" in result
        assert "last_name" in result
        assert "email" not in result

    def test_model_to_dict_exclude_filter(self, db_session, subscriber):
        result = model_to_dict(subscriber, exclude={"email"})
        assert "email" not in result
        assert "first_name" in result

    def test_diff_dicts_no_changes(self):
        d = {"a": 1, "b": 2}
        assert diff_dicts(d, d) == {}

    def test_diff_dicts_with_changes(self):
        before = {"a": 1, "b": 2, "c": 3}
        after = {"a": 1, "b": 99, "c": 3}
        result = diff_dicts(before, after)
        assert result == {"b": {"from": 2, "to": 99}}

    def test_diff_dicts_added_key(self):
        before = {"a": 1}
        after = {"a": 1, "b": 2}
        result = diff_dicts(before, after)
        assert result == {"b": {"from": None, "to": 2}}

    def test_diff_dicts_removed_key(self):
        before = {"a": 1, "b": 2}
        after = {"a": 1}
        result = diff_dicts(before, after)
        assert result == {"b": {"from": 2, "to": None}}

    def test_build_changes_metadata_with_changes(self, db_session, subscriber):
        before_dict = model_to_dict(subscriber)
        subscriber.first_name = "Updated"
        after_dict = model_to_dict(subscriber)
        changes = diff_dicts(before_dict, after_dict)
        assert "first_name" in changes
        assert changes["first_name"]["from"] == "Test"
        assert changes["first_name"]["to"] == "Updated"

    def test_build_changes_metadata_no_changes(self, db_session, subscriber):
        result = build_changes_metadata(subscriber, subscriber)
        assert result is None

    def test_format_changes_none(self):
        assert format_changes(None) is None

    def test_format_changes_empty(self):
        assert format_changes({}) is None

    def test_format_changes_single(self):
        changes = {"status": {"from": "pending", "to": "active"}}
        result = format_changes(changes)
        assert result == "status: pending -> active"

    def test_format_changes_truncated(self):
        changes = {
            "a": {"from": 1, "to": 2},
            "b": {"from": 3, "to": 4},
            "c": {"from": 5, "to": 6},
            "d": {"from": 7, "to": 8},
        }
        result = format_changes(changes, max_items=2)
        assert result is not None
        assert result.endswith("\u2026")
        assert result.count(";") == 1

    def test_format_changes_exact_max(self):
        changes = {
            "a": {"from": 1, "to": 2},
            "b": {"from": 3, "to": 4},
            "c": {"from": 5, "to": 6},
        }
        result = format_changes(changes, max_items=3)
        assert result is not None
        assert not result.endswith("\u2026")

    def test_extract_changes_none(self):
        assert extract_changes(None) is None

    def test_extract_changes_empty(self):
        assert extract_changes({}) is None

    def test_extract_changes_with_changes_key(self):
        metadata = {"changes": {"name": {"from": "A", "to": "B"}}}
        result = extract_changes(metadata)
        assert result == {"name": {"from": "A", "to": "B"}}

    def test_extract_changes_from_to_status_change(self):
        metadata = {"from": "pending", "to": "active"}
        result = extract_changes(metadata, action="status_change")
        assert result == {"status": {"from": "pending", "to": "active"}}

    def test_extract_changes_from_to_priority_change(self):
        metadata = {"from": "low", "to": "high"}
        result = extract_changes(metadata, action="priority_change")
        assert result == {"priority": {"from": "low", "to": "high"}}

    def test_extract_changes_from_to_generic(self):
        metadata = {"from": "a", "to": "b"}
        result = extract_changes(metadata, action="some_action")
        assert result == {"value": {"from": "a", "to": "b"}}

    def test_humanize_action_none(self):
        assert humanize_action(None) == "Activity"

    def test_humanize_action_http_methods(self):
        assert humanize_action("GET") == "Viewed"
        assert humanize_action("POST") == "Created"
        assert humanize_action("PUT") == "Updated"
        assert humanize_action("PATCH") == "Updated"
        assert humanize_action("DELETE") == "Deleted"

    def test_humanize_action_case_insensitive(self):
        assert humanize_action("get") == "Viewed"
        assert humanize_action("post") == "Created"

    def test_humanize_action_custom(self):
        assert humanize_action("status_change") == "Status Change"
        assert humanize_action("reset-password") == "Reset Password"

    def test_humanize_entity_none(self):
        assert humanize_entity(None) == "Item"

    def test_humanize_entity_simple(self):
        assert humanize_entity("subscriber") == "Subscriber"

    def test_humanize_entity_with_id(self):
        result = humanize_entity("subscriber", "12345678-abcd-efgh-1234-567890abcdef")
        assert result == "Subscriber #12345678"

    def test_humanize_entity_path(self):
        result = humanize_entity("/admin/subscribers")
        assert result == "Subscribers"

    def test_humanize_entity_path_with_segments(self):
        result = humanize_entity("/admin/system/users")
        assert result == "Users"

    def test_humanize_entity_path_with_numeric_id(self):
        result = humanize_entity("/api/subscribers/123")
        assert result == "Subscribers"

    def test_humanize_entity_underscore(self):
        result = humanize_entity("service_order")
        assert result == "Service Order"


# ============================================================================
# numbering tests
# ============================================================================


class TestNumbering:
    """Tests for number generation service."""

    def test_format_number_with_prefix_and_padding(self):
        from app.services.numbering import _format_number

        result = _format_number("INV-", 6, 42)
        assert result == "INV-000042"

    def test_format_number_no_prefix(self):
        from app.services.numbering import _format_number

        result = _format_number(None, 4, 7)
        assert result == "0007"

    def test_format_number_no_padding(self):
        from app.services.numbering import _format_number

        result = _format_number("SO-", None, 123)
        assert result == "SO-123"

    def test_format_number_zero_padding(self):
        from app.services.numbering import _format_number

        result = _format_number("", 0, 99)
        assert result == "99"

    def test_next_sequence_value_creates_new(self, db_session):
        from app.models.sequence import DocumentSequence  # noqa: F401

        DocumentSequence.__table__.create(db_session.get_bind(), checkfirst=True)
        from app.services.numbering import _next_sequence_value

        value = _next_sequence_value(db_session, "test_seq_new", 100)
        assert value == 100

    def test_next_sequence_value_increments(self, db_session):
        from app.models.sequence import DocumentSequence  # noqa: F401

        DocumentSequence.__table__.create(db_session.get_bind(), checkfirst=True)
        from app.services.numbering import _next_sequence_value

        v1 = _next_sequence_value(db_session, "test_seq_inc", 1)
        v2 = _next_sequence_value(db_session, "test_seq_inc", 1)
        assert v1 == 1
        assert v2 == 2

    def test_generate_number_disabled(self, db_session):
        from app.models.domain_settings import SettingDomain
        from app.services.numbering import generate_number

        with patch(
            "app.services.numbering._resolve_setting",
            side_effect=lambda db, domain, key: {
                "enabled": False,
            }.get(key),
        ):
            result = generate_number(
                db_session,
                SettingDomain.billing,
                "test_invoice_seq",
                "enabled",
                "prefix",
                "padding",
                "start",
            )
            assert result is None

    def test_generate_number_enabled(self, db_session):
        from app.models.domain_settings import SettingDomain
        from app.models.sequence import DocumentSequence  # noqa: F401

        DocumentSequence.__table__.create(db_session.get_bind(), checkfirst=True)
        from app.services.numbering import generate_number

        settings_map = {
            "inv_enabled": True,
            "inv_prefix": "INV-",
            "inv_padding": 5,
            "inv_start": 1000,
        }
        with patch(
            "app.services.numbering._resolve_setting",
            side_effect=lambda db, domain, key: settings_map.get(key),
        ):
            result = generate_number(
                db_session,
                SettingDomain.billing,
                "test_gen_num_seq",
                "inv_enabled",
                "inv_prefix",
                "inv_padding",
                "inv_start",
            )
            assert result == "INV-01000"


# ============================================================================
# contracts tests
# ============================================================================


class TestContracts:
    """Tests for contract signature service."""

    def _create_service_order(self, db_session, subscriber):
        """Helper to create a service order."""
        order = ServiceOrder(
            subscriber_id=subscriber.id,
            status=ServiceOrderStatus.draft,
            order_type="new_install",
        )
        db_session.add(order)
        db_session.commit()
        db_session.refresh(order)
        return order

    def _create_signature(self, db_session, subscriber, service_order=None):
        """Helper to create a contract signature directly."""
        sig = ContractSignature(
            subscriber_id=subscriber.id,
            service_order_id=service_order.id if service_order else None,
            signer_name="Test Signer",
            signer_email="signer@example.com",
            ip_address="127.0.0.1",
            user_agent="TestAgent/1.0",
            agreement_text="I agree to terms.",
            signed_at=datetime.now(UTC),
        )
        db_session.add(sig)
        db_session.commit()
        db_session.refresh(sig)
        return sig

    def test_get_signature(self, db_session, subscriber):
        sig = self._create_signature(db_session, subscriber)
        fetched = contract_signatures.get(db_session, str(sig.id))
        assert fetched.id == sig.id
        assert fetched.signer_name == "Test Signer"

    def test_get_signature_not_found(self, db_session):
        fake_id = str(uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            contract_signatures.get(db_session, fake_id)
        assert exc_info.value.status_code == 404

    def test_get_for_service_order(self, db_session, subscriber):
        order = self._create_service_order(db_session, subscriber)
        sig = self._create_signature(db_session, subscriber, service_order=order)
        fetched = contract_signatures.get_for_service_order(db_session, str(order.id))
        assert fetched is not None
        assert fetched.id == sig.id

    def test_get_for_service_order_none(self, db_session, subscriber):
        order = self._create_service_order(db_session, subscriber)
        fetched = contract_signatures.get_for_service_order(db_session, str(order.id))
        assert fetched is None

    def test_is_signed_true(self, db_session, subscriber):
        order = self._create_service_order(db_session, subscriber)
        self._create_signature(db_session, subscriber, service_order=order)
        assert contract_signatures.is_signed(db_session, str(order.id)) is True

    def test_is_signed_false(self, db_session, subscriber):
        order = self._create_service_order(db_session, subscriber)
        assert contract_signatures.is_signed(db_session, str(order.id)) is False

    def test_list_for_account(self, db_session, subscriber):
        sig1 = self._create_signature(db_session, subscriber)
        sig2 = self._create_signature(db_session, subscriber)
        sigs = contract_signatures.list_for_account(db_session, str(subscriber.id))
        sig_ids = [s.id for s in sigs]
        assert sig1.id in sig_ids
        assert sig2.id in sig_ids

    def test_list_for_account_empty(self, db_session):
        fake_id = str(uuid.uuid4())
        sigs = contract_signatures.list_for_account(db_session, fake_id)
        assert sigs == []

    def test_list_for_account_pagination(self, db_session, subscriber):
        for _ in range(5):
            self._create_signature(db_session, subscriber)
        sigs = contract_signatures.list_for_account(
            db_session, str(subscriber.id), limit=2, offset=0
        )
        assert len(sigs) == 2

    def test_list_for_account_with_offset(self, db_session, subscriber):
        for _ in range(5):
            self._create_signature(db_session, subscriber)
        all_sigs = contract_signatures.list_for_account(
            db_session, str(subscriber.id), limit=100
        )
        offset_sigs = contract_signatures.list_for_account(
            db_session, str(subscriber.id), limit=100, offset=2
        )
        assert len(offset_sigs) == len(all_sigs) - 2

    def test_inactive_signature_excluded(self, db_session, subscriber):
        order = self._create_service_order(db_session, subscriber)
        sig = self._create_signature(db_session, subscriber, service_order=order)
        sig.is_active = False
        db_session.commit()
        fetched = contract_signatures.get_for_service_order(db_session, str(order.id))
        assert fetched is None

    def test_get_contract_context_not_found(self, db_session, subscriber):
        fake_id = str(uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            contract_signatures.get_contract_context(
                db_session, fake_id, str(subscriber.id)
            )
        assert exc_info.value.status_code == 404

    def test_get_contract_context_access_denied(self, db_session, subscriber):
        order = self._create_service_order(db_session, subscriber)
        other_account_id = str(uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            contract_signatures.get_contract_context(
                db_session, str(order.id), other_account_id
            )
        assert exc_info.value.status_code == 403

    def test_get_contract_context_already_signed(self, db_session, subscriber):
        order = self._create_service_order(db_session, subscriber)
        self._create_signature(db_session, subscriber, service_order=order)
        result = contract_signatures.get_contract_context(
            db_session, str(order.id), str(subscriber.id)
        )
        assert "redirect" in result

    def test_get_contract_context_default_html(self, db_session, subscriber):
        order = self._create_service_order(db_session, subscriber)
        result = contract_signatures.get_contract_context(
            db_session, str(order.id), str(subscriber.id)
        )
        assert "contract_html" in result
        assert "Service Agreement" in result["contract_html"]
        assert result["service_order"] is not None
        assert result["document_id"] is None


# ============================================================================
# subscription_changes tests
# ============================================================================


def _create_change_request_directly(
    db_session,
    subscription,
    new_offer,
    effective_date=None,
    notes=None,
):
    """Create a SubscriptionChangeRequest directly via the ORM."""
    cr = SubscriptionChangeRequest(
        subscription_id=subscription.id,
        current_offer_id=subscription.offer_id,
        requested_offer_id=new_offer.id,
        effective_date=effective_date or date(2025, 7, 1),
        status=SubscriptionChangeStatus.pending,
        notes=notes,
    )
    db_session.add(cr)
    db_session.commit()
    db_session.refresh(cr)
    return cr


class TestSubscriptionChanges:
    """Tests for subscription change request service."""

    def _create_second_offer(self, db_session):
        """Create a second catalog offer for change requests."""
        from app.models.catalog import AccessType, CatalogOffer, PriceBasis, ServiceType

        offer = CatalogOffer(
            name="Premium Internet",
            code=f"PRM-INT-{uuid.uuid4().hex[:6]}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        )
        db_session.add(offer)
        db_session.commit()
        db_session.refresh(offer)
        return offer

    def test_get_change_request(self, db_session, subscription):
        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(db_session, subscription, new_offer)
        fetched = subscription_change_requests.get(db_session, str(cr.id))
        assert fetched.id == cr.id
        assert fetched.status == SubscriptionChangeStatus.pending

    def test_get_change_request_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            subscription_change_requests.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_approve_change_request(self, db_session, subscription):
        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(db_session, subscription, new_offer)
        # Approve without reviewer_id to avoid the field name mismatch
        approved = subscription_change_requests.approve(db_session, str(cr.id))
        assert approved.status == SubscriptionChangeStatus.approved
        assert approved.reviewed_at is not None

    def test_approve_non_pending_raises(self, db_session, subscription):
        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(db_session, subscription, new_offer)
        subscription_change_requests.approve(db_session, str(cr.id))
        with pytest.raises(HTTPException) as exc_info:
            subscription_change_requests.approve(db_session, str(cr.id))
        assert exc_info.value.status_code == 400

    def test_approve_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            subscription_change_requests.approve(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_reject_change_request(self, db_session, subscription):
        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(db_session, subscription, new_offer)
        rejected = subscription_change_requests.reject(
            db_session, str(cr.id), reason="Not eligible"
        )
        assert rejected.status == SubscriptionChangeStatus.rejected
        assert rejected.rejection_reason == "Not eligible"

    def test_reject_non_pending_raises(self, db_session, subscription):
        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(db_session, subscription, new_offer)
        subscription_change_requests.reject(db_session, str(cr.id))
        with pytest.raises(HTTPException) as exc_info:
            subscription_change_requests.reject(db_session, str(cr.id))
        assert exc_info.value.status_code == 400

    def test_reject_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            subscription_change_requests.reject(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_apply_change_request(self, db_session, subscription):
        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(db_session, subscription, new_offer)
        subscription_change_requests.approve(db_session, str(cr.id))
        applied = subscription_change_requests.apply(db_session, str(cr.id))
        assert applied.status == SubscriptionChangeStatus.applied
        assert applied.applied_at is not None

        db_session.refresh(subscription)
        assert subscription.offer_id == new_offer.id

    def test_apply_change_request_routes_through_shared_subscription_update(
        self, db_session, subscription
    ):
        from unittest.mock import patch

        from app.services import catalog as catalog_service

        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(db_session, subscription, new_offer)
        subscription_change_requests.approve(db_session, str(cr.id))

        with patch.object(catalog_service.subscriptions, "update") as update_mock:
            update_mock.return_value = subscription
            applied = subscription_change_requests.apply(db_session, str(cr.id))

        assert applied.status == SubscriptionChangeStatus.applied
        update_mock.assert_called_once()
        assert update_mock.call_args.args[1] == str(subscription.id)
        assert str(update_mock.call_args.args[2].offer_id) == str(new_offer.id)

    def test_apply_non_approved_raises(self, db_session, subscription):
        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(db_session, subscription, new_offer)
        with pytest.raises(HTTPException) as exc_info:
            subscription_change_requests.apply(db_session, str(cr.id))
        assert exc_info.value.status_code == 400

    def test_apply_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            subscription_change_requests.apply(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_cancel_change_request(self, db_session, subscription):
        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(db_session, subscription, new_offer)
        canceled = subscription_change_requests.cancel(
            db_session, str(cr.id), notes="Changed mind"
        )
        assert canceled.status == SubscriptionChangeStatus.canceled
        assert "Changed mind" in canceled.notes

    def test_cancel_non_pending_raises(self, db_session, subscription):
        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(db_session, subscription, new_offer)
        subscription_change_requests.approve(db_session, str(cr.id))
        with pytest.raises(HTTPException) as exc_info:
            subscription_change_requests.cancel(db_session, str(cr.id))
        assert exc_info.value.status_code == 400

    def test_cancel_not_found(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            subscription_change_requests.cancel(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_list_change_requests(self, db_session, subscription):
        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(db_session, subscription, new_offer)
        results = subscription_change_requests.list(
            db=db_session,
            subscription_id=str(subscription.id),
            account_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
        assert any(r.id == cr.id for r in results)

    def test_list_change_requests_by_status(self, db_session, subscription):
        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(db_session, subscription, new_offer)
        results = subscription_change_requests.list(
            db=db_session,
            subscription_id=None,
            account_id=None,
            status="pending",
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
        assert any(r.id == cr.id for r in results)

    def test_cancel_with_existing_notes(self, db_session, subscription):
        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(
            db_session, subscription, new_offer, notes="Original note"
        )
        canceled = subscription_change_requests.cancel(
            db_session, str(cr.id), notes="Cancellation reason"
        )
        assert "Original note" in canceled.notes
        assert "Cancellation reason" in canceled.notes

    def test_change_request_model_fields(self, db_session, subscription, catalog_offer):
        new_offer = self._create_second_offer(db_session)
        cr = _create_change_request_directly(db_session, subscription, new_offer)
        assert cr.subscription_id == subscription.id
        assert cr.current_offer_id == catalog_offer.id
        assert cr.requested_offer_id == new_offer.id
        assert cr.effective_date == date(2025, 7, 1)
