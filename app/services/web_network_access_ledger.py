"""Access / RADIUS / FUP ledger page data — a projection over the access owners.

Consolidates the operational "who is online / who is throttled" views into one
archetype-D ledger with a facet per list owner:
  - sessions: the live active-session inventory (owner: network.radius_sessions)
  - fup: subscriptions currently throttled/blocked by FUP (owner: fup_state)

Access-state is derive-only (`radius_access_state` has no list read) and
reject/auth are config/log surfaces, so neither is a ledger facet here. Status
tone comes from the server-owned presentations. Read-only projection.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.fup_state import fup_state
from app.services.network import radius_sessions
from app.services.status_presentation import fup_action_status_presentation

# facet order + labels
FACETS: tuple[tuple[str, str], ...] = (
    ("sessions", "Online sessions"),
    ("fup", "FUP throttled"),
)
_VALID = {key for key, _ in FACETS}


def _fmt_bytes(value: object) -> str:
    n = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_duration(seconds: object) -> str:
    total = int(seconds or 0)
    hours, rem = divmod(total, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _session_rows(db: Session) -> tuple[list, list]:
    # RADIUS accounting is from the NAS's perspective: bytes_in is what the NAS
    # received from the subscriber (their upload), bytes_out is what it sent
    # (their download).
    columns = [
        ("User", "username"),
        ("Framed IP", "framed_ip"),
        ("NAS IP", "nas_ip"),
        ("Duration", "duration"),
        ("Down", "down"),
        ("Up", "up"),
    ]
    rows = [
        {
            "id": str(s.id),
            "username": s.username,
            "framed_ip": s.framed_ip_address or s.framed_ipv6_prefix or "—",
            "nas_ip": s.nas_ip_address or "—",
            "duration": _fmt_duration(s.session_time),
            "down": _fmt_bytes(s.bytes_out),
            "up": _fmt_bytes(s.bytes_in),
        }
        for s in radius_sessions.list_all_active_sessions(db)
    ]
    return columns, rows


def _fup_rows(db: Session) -> tuple[list, list]:
    columns = [
        ("Subscription", "subscription"),
        ("Status", "__status"),
        ("Speed cut", "speed_cut"),
        ("Cap resets", "cap_resets"),
    ]
    rows = []
    for st in fup_state.list_throttled(db):
        pct = st.speed_reduction_percent
        rows.append(
            {
                "id": str(st.id),
                "subscription": str(st.subscription_id),
                "status": fup_action_status_presentation(st.action_status),
                "speed_cut": f"{pct:.0f}%" if pct is not None else "—",
                "cap_resets": st.cap_resets_at.strftime("%b %d, %H:%M")
                if st.cap_resets_at
                else "—",
            }
        )
    return columns, rows


_DISPATCH = {
    "sessions": _session_rows,
    "fup": _fup_rows,
}


def access_ledger_data(db: Session, facet: str = "sessions") -> dict:
    """Return the ledger page data for one access facet (from its owner)."""
    facet = facet if facet in _VALID else "sessions"
    columns, rows = _DISPATCH[facet](db)
    return {
        "facet": facet,
        "facet_label": dict(FACETS)[facet],
        "facets": [{"key": k, "label": lbl} for k, lbl in FACETS],
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        # live "who is online now" counts from the canonical read owner
        "summary": radius_sessions.online_summary(db),
        "detail_base": "",
    }
