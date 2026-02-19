"""Tests for customer_search service."""


from app.models.subscriber import Organization
from app.services import customer_search as customer_search_service


class TestSearch:
    """Tests for search function."""

    def test_returns_empty_for_empty_query(self, db_session):
        """Test returns empty list for empty query."""
        result = customer_search_service.search(db_session, "")
        assert result == []

    def test_returns_empty_for_none_query(self, db_session):
        """Test returns empty list for None query."""
        result = customer_search_service.search(db_session, None)
        assert result == []

    def test_returns_empty_for_whitespace_query(self, db_session):
        """Test returns empty list for whitespace only query."""
        result = customer_search_service.search(db_session, "   ")
        assert result == []

    def test_finds_person_by_first_name(self, db_session, person):
        """Test finds person by first name."""
        result = customer_search_service.search(db_session, person.first_name)
        assert len(result) >= 1
        person_result = next((r for r in result if r["id"] == person.id), None)
        assert person_result is not None
        assert person_result["type"] == "person"
        assert "ref" in person_result

    def test_finds_person_by_last_name(self, db_session, person):
        """Test finds person by last name."""
        result = customer_search_service.search(db_session, person.last_name)
        assert len(result) >= 1
        person_result = next((r for r in result if r["id"] == person.id), None)
        assert person_result is not None

    def test_finds_person_by_email(self, db_session, person):
        """Test finds person by email."""
        result = customer_search_service.search(db_session, person.email.split("@")[0])
        assert len(result) >= 1

    def test_finds_organization_by_name(self, db_session):
        """Test finds organization by name."""
        org = Organization(name="Acme Corporation")
        db_session.add(org)
        db_session.commit()

        result = customer_search_service.search(db_session, "Acme")
        assert len(result) >= 1
        org_result = next((r for r in result if r["id"] == org.id), None)
        assert org_result is not None
        assert org_result["type"] == "organization"
        assert org_result["ref"] == f"organization:{org.id}"

    def test_finds_organization_by_domain(self, db_session):
        """Test finds organization by domain."""
        org = Organization(name="Test Corp", domain="testcorp.com")
        db_session.add(org)
        db_session.commit()

        result = customer_search_service.search(db_session, "testcorp")
        assert len(result) >= 1
        org_result = next((r for r in result if r["id"] == org.id), None)
        assert org_result is not None
        assert "testcorp.com" in org_result["label"]

    def test_respects_limit(self, db_session, person):
        """Test respects limit parameter."""
        result = customer_search_service.search(db_session, "Test", limit=1)
        assert len(result) <= 1

    def test_person_label_includes_email(self, db_session, person):
        """Test person label includes email when present."""
        result = customer_search_service.search(db_session, person.first_name)
        person_result = next((r for r in result if r["id"] == person.id), None)
        assert person_result is not None
        assert person.email in person_result["label"]

    def test_results_are_sorted_alphabetically(self, db_session):
        """Test results are sorted alphabetically by label."""
        org1 = Organization(name="Zebra Corp")
        org2 = Organization(name="Alpha Inc")
        db_session.add_all([org1, org2])
        db_session.commit()

        result = customer_search_service.search(db_session, "Corp")
        if len(result) >= 2:
            labels = [r["label"] for r in result]
            assert labels == sorted(labels, key=lambda x: x.lower())


class TestSearchResponse:
    """Tests for search_response function."""

    def test_returns_list_response_format(self, db_session, person):
        """Test returns proper list_response format."""
        result = customer_search_service.search_response(db_session, person.first_name)
        assert "items" in result
        assert "limit" in result
        assert "offset" in result

    def test_returns_empty_items_for_empty_query(self, db_session):
        """Test returns empty items for empty query."""
        result = customer_search_service.search_response(db_session, "")
        assert result["items"] == []
