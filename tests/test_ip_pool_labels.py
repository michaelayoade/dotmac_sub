"""Phase A IP pool labels: purpose, site and NAS bindings (labels only)."""

from __future__ import annotations

import importlib.util
import uuid
from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import FormData

from app.models.catalog import NasDevice, NasPoolBinding
from app.models.network import IpPool
from app.models.network_monitoring import PopSite
from app.services import ip_pool_labels, web_network_ip
from app.services.web_network_ip_actions import update_ip_pool_from_form

ROOT = Path(__file__).resolve().parents[1]


def _pool(db, name, cidr, **kw) -> IpPool:
    pool = IpPool(name=name, cidr=cidr, **kw)
    db.add(pool)
    db.commit()
    return pool


def _nas(db, name, ip, **kw) -> NasDevice:
    nas = NasDevice(name=name, nas_ip=ip, **kw)
    db.add(nas)
    db.commit()
    return nas


def _site(db, name="Garki") -> PopSite:
    site = PopSite(name=name)
    db.add(site)
    db.commit()
    return site


def _form(pool: IpPool, **extra) -> FormData:
    items = [
        ("name", pool.name),
        ("ip_version", "ipv4"),
        ("cidr", pool.cidr),
        ("is_active", "true"),
        ("labels_form", "1"),
    ]
    for key, value in extra.items():
        if isinstance(value, list):
            items.extend((key, v) for v in value)
        else:
            items.append((key, value))
    return FormData(items)


def _request():
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


# --- migration -------------------------------------------------------------


def _load_migration():
    path = ROOT / "alembic/versions/703_ip_pool_labels.py"
    spec = importlib.util.spec_from_file_location("migration_703_pool_labels", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(fn) -> str:
    out = StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": out}
    )
    with Operations.context(ctx):
        fn()
    return out.getvalue()


def test_migration_upgrade_is_additive_and_nullable_only() -> None:
    migration = _load_migration()
    assert migration.down_revision == "702_projection_secret_requests"
    sql = _sql(migration.upgrade)
    assert "ALTER TABLE ip_pools ADD COLUMN purpose VARCHAR(20)" in sql
    assert "ALTER TABLE ip_pools ADD COLUMN site_ref UUID" in sql
    assert "NOT NULL" not in sql.split("CREATE TABLE")[0]
    assert "CONSTRAINT ck_ip_pools_purpose CHECK" in sql
    assert "'customer_nat'" in sql
    assert "CREATE TABLE nas_pool_bindings" in sql
    assert "PRIMARY KEY (nas_device_id, pool_id)" in sql
    assert "REFERENCES nas_devices (id)" in sql
    assert "REFERENCES ip_pools (id)" in sql
    # No data writes and no backfill.
    statements = [x.strip() for x in sql.split(";") if x.strip()]
    for stmt in statements:
        assert not stmt.startswith(("UPDATE", "INSERT", "DELETE"))


def test_migration_downgrade_drops_what_upgrade_adds() -> None:
    sql = _sql(_load_migration().downgrade)
    assert "DROP TABLE nas_pool_bindings" in sql
    assert "DROP CONSTRAINT ck_ip_pools_purpose" in sql
    assert "DROP COLUMN site_ref" in sql
    assert "DROP COLUMN purpose" in sql


def test_migration_is_single_head_child_of_702() -> None:
    revs = [
        p
        for p in (ROOT / "alembic/versions").glob("*.py")
        if '"703_ip_pool_labels"' in p.read_text(encoding="utf-8")
        or "702_projection_secret_requests" in p.read_text(encoding="utf-8")
    ]
    children = [
        p.name
        for p in revs
        if 'down_revision: str | None = "702_projection_secret_requests"'
        in p.read_text(encoding="utf-8")
    ]
    assert children == ["703_ip_pool_labels.py"]


# --- model -----------------------------------------------------------------


