import argparse

from dotenv import load_dotenv

from app.db import SessionLocal
from app.models.person import Person
from app.models.rbac import Permission, PersonRole, Role, RolePermission


DEFAULT_PERMISSIONS = [
    # Audit
    ("audit:read", "Read audit events"),

    # Auth & System
    ("auth:manage", "Manage authentication settings"),
    ("system:settings:read", "View system settings"),
    ("system:settings:write", "Modify system settings"),

    # RBAC - Granular permissions for role builder
    ("rbac:roles:read", "View roles"),
    ("rbac:roles:write", "Create and update roles"),
    ("rbac:roles:delete", "Delete roles"),
    ("rbac:permissions:read", "View permissions"),
    ("rbac:permissions:write", "Create and update permissions"),
    ("rbac:permissions:delete", "Delete permissions"),
    ("rbac:assign", "Assign roles to users"),

    # Customers/Subscribers
    ("customer:read", "View customers and subscribers"),
    ("customer:create", "Create customers and subscribers"),
    ("customer:update", "Update customers and subscribers"),
    ("customer:delete", "Delete customers and subscribers"),
    ("customer:impersonate", "Impersonate customer accounts"),

    # Billing - Invoices
    ("billing:invoice:read", "View invoices"),
    ("billing:invoice:create", "Create invoices"),
    ("billing:invoice:update", "Update invoices"),
    ("billing:invoice:delete", "Delete/void invoices"),

    # Billing - Payments
    ("billing:payment:read", "View payments"),
    ("billing:payment:create", "Record payments"),
    ("billing:payment:update", "Update payments"),
    ("billing:payment:delete", "Delete/refund payments"),

    # Billing - Credit Notes
    ("billing:credit_note:read", "View credit notes"),
    ("billing:credit_note:create", "Create credit notes"),
    ("billing:credit_note:update", "Update credit notes"),
    ("billing:credit_note:delete", "Delete credit notes"),

    # Billing - Accounts & Ledger
    ("billing:account:read", "View billing accounts"),
    ("billing:account:write", "Manage billing accounts"),
    ("billing:ledger:read", "View ledger entries"),
    ("billing:tax:read", "View tax rates"),
    ("billing:tax:write", "Manage tax rates"),

    # Catalog
    ("catalog:product:read", "View catalog products"),
    ("catalog:product:write", "Manage catalog products"),
    ("catalog:offer:read", "View catalog offers"),
    ("catalog:offer:write", "Manage catalog offers"),

    # Subscriptions
    ("subscription:read", "View subscriptions"),
    ("subscription:create", "Create subscriptions"),
    ("subscription:update", "Update subscriptions"),
    ("subscription:cancel", "Cancel subscriptions"),

    # Network - Devices
    ("network:device:read", "View network devices"),
    ("network:device:write", "Manage network devices"),
    ("network:ont:read", "View ONT units"),
    ("network:ont:write", "Manage ONT units"),

    # Network - IP Management
    ("network:ip:read", "View IP pools and assignments"),
    ("network:ip:write", "Manage IP pools and assignments"),

    # Network - Fiber
    ("network:fiber:read", "View fiber infrastructure"),
    ("network:fiber:write", "Manage fiber infrastructure"),

    # Network - RADIUS
    ("network:radius:read", "View RADIUS configuration"),
    ("network:radius:write", "Manage RADIUS configuration"),

    # Operations - Work Orders
    ("operations:work_order:read", "View work orders"),
    ("operations:work_order:create", "Create work orders"),
    ("operations:work_order:update", "Update work orders"),
    ("operations:work_order:delete", "Delete work orders"),
    ("operations:work_order:dispatch", "Dispatch work orders"),

    # Operations - Service Orders
    ("operations:service_order:read", "View service orders"),
    ("operations:service_order:create", "Create service orders"),
    ("operations:service_order:update", "Update service orders"),

    # Operations - Technicians
    ("operations:technician:read", "View technicians"),
    ("operations:technician:write", "Manage technicians"),

    # Support - Tickets
    ("support:ticket:read", "View tickets"),
    ("support:ticket:create", "Create tickets"),
    ("support:ticket:update", "Update tickets"),
    ("support:ticket:delete", "Delete tickets"),
    ("support:ticket:assign", "Assign tickets"),

    # CRM
    ("crm:contact:read", "View CRM contacts"),
    ("crm:contact:write", "Manage CRM contacts"),
    ("crm:conversation:read", "View conversations"),
    ("crm:conversation:write", "Manage conversations"),
    ("crm:lead:read", "View leads"),
    ("crm:lead:write", "Manage leads"),

    # Projects
    ("project:read", "View projects"),
    ("project:create", "Create projects"),
    ("project:update", "Update projects"),
    ("project:delete", "Delete projects"),
    ("project:task:read", "View project tasks"),
    ("project:task:write", "Manage project tasks"),

    # Vendors
    ("vendor:read", "View vendors"),
    ("vendor:write", "Manage vendors"),
    ("vendor:project:read", "View vendor projects"),
    ("vendor:project:write", "Manage vendor projects"),

    # Inventory
    ("inventory:read", "View inventory"),
    ("inventory:write", "Manage inventory"),

    # GIS / Mapping
    ("gis:map:view", "View maps and layers"),
    ("gis:map:edit", "Edit map features (markers, polygons)"),
    ("gis:map:configure", "Configure map settings and layers"),
    ("gis:coverage:read", "View coverage areas"),
    ("gis:coverage:write", "Manage coverage areas"),
    ("gis:fiber:view", "View fiber routes on map"),
    ("gis:fiber:edit", "Edit fiber routes on map"),
    ("gis:serviceability:check", "Run address serviceability checks"),
    ("gis:export", "Export GIS data (KML, GeoJSON)"),

    # Reports
    ("reports:billing", "View billing reports"),
    ("reports:network", "View network reports"),
    ("reports:operations", "View operations reports"),
    ("reports:subscribers", "View subscriber reports"),

    # Legacy broad permissions (for backward compatibility)
    ("billing:read", "Read all billing data"),
    ("billing:write", "Manage all billing data"),
    ("catalog:read", "Read catalog data"),
    ("catalog:write", "Manage catalog data"),
    ("network:read", "Read network inventory and telemetry"),
    ("network:write", "Manage network inventory and telemetry"),
    ("provisioning:read", "Read provisioning data"),
    ("provisioning:write", "Manage provisioning data"),
    ("subscriber:read", "Read subscribers and accounts"),
    ("subscriber:write", "Manage subscribers and accounts"),
    ("subscriber:impersonate", "Impersonate subscriber accounts"),
]

