"""In-app notification feed sanitises bodies before serving them.

Regression for two walkthrough bugs: the feed rendered raw email HTML
(``<!DOCTYPE html>…``) and leaked unrendered ``{{token}}`` template
placeholders. The cleaning is display-only and must not persist.
"""

from app.services.notification import _clean_feed_body, notifications


class TestCleanFeedBody:
    def test_strips_email_html(self) -> None:
        out = _clean_feed_body(
            "<!DOCTYPE html><html><body>Your invoice "
            "<b>INV-108250</b> is ready</body></html>"
        )
        assert out is not None
        assert "<" not in out
        assert "DOCTYPE" not in out
        assert "INV-108250" in out

    def test_drops_leaked_template_token(self) -> None:
        assert (
            _clean_feed_body("Outstanding balance of {{amount}} due")
            == "Outstanding balance of due"
        )

    def test_plain_text_unchanged(self) -> None:
        assert _clean_feed_body("Service suspended") == "Service suspended"

    def test_none_and_token_only_collapse_to_none(self) -> None:
        assert _clean_feed_body(None) is None
        assert _clean_feed_body("") == ""
        assert _clean_feed_body("{{amount}}") is None


class TestFeedSanitisedEndToEnd:
    def test_bodies_cleaned_and_storage_untouched(self, db_session, subscriber) -> None:
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )

        raw_html = "<!DOCTYPE html><html><body>Invoice ready</body></html>"
        leaked = "Balance of {{amount}} is due"
        for body in (raw_html, leaked, "Plain message"):
            db_session.add(
                Notification(
                    subscriber_id=subscriber.id,
                    channel=NotificationChannel.email,
                    recipient=subscriber.email,
                    body=body,
                    status=NotificationStatus.delivered,
                    is_active=True,
                )
            )
        db_session.commit()

        resp = notifications.list_response_for_subscriber(
            db_session, subscriber.id, 50, 0
        )
        served = [n.body for n in resp["items"]]
        assert served, "expected notifications in the feed"
        for b in served:
            assert b is None or ("<" not in b and "{{" not in b)

        # Display-only cleaning must never flush back to the database.
        db_session.expire_all()
        stored = [
            n.body
            for n in db_session.query(Notification)
            .filter(Notification.subscriber_id == subscriber.id)
            .all()
        ]
        assert raw_html in stored