def test_model_check_constraint_declared_and_enforced(db_session) -> None:
    from sqlalchemy import insert

    assert "ck_ip_pools_purpose" in {c.name for c in IpPool.__table__.constraints}
    for n, purpose in enumerate(
        (None, "customer", "customer_nat", "infrastructure", "management")
    ):
        _pool(db_session, f"ok-{n}", f"192.0.{n}.0/24", purpose=purpose)
    # Last statement of the test: the failed insert aborts the transaction,
    # which the fixture rolls back at teardown.
    with pytest.raises(IntegrityError):
        db_session.connection().execute(
            insert(IpPool.__table__).values(
                id=uuid.uuid4(), name="bad", cidr="198.51.100.0/24", purpose="bogus"
            )
        )


def test_bindings_are_many_to_many_with_composite_pk(db_session) -> None:
    pool_a = _pool(db_session, "a", "192.0.2.0/24")
    pool_b = _pool(db_session, "b", "198.51.100.0/24")
    nas1 = _nas(db_session, "n1", "203.0.113.1")
    nas2 = _nas(db_session, "n2", "203.0.113.2")
    for pool in (pool_a, pool_b):
        for nas in (nas1, nas2):
            db_session.add(NasPoolBinding(pool_id=pool.id, nas_device_id=nas.id))
    db_session.commit()
    assert ip_pool_labels.nas_binding_counts(db_session) == {
        pool_a.id: 2,
        pool_b.id: 2,
    }
    pk_cols = [c.name for c in NasPoolBinding.__table__.primary_key.columns]
    assert pk_cols == ["nas_device_id", "pool_id"]
    cols = {c.name for c in NasPoolBinding.__table__.columns}
    assert cols == {"nas_device_id", "pool_id", "created_at"}
    assert NasPoolBinding.__module__ == "app.models.catalog"


# --- form save -------------------------------------------------------------


def test_form_saves_all_three_labels_with_multi_nas(db_session) -> None:
    pool = _pool(db_session, "p", "192.0.2.0/24")
    site = _site(db_session)
    nas1 = _nas(db_session, "n1", "203.0.113.1")
    nas2 = _nas(db_session, "n2", "203.0.113.2")
    result = update_ip_pool_from_form(
        _request(),
        db_session,
        pool_id=str(pool.id),
        form=_form(
            pool,
            purpose="customer_nat",
            site_ref=str(site.id),
            nas_binding_ids=[str(nas1.id), str(nas2.id)],
        ),
    )
    assert result.success, result.error
    db_session.expire_all()
    saved = db_session.get(IpPool, pool.id)
    assert saved.purpose == "customer_nat"
    assert saved.site_ref == site.id
    assert set(ip_pool_labels.saved_binding_nas_ids(db_session, pool.id)) == {
        str(nas1.id),
        str(nas2.id),
    }

    # Edit later: shrink bindings and clear labels.
    result = update_ip_pool_from_form(
        _request(),
        db_session,
        pool_id=str(pool.id),
        form=_form(pool, nas_binding_ids=[str(nas2.id)]),
    )
    assert result.success, result.error
    db_session.expire_all()
    saved = db_session.get(IpPool, pool.id)
    assert saved.purpose is None and saved.site_ref is None
    assert ip_pool_labels.saved_binding_nas_ids(db_session, pool.id) == [str(nas2.id)]


def test_submit_without_label_fields_keeps_saved_bindings(db_session) -> None:
    pool = _pool(db_session, "p", "192.0.2.0/24")
    nas = _nas(db_session, "n1", "203.0.113.1")
    db_session.add(NasPoolBinding(pool_id=pool.id, nas_device_id=nas.id))
    db_session.commit()
    form = FormData(
        [
            ("name", "p"),
            ("ip_version", "ipv4"),
            ("cidr", "192.0.2.0/24"),
            ("is_active", "true"),
        ]
    )
    values = web_network_ip.parse_ip_pool_form(form)
    assert values["nas_binding_ids"] is None
    _, _, error = web_network_ip.update_ip_pool(
        db_session, pool_id=str(pool.id), values=values
    )
    assert error is None
    assert ip_pool_labels.saved_binding_nas_ids(db_session, pool.id) == [str(nas.id)]


