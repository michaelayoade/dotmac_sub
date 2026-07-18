"""Preview-only UFiber ONU/subscription MAC-match audit.

UISP and RADIUS observations can identify candidates for field verification,
but they are not customer-assignment decisions.  This compatibility task keeps
the bounded matching report and operational signals while never creating or
rewriting ``OntAssignment`` rows.  Normal assignments require an operator- or
field-selected exact subscription and modeled PON through
``network.ont_assignment_commands``; disagreements use reviewed identity repair.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import exists
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import OntAssignment, OntUnit
from app.models.subscriber import Subscriber

logger = logging.getLogger(__name__)

# Distinct from uisp_sync (0x755953 "uiS") and zabbix_reconcile (0x707007
# "topo"). "uFL" -> UFiber ONU Link.
ADVISORY_LOCK_KEY = 0x75_46_4C

# Evidence label emitted for the primary direct, unambiguous MAC candidate.
PROVENANCE = "candidate via UISP ONU MAC (router-mode)"

# Name-tiebreak thresholds — mirror uisp_sync's IP+name arm
# (``_IP_NAME_SIM_THRESHOLD = 0.60``). To resolve an ambiguous MAC the winning
# subscriber must clear the threshold AND beat the runner-up by a clear margin,
# with the runner-up itself below the threshold.
_NAME_SIM_THRESHOLD = 0.60
_NAME_TIEBREAK_MARGIN = 0.20

# Replicated from uisp_sync (that module is owned by another branch; we must NOT
# edit it, so we mirror the exact rules byte-for-byte to keep matches
# consistent). ``_MAC_JUNK``: strip every non-hex char, lowercase, require a
# full 12 nibbles. ``_NAME_JUNK``: collapse every non-alphanumeric run to a
# single space.
_MAC_JUNK = re.compile(r"[^0-9a-f]")
_NAME_JUNK = re.compile(r"[^a-z0-9]+")


def _norm_mac(mac: str | None) -> str | None:
    """Normalize a MAC to bare lowercase hex; None when unusable."""
    if not mac:
        return None
    normalized = _MAC_JUNK.sub("", str(mac).lower())
    return normalized if len(normalized) == 12 else None


def _norm_name(value: str | None) -> str:
    """Lowercase, collapse every non-alphanumeric run to a single space, trim."""
    if not value:
        return ""
    return _NAME_JUNK.sub(" ", str(value).lower()).strip()


def _name_similarity(name: str | None, candidates: list[str]) -> float:
    """Best similarity of ``name`` to any candidate label (0.0-1.0).

    Replicates ``uisp_sync._name_similarity``: both signals are computed on the
    normalized (lowercase, alphanumeric-only) forms and the larger wins, so
    either a token overlap (word-order-robust) or a close character sequence
    (typos, glued words) can corroborate. Empty on either side scores 0.0.
    """
    left = _norm_name(name)
    if not left:
        return 0.0
    left_tokens = set(left.split())
    left_compact = left.replace(" ", "")
    best = 0.0
    for candidate in candidates:
        right = _norm_name(candidate)
        if not right:
            continue
        right_tokens = set(right.split())
        if left_tokens and right_tokens:
            jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        else:
            jaccard = 0.0
        ratio = SequenceMatcher(None, left_compact, right.replace(" ", "")).ratio()
        best = max(best, jaccard, ratio)
    return best


@dataclass
class _SubMatch:
    """One exact active subscription a MAC resolves to."""

    subscriber_id: uuid.UUID
    subscription_id: uuid.UUID
    service_address_id: uuid.UUID | None = None
    labels: list[str] = field(default_factory=list)
    login: str | None = None


def _active_subscriber_mac_index(
    session: Session,
) -> dict[str, dict[uuid.UUID, _SubMatch]]:
    """Normalized MAC -> {subscription_id: match} over active services.

    Mirrors ``uisp_sync._active_subscriber_macs`` but also carries each
    subscriber's labels and service identity. Multiple services for one customer
    remain distinct and therefore ambiguous unless another signal resolves them.
    ``subscriptions.mac_address`` is only READ here, never written.
    """
    index: dict[str, dict[uuid.UUID, _SubMatch]] = {}
    rows = (
        session.query(
            Subscription.mac_address,
            Subscription.subscriber_id,
            Subscription.id,
            Subscription.service_address_id,
            Subscription.login,
            Subscriber.display_name,
            Subscriber.first_name,
            Subscriber.last_name,
            Subscriber.company_name,
        )
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .filter(
            Subscription.status == SubscriptionStatus.active,
            Subscription.mac_address.isnot(None),
        )
        .all()
    )
    for (
        mac,
        sid,
        subscription_id,
        address_id,
        login,
        display,
        first,
        last,
        company,
    ) in rows:
        normalized = _norm_mac(mac)
        if not normalized:
            continue
        by_sub = index.setdefault(normalized, {})
        match = by_sub.get(subscription_id)
        if match is None:
            labels = [
                label
                for label in (display, f"{first or ''} {last or ''}", company)
                if _norm_name(label)
            ]
            by_sub[subscription_id] = _SubMatch(
                subscriber_id=sid,
                subscription_id=subscription_id,
                service_address_id=address_id,
                labels=labels,
                login=login,
            )
        elif match.login is None and login:
            match.login = login
    return index


def _name_tiebreak(
    onu_name: str | None, candidates: list[_SubMatch]
) -> tuple[_SubMatch | None, float, float]:
    """Resolve an ambiguous MAC by ONU-name vs subscriber-name similarity.

    Returns ``(winner, best_score, runner_up_score)``. A winner is only returned
    when exactly one candidate clears ``_NAME_SIM_THRESHOLD`` AND beats the
    runner-up by ``>= _NAME_TIEBREAK_MARGIN`` AND the runner-up stays below the
    threshold — so the loser really is a stale-duplicate MAC on another account.
    Otherwise ``winner`` is None and the caller leaves the item ambiguous.
    """
    scored = sorted(
        ((c, _name_similarity(onu_name, c.labels)) for c in candidates),
        key=lambda pair: pair[1],
        reverse=True,
    )
    best_match, best_score = scored[0]
    runner_up_score = scored[1][1]
    if (
        best_score >= _NAME_SIM_THRESHOLD
        and runner_up_score < _NAME_SIM_THRESHOLD
        and best_score - runner_up_score >= _NAME_TIEBREAK_MARGIN
    ):
        return best_match, best_score, runner_up_score
    return None, best_score, runner_up_score


def _candidate_onus(session: Session) -> list[OntUnit]:
    """UISP-managed ONUs with a MAC and NO active assignment.

    ``uisp_device_id IS NOT NULL`` scopes strictly to UISP-imported fiber
    (UFiber), never Huawei ONTs (which keep ``uisp_device_id`` NULL). The
    correlated ``NOT EXISTS`` (rather than a giant ``IN(...)`` of assigned unit
    ids) both keeps the plan index-friendly and enforces fill-null-only: an ONU
    that already has an active assignment is never a candidate, so an existing
    link is never overwritten and the pass is idempotent.
    """
    active_assignment = (
        exists()
        .where(OntAssignment.ont_unit_id == OntUnit.id)
        .where(OntAssignment.active.is_(True))
    )
    return (
        session.query(OntUnit)
        .filter(
            OntUnit.uisp_device_id.isnot(None),
            OntUnit.mac_address.isnot(None),
            ~active_assignment,
        )
        .all()
    )


def _blank_result() -> dict:
    return {
        "candidates": 0,
        "matched_candidate": 0,
        "matched_by_name_candidate": 0,
        # Retained as zero-valued compatibility counters for dashboards that
        # previously displayed mutations performed by this task.
        "matched_linked": 0,
        "matched_by_name_tiebreak": 0,
        "ambiguous": 0,
        "no_match": 0,
        "duplicate_active_mac": 0,
        "already_linked_skipped": 0,
        "failed": 0,
    }


def _resolve_match(
    onu: OntUnit, matches: list[_SubMatch], result: dict
) -> tuple[_SubMatch | None, str, bool]:
    """Pick the strongest review candidate for one MAC-matched ONU.

    ``matches`` is the list of DISTINCT active subscribers the ONU's MAC
    resolves to. Returns ``(winner, provenance_note, via_tiebreak)`` where
    ``winner`` is None when the item stays ambiguous. Exactly one subscriber is
    the unambiguous, direct case; more than one is resolved (when possible) by
    the ONU-name-vs-subscriber-name tiebreak.
    """
    if len(matches) == 1:
        return matches[0], PROVENANCE, False

    winner, best_score, runner_up_score = _name_tiebreak(onu.name, matches)
    if winner is None:
        logger.info(
            "ufiber_onu_link_ambiguous ont_unit_id=%s mac=%s subscribers=%d "
            "best_sim=%.2f runner_up_sim=%.2f",
            onu.id,
            _norm_mac(onu.mac_address),
            len(matches),
            best_score,
            runner_up_score,
        )
        result["ambiguous"] += 1
        return None, PROVENANCE, False
    note = f"candidate via ONU MAC + name tiebreak sim={best_score:.2f}"
    return winner, note, True


def link_ufiber_onus_to_subscribers(db: Session) -> dict:
    """Report router-mode UFiber ONU/subscription candidates without mutation.

    A MAC resolving to >1 distinct active subscriber is ambiguous; the
    ONU-name-vs-subscriber-name tiebreak (mirroring ``uisp_sync``'s IP+name arm)
    can still resolve it when one candidate clearly wins. When a match's MAC also
    lives on a DIFFERENT active subscription (a stale duplicate), a
    ``duplicate_active_mac`` ops signal is emitted with the losing login — the
    subscription itself is never modified.

    Candidate identity is logged for review. No assignment or subscription is
    created, updated, activated, deactivated, or staged for provisioning.
    """
    result = _blank_result()

    candidates = _candidate_onus(db)
    result["candidates"] = len(candidates)
    if not candidates:
        return result

    mac_index = _active_subscriber_mac_index(db)
    for onu in candidates:
        mac = _norm_mac(onu.mac_address)
        if not mac:
            # mac_address present but not a usable 12-nibble MAC.
            result["no_match"] += 1
            continue

        matches = list(mac_index.get(mac, {}).values())
        if not matches:
            result["no_match"] += 1
            continue

        winner, note, via_tiebreak = _resolve_match(onu, matches, result)
        if winner is None:
            continue

        if via_tiebreak:
            result["matched_by_name_candidate"] += 1
        else:
            result["matched_candidate"] += 1
        logger.info(
            "ufiber_onu_assignment_candidate ont_unit_id=%s mac=%s "
            "subscription_id=%s subscriber_id=%s pon_port_id=%s evidence=%s",
            onu.id,
            mac,
            winner.subscription_id,
            winner.subscriber_id,
            onu.pon_port_id,
            note,
            extra={
                "event": "ufiber_onu_assignment_candidate",
                "ont_unit_id": str(onu.id),
                "subscription_id": str(winner.subscription_id),
                "subscriber_id": str(winner.subscriber_id),
                "pon_port_id": str(onu.pon_port_id) if onu.pon_port_id else None,
                "evidence": note,
            },
        )
        # Ops signal: every OTHER distinct active subscriber carrying this
        # ONU's MAC is a stale duplicate. Surfaced for cleanup; never mutated.
        for loser in matches:
            if loser.subscriber_id != winner.subscriber_id:
                logger.warning(
                    "ufiber_onu_link_duplicate_active_mac ont_unit_id=%s "
                    "mac=%s candidate_subscriber=%s losing_subscriber=%s "
                    "losing_login=%s",
                    onu.id,
                    mac,
                    winner.subscriber_id,
                    loser.subscriber_id,
                    loser.login,
                    extra={
                        "event": "ufiber_onu_link_duplicate_active_mac",
                        "ont_unit_id": str(onu.id),
                        "mac": mac,
                        "candidate_subscriber_id": str(winner.subscriber_id),
                        "losing_subscriber_id": str(loser.subscriber_id),
                        "losing_login": loser.login,
                    },
                )
                result["duplicate_active_mac"] += 1

    logger.info("ufiber_onu_link_done %s", result)
    return result
