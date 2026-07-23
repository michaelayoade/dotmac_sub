"""The NOC queue merges the open outage / mismatch / alarm owners into one list."""

from fastapi.templating import Jinja2Templates

from app.services.web_network_noc import noc_queue_data
from app.web.brand_globals import install_brand_jinja_global


def test_noc_queue_shape_and_empty(db_session):
    data = noc_queue_data(db_session)
    assert set(data["counts"]) == {
        "total",
        "outages",
        "mismatches",
        "alarms",
        "collectors",
    }
    assert data["counts"]["total"] == len(data["items"])
    # empty test DB → nothing in queue, but all three owner reads must run cleanly
    assert data["items"] == []
    assert data["counts"]["total"] == 0


def test_noc_operational_evidence_template_compiles() -> None:
    install_brand_jinja_global()
    env = Jinja2Templates(directory="templates").env

    env.get_template("admin/network/noc/index.html")