def test_invalid_purpose_and_unknown_nas_are_form_errors(db_session) -> None:
    pool = _pool(db_session, "p", "192.0.2.0/24")
    result = update_ip_pool_from_form(
        _request(), db_session, pool_id=str(pool.id), form=_form(pool, purpose="x")
    )
    assert not result.success and result.error
    result = update_ip_pool_from_form(
        _request(),
        db_session,
        pool_id=str(pool.id),
        form=_form(pool, nas_binding_ids=[str(uuid.uuid4())]),
    )
    assert not result.success and "NAS" in (result.error or "")
    assert ip_pool_labels.saved_binding_nas_ids(db_session, pool.id) == []


# --- unlabelled logic ------------------------------------------------------


def test_unlabelled_definition(db_session) -> None:
    site = _site(db_session)
    nas = _nas(db_session, "n1", "203.0.113.1")
    no_purpose = _pool(db_session, "no-purpose", "192.0.2.0/24", site_ref=site.id)
    no_site = _pool(db_session, "no-site", "192.0.3.0/24", purpose="infrastructure")
    active_cust_no_nas = _pool(
        db_session, "cust-no-nas", "192.0.4.0/24", purpose="customer", site_ref=site.id
    )
    inactive_cust_no_nas = _pool(
        db_session,
        "inactive-cust",
        "192.0.5.0/24",
        purpose="customer_nat",
        site_ref=site.id,
        is_active=False,
    )
    infra_no_nas = _pool(
        db_session, "infra", "192.0.6.0/24", purpose="management", site_ref=site.id
    )
    complete = _pool(
        db_session, "complete", "192.0.7.0/24", purpose="customer", site_ref=site.id
    )
    db_session.add(NasPoolBinding(pool_id=complete.id, nas_device_id=nas.id))
    db_session.commit()
    pools = [
        no_purpose,
        no_site,
        active_cust_no_nas,
        inactive_cust_no_nas,
        infra_no_nas,
        complete,
    ]
    rows = ip_pool_labels.build_pool_label_rows(db_session, pools)
    flags = {p.name: rows[str(p.id)].unlabelled for p in pools}
    assert flags == {
        "no-purpose": True,
        "no-site": True,
        "cust-no-nas": True,
        "inactive-cust": False,
        "infra": False,
        "complete": False,
    }
    assert rows[str(complete.id)].nas_count == 1
    assert rows[str(complete.id)].site_name == "Garki"


def test_list_filter_and_count_cover_all_pools(db_session) -> None:
    site = _site(db_session)
    _pool(db_session, "u1", "192.0.2.0/24", is_active=False)  # inactive, unlabelled
    _pool(db_session, "u2", "192.0.3.0/24")
    _pool(db_session, "ok", "192.0.4.0/24", purpose="infrastructure", site_ref=site.id)
    data = web_network_ip.build_ip_pools_data(db_session)
    assert data["unlabelled_count"] == 2
    assert data["total_pool_count"] == 3
    assert len(data["pools"]) == 3

    data = web_network_ip.build_ip_pools_data(db_session, label_filter="unlabelled")
    assert {p.name for p in data["pools"]} == {"u1", "u2"}
    assert data["label_filter"] == "unlabelled"

    # The gate count ignores the type filter.
    data = web_network_ip.build_ip_pools_data(
        db_session, pool_type="fallback", label_filter="unlabelled"
    )
    assert data["unlabelled_count"] == 2
    assert data["pools"] == []


# --- suggestion prefill ----------------------------------------------------