DEFAULT_ROLES = [
    ("admin", "Full system access"),
    ("auditor", "Audit read-only access"),
    ("operator", "Network and provisioning operations"),
    ("support", "Subscriber and billing support"),
]

ROLE_PERMISSIONS = {
    "admin": [perm for perm, _ in DEFAULT_PERMISSIONS],
    "auditor": [
        "audit:read",
        "billing:invoice:read",
        "billing:payment:read",
        "billing:credit_note:read",
        "billing:account:read",
        "billing:ledger:read",
        "customer:read",
        "reports:billing",
        "reports:subscribers",
    ],
    "operator": [
        "network:device:read",
        "network:device:write",
        "network:ont:read",
        "network:ont:write",
        "network:ip:read",
        "network:ip:write",
        "network:fiber:read",
        "network:fiber:write",
        "network:radius:read",
        "network:radius:write",
        "network:read",
        "network:write",
        "provisioning:read",
        "provisioning:write",
        "customer:read",
        "subscription:read",
        "operations:work_order:read",
        "operations:work_order:update",
        "reports:network",
    ],
    "support": [
        "customer:read",
        "customer:update",
        "billing:invoice:read",
        "billing:payment:read",
        "billing:payment:create",
        "billing:credit_note:read",
        "billing:account:read",
        "subscription:read",
        "support:ticket:read",
        "support:ticket:create",
        "support:ticket:update",
        "crm:contact:read",
        "crm:conversation:read",
        "crm:conversation:write",
        "reports:subscribers",
    ],
}


def parse_args():
    parser = argparse.ArgumentParser(description="Seed RBAC roles and permissions.")
    parser.add_argument("--admin-email", help="Email to map to admin role.")
    parser.add_argument("--admin-person-id", help="Person ID to map to admin role.")
    return parser.parse_args()


def _ensure_role(db, name, description):
    role = db.query(Role).filter(Role.name == name).first()
    if not role:
        role = Role(name=name, description=description, is_active=True)
        db.add(role)
    else:
        if not role.is_active:
            role.is_active = True
        if description and not role.description:
            role.description = description
    return role


def _ensure_permission(db, key, description):
    permission = db.query(Permission).filter(Permission.key == key).first()
    if not permission:
        permission = Permission(key=key, description=description, is_active=True)
        db.add(permission)
    else:
        if not permission.is_active:
            permission.is_active = True
        if description and not permission.description:
            permission.description = description
    return permission


def _ensure_role_permission(db, role_id, permission_id):
    link = (
        db.query(RolePermission)
        .filter(RolePermission.role_id == role_id)
        .filter(RolePermission.permission_id == permission_id)
        .first()
    )
    if not link:
        link = RolePermission(role_id=role_id, permission_id=permission_id)
        db.add(link)
    return link


def _ensure_person_role(db, person_id, role_id):
    link = (
        db.query(PersonRole)
        .filter(PersonRole.person_id == person_id)
        .filter(PersonRole.role_id == role_id)
        .first()
    )
    if not link:
        link = PersonRole(person_id=person_id, role_id=role_id)
        db.add(link)
    return link


def main():
    load_dotenv()
    args = parse_args()
    db = SessionLocal()
    try:
        for name, description in DEFAULT_ROLES:
            _ensure_role(db, name, description)
        for key, description in DEFAULT_PERMISSIONS:
            _ensure_permission(db, key, description)
        db.commit()

        roles = {role.name: role for role in db.query(Role).all()}
        permissions = {perm.key: perm for perm in db.query(Permission).all()}
        for role_name, permission_keys in ROLE_PERMISSIONS.items():
            role = roles.get(role_name)
            if not role:
                continue
            for key in permission_keys:
                permission = permissions.get(key)
                if not permission:
                    continue
                _ensure_role_permission(db, role.id, permission.id)
        db.commit()

        admin_role = roles.get("admin")
        if admin_role and (args.admin_email or args.admin_person_id):
            person = None
            if args.admin_person_id:
                person = db.get(Person, args.admin_person_id)
            if not person and args.admin_email:
                person = db.query(Person).filter(Person.email == args.admin_email).first()
            if not person:
                raise SystemExit("Admin person not found.")
            _ensure_person_role(db, person.id, admin_role.id)
            db.commit()
            print("Admin role assigned.")
        print("RBAC seed complete.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