def test_suggestion_is_fk_union_tags_and_writes_nothing(db_session) -> None:
    pool = _pool(db_session, "p", "192.0.2.0/24")
    fk_nas = _nas(db_session, "fk", "203.0.113.1")
    tag_nas = _nas(db_session, "tag", "203.0.113.2", tags=[f"radius_pool:{pool.id}"])
    _nas(db_session, "other", "203.0.113.3", tags=["radius_pool:" + str(uuid.uuid4())])
    _nas(
        db_session,
        "dead",
        "203.0.113.4",
        is_active=False,
        tags=[f"radius_pool:{pool.id}"],
    )
    pool.nas_device_id = fk_nas.id
    db_session.commit()

    ctx = web_network_ip.pool_label_form_context(db_session, pool_id=str(pool.id))
    assert set(ctx["selected_nas_ids"]) == {str(fk_nas.id), str(tag_nas.id)}
    assert ctx["nas_prefilled"] is True
    assert db_session.query(NasPoolBinding).count() == 0

    # Saved bindings replace the suggestion.
    db_session.add(NasPoolBinding(pool_id=pool.id, nas_device_id=tag_nas.id))
    db_session.commit()
    ctx = web_network_ip.pool_label_form_context(db_session, pool_id=str(pool.id))
    assert ctx["selected_nas_ids"] == [str(tag_nas.id)]
    assert ctx["nas_prefilled"] is False

    # A new pool has no suggestion.
    ctx = web_network_ip.pool_label_form_context(db_session, pool_id=None)
    assert ctx["selected_nas_ids"] == [] and ctx["nas_prefilled"] is False


def test_edit_form_renders_label_fields(db_session) -> None:
    from app.web.templates import templates

    pool = _pool(db_session, "p", "192.0.2.0/24", purpose="customer")
    nas = _nas(db_session, "n1", "203.0.113.1", tags=[f"radius_pool:{pool.id}"])
    ctx = {
        "request": _request(),
        "pool": web_network_ip.pool_form_snapshot_from_model(pool),
        "action_url": "/x",
        "olt_devices": [],
        "vlans": [],
        "nas_devices": [],
        **web_network_ip.pool_label_form_context(db_session, pool_id=str(pool.id)),
    }
    html = templates.get_template("admin/network/ip-management/pool_form.html").render(
        ctx
    )
    assert 'name="purpose"' in html and 'name="site_ref"' in html
    assert 'name="nas_binding_ids"' in html and 'name="labels_form"' in html
    assert f'value="{nas.id}" selected' in html
    assert "Nothing is saved until you submit" in html


def test_inactive_pool_can_be_labelled(db_session) -> None:
    pool = _pool(db_session, "old", "192.0.2.0/24", is_active=False)
    assert web_network_ip.get_ip_pool_for_edit(db_session, pool_id=str(pool.id))
    form = _form(pool, purpose="infrastructure")
    form = FormData([(k, v) for k, v in form.multi_items() if k != "is_active"])
    result = update_ip_pool_from_form(
        _request(), db_session, pool_id=str(pool.id), form=form
    )
    assert result.success, result.error
    db_session.expire_all()
    saved = db_session.get(IpPool, pool.id)
    assert saved.purpose == "infrastructure" and saved.is_active is False


def test_inactive_pool_update_writes_only_labels_and_skips_checks(db_session) -> None:
    site = _site(db_session)
    nas = _nas(db_session, "n1", "203.0.113.1")
    # An active pool now owns the same CIDR, so overlap checks would refuse.
    _pool(db_session, "live", "192.0.2.0/24")
    old = _pool(db_session, "old", "192.0.2.0/24", is_active=False)
    form = FormData(
        [
            ("name", "renamed"),
            ("ip_version", "ipv4"),
            ("cidr", "10.9.9.0/24"),
            ("is_active", "true"),
            ("labels_form", "1"),
            ("purpose", "management"),
            ("site_ref", str(site.id)),
            ("nas_binding_ids", str(nas.id)),
        ]
    )
    result = update_ip_pool_from_form(
        _request(), db_session, pool_id=str(old.id), form=form
    )
    assert result.success, result.error
    db_session.expire_all()
    saved = db_session.get(IpPool, old.id)
    assert saved.is_active is False
    assert saved.name == "old" and saved.cidr == "192.0.2.0/24"
    assert saved.purpose == "management" and saved.site_ref == site.id
    assert ip_pool_labels.saved_binding_nas_ids(db_session, old.id) == [str(nas.id)]


def test_active_pool_overlap_still_refused(db_session) -> None:
    _pool(db_session, "live", "192.0.2.0/24")
    other = _pool(db_session, "other", "198.51.100.0/24")
    result = update_ip_pool_from_form(
        _request(),
        db_session,
        pool_id=str(other.id),
        form=_form(other, cidr="192.0.2.0/24"),
    )
    assert not result.success


def test_active_update_is_single_transaction_and_audits_bindings(db_session) -> None:
    pool = _pool(db_session, "p", "192.0.2.0/24")
    nas = _nas(db_session, "n1", "203.0.113.1")
    values = web_network_ip.parse_ip_pool_form(
        _form(pool, nas_binding_ids=[str(nas.id)])
    )
    commits = []
    original = db_session.commit
    db_session.commit = lambda: (commits.append(1), original())[1]  # type: ignore[method-assign]
    try:
        _, changes, error = web_network_ip.update_ip_pool(
            db_session, pool_id=str(pool.id), values=values
        )
    finally:
        db_session.commit = original  # type: ignore[method-assign]
    assert error is None
    assert len(commits) == 1
    assert changes["nas_binding_ids"] == {"from": [], "to": [str(nas.id)]}


def test_bad_binding_rolls_back_pool_fields(db_session) -> None:
    pool = _pool(db_session, "p", "192.0.2.0/24")
    values = web_network_ip.parse_ip_pool_form(
        _form(pool, purpose="customer", nas_binding_ids=[str(uuid.uuid4())])
    )
    _, _, error = web_network_ip.update_ip_pool(
        db_session, pool_id=str(pool.id), values=values
    )
    assert error
    db_session.expire_all()
    assert db_session.get(IpPool, pool.id).purpose is None


def test_crafted_non_uuid_nas_ids_do_not_break_error_rerender(db_session) -> None:
    pool = _pool(db_session, "p", "192.0.2.0/24")
    result = update_ip_pool_from_form(
        _request(),
        db_session,
        pool_id=str(pool.id),
        form=_form(pool, purpose="bogus", nas_binding_ids=["not-a-uuid"]),
    )
    assert not result.success
    assert result.form_context["selected_nas_ids"] == []


def test_submit_without_labels_form_cannot_clear_labels(db_session) -> None:
    site = _site(db_session)
    pool = _pool(db_session, "p", "192.0.2.0/24", purpose="customer", site_ref=site.id)
    form = FormData(
        [
            ("name", "p"),
            ("ip_version", "ipv4"),
            ("cidr", "192.0.2.0/24"),
            ("is_active", "true"),
        ]
    )
    result = update_ip_pool_from_form(
        _request(), db_session, pool_id=str(pool.id), form=form
    )
    assert result.success, result.error
    db_session.expire_all()
    saved = db_session.get(IpPool, pool.id)
    assert saved.purpose == "customer" and saved.site_ref == site.id


def test_pool_list_label_cells_follow_gateway_header(db_session) -> None:
    html = (ROOT / "templates/admin/network/ip-management/pools.html").read_text()
    headers = html[html.index("<thead>") : html.index("</thead>")]
    assert (
        headers.index(">Gateway<")
        < headers.index(">Purpose<")
        < headers.index(">Used<")
    )
    row = html[html.index("{% for pool in pools %}") :]
    assert (
        row.index("pool.gateway")
        < row.index("label.purpose_label")
        < row.index("util = pool_utilization")
    )
