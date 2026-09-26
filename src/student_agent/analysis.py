"""Pure, deterministic analysis helpers shared by the specialist agents.

Nothing in this module performs I/O. Specialists feed it the ``data`` part of MCP evidence
and receive typed facts back, which keeps the business rules unit-testable offline.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

PRIMARY_ISSUES = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
)
INSUFFICIENT = "insufficient_evidence"

# A payment belongs to the purchase when it is captured within this window after approval.
CAPTURE_ANCHOR = timedelta(hours=24)


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def dedupe(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop byte-identical rows while keeping the first occurrence order."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = json.dumps(row, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


@dataclass(frozen=True)
class OrderWindow:
    """Lifecycle window derived from the authoritative order row and the case timestamp."""

    purchase_at: datetime
    approved_at: datetime | None
    estimated_at: datetime | None
    opened_at: datetime

    def holds_event(self, moment: datetime | None) -> bool:
        return moment is not None and self.purchase_at <= moment <= self.opened_at

    def holds_shipping_limit(self, moment: datetime | None) -> bool:
        ceiling = self.estimated_at or self.opened_at
        return moment is not None and self.purchase_at <= moment <= ceiling

    def anchors_capture(self, moment: datetime | None) -> bool:
        anchor = self.approved_at or self.purchase_at
        return moment is not None and anchor <= moment < anchor + CAPTURE_ANCHOR


@dataclass(frozen=True)
class OrderFacts:
    order_id: str
    status: str
    window: OrderWindow
    carrier_at: datetime | None
    delivered_at: datetime | None
    items: tuple[dict[str, Any], ...]
    excluded_items: int

    @property
    def item_ids(self) -> list[str]:
        return sorted({str(item["order_item_id"]) for item in self.items})

    @property
    def seller_ids(self) -> list[str]:
        return sorted({str(item["seller_id"]) for item in self.items if item.get("seller_id")})

    @property
    def total_brl(self) -> Decimal:
        return sum(
            (money(item.get("price")) + money(item.get("freight_value")) for item in self.items),
            Decimal("0.00"),
        )


def build_order_facts(
    order: dict[str, Any], items: list[dict[str, Any]], opened_at: datetime
) -> OrderFacts:
    purchase_at = parse_ts(order.get("order_purchase_timestamp"))
    if purchase_at is None:
        raise ValueError("order row has no purchase timestamp")
    window = OrderWindow(
        purchase_at=purchase_at,
        approved_at=parse_ts(order.get("order_approved_at")),
        estimated_at=parse_ts(order.get("order_estimated_delivery_date")),
        opened_at=opened_at,
    )
    unique = dedupe(items)
    scoped = [
        row
        for row in unique
        if window.holds_shipping_limit(parse_ts(row.get("shipping_limit_date")))
    ]
    return OrderFacts(
        order_id=str(order["order_id"]),
        status=str(order.get("order_status") or ""),
        window=window,
        carrier_at=parse_ts(order.get("order_delivered_carrier_date")),
        delivered_at=parse_ts(order.get("order_delivered_customer_date")),
        items=tuple(scoped),
        excluded_items=len(items) - len(scoped),
    )


@dataclass(frozen=True)
class PaymentFacts:
    captures: tuple[dict[str, Any], ...]
    anchored_captures: tuple[dict[str, Any], ...]
    open_mismatches: tuple[dict[str, Any], ...]
    refund_events: tuple[dict[str, Any], ...]
    refund_lookup: str  # "found" | "not_found"
    excluded_events: int

    @property
    def anchored_total_brl(self) -> Decimal:
        return sum((money(e.get("amount_brl")) for e in self.anchored_captures), Decimal("0.00"))

    def refunds_with_status(self, status: str) -> list[dict[str, Any]]:
        return [e for e in self.refund_events if e.get("status") == status]


def build_payment_facts(
    window: OrderWindow,
    payment_events: list[dict[str, Any]],
    refund_events: list[dict[str, Any]] | None,
) -> PaymentFacts:
    unique = dedupe(payment_events)
    scoped = [e for e in unique if window.holds_event(parse_ts(e.get("event_at")))]
    captures = [
        e for e in scoped if e.get("event_type") == "captured" and e.get("status") == "confirmed"
    ]
    anchored = [e for e in captures if window.anchors_capture(parse_ts(e.get("event_at")))]
    mismatches = [
        e
        for e in scoped
        if e.get("event_type") == "reconciliation_mismatch" and e.get("status") == "open"
    ]
    refunds_unique = dedupe(refund_events or [])
    refunds = [e for e in refunds_unique if window.holds_event(parse_ts(e.get("event_at")))]
    excluded = (len(payment_events) - len(scoped)) + (len(refund_events or []) - len(refunds))
    return PaymentFacts(
        captures=tuple(captures),
        anchored_captures=tuple(anchored),
        open_mismatches=tuple(mismatches),
        refund_events=tuple(refunds),
        refund_lookup="not_found" if refund_events is None else "found",
        excluded_events=excluded,
    )


@dataclass(frozen=True)
class ShipmentFacts:
    status: str
    carrier_at: datetime | None
    delivered_at: datetime | None
    estimated_at: datetime | None
    shipping_limit_at: datetime | None
    late_event_actors: tuple[str, ...]
    excluded_events: int

    @property
    def delivered_late(self) -> bool:
        return bool(
            self.delivered_at and self.estimated_at and self.delivered_at > self.estimated_at
        )

    @property
    def seller_missed_handoff(self) -> bool | None:
        if self.carrier_at is None or self.shipping_limit_at is None:
            return None
        return self.carrier_at > self.shipping_limit_at


def build_shipment_facts(window: OrderWindow, shipment: dict[str, Any]) -> ShipmentFacts:
    delivered_at = parse_ts(shipment.get("delivered_customer_at"))
    limits = [
        parse_ts(row.get("shipping_limit_at"))
        for row in dedupe(shipment.get("shipping_limits") or [])
    ]
    scoped_limits = [limit for limit in limits if window.holds_shipping_limit(limit)]
    events = dedupe(shipment.get("events") or [])
    # A delivery event describes this order only when it matches the recorded delivery instant.
    scoped_events = [
        e
        for e in events
        if delivered_at is not None
        and parse_ts(e.get("event_at")) == delivered_at
        and e.get("status") == "confirmed"
    ]
    actors = sorted(
        {str(e.get("actor")) for e in scoped_events if e.get("event_type") == "delivered_late"}
    )
    return ShipmentFacts(
        status=str(shipment.get("order_status") or ""),
        carrier_at=parse_ts(shipment.get("delivered_carrier_at")),
        delivered_at=delivered_at,
        estimated_at=parse_ts(shipment.get("estimated_delivery_at")),
        shipping_limit_at=max(scoped_limits) if scoped_limits else None,
        late_event_actors=tuple(actors),
        excluded_events=len(events) - len(scoped_events),
    )


@dataclass
class Triage:
    issue: str
    reasons: list[str] = field(default_factory=list)
    competing: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def _capture_pattern(order: OrderFacts, payment: PaymentFacts) -> str | None:
    captures = payment.anchored_captures
    if len(captures) < 2:
        return None
    amounts = [money(e.get("amount_brl")) for e in captures]
    if order.items and sum(amounts, Decimal("0.00")) == order.total_brl:
        return "valid_split_payment"
    if len(set(amounts)) == 1:
        return "duplicate_charge"
    return None


def triage(order: OrderFacts, payment: PaymentFacts, shipment: ShipmentFacts) -> Triage:
    """Apply the evidence rules in a fixed priority so decoy rows cannot flip the verdict."""
    signals: list[tuple[str, str]] = []
    paid = payment.anchored_total_brl > 0
    if order.status == "canceled" and paid:
        signals.append(("canceled_order_paid", "ORDER_CANCELED_WITH_CAPTURE"))
    if order.status == "unavailable" and paid:
        signals.append(("unavailable_order_paid", "ORDER_UNAVAILABLE_WITH_CAPTURE"))
    if payment.refunds_with_status("failed"):
        signals.append(("refund_failed", "REFUND_EVENT_FAILED"))
    if payment.refunds_with_status("pending"):
        signals.append(("refund_pending", "REFUND_EVENT_PENDING"))
    if payment.open_mismatches:
        signals.append(("payment_mismatch", "RECONCILIATION_MISMATCH_OPEN"))
    pattern = _capture_pattern(order, payment)
    if pattern == "duplicate_charge":
        signals.append((pattern, "EQUAL_CAPTURES_EXCEED_ORDER_TOTAL"))
    elif pattern == "valid_split_payment":
        signals.append((pattern, "CAPTURES_SUM_TO_ORDER_TOTAL"))

    result = Triage(issue="unsupported_claim")
    if order.status == "delivered" and shipment.delivered_late:
        missed = shipment.seller_missed_handoff
        by_timestamps = "late_delivery_seller" if missed else "late_delivery_logistics"
        actors = set(shipment.late_event_actors)
        by_events = None
        if actors == {"seller"}:
            by_events = "late_delivery_seller"
        elif actors == {"logistics_provider"}:
            by_events = "late_delivery_logistics"
        if missed is None and by_events:
            by_timestamps = by_events
        elif by_events and by_events != by_timestamps:
            result.conflicts.append(
                {
                    "field": "late_delivery_responsible_party",
                    "sources": ["get_shipment_summary.timestamps", "get_shipment_summary.events"],
                    "selected_source": "get_shipment_summary.timestamps",
                    "resolution_code": "PREFER_HANDOFF_TIMESTAMPS",
                }
            )
        signals.append((by_timestamps, "DELIVERED_AFTER_ESTIMATE"))

    if signals:
        result.issue, first_reason = signals[0]
        result.reasons.append(first_reason)
        result.competing = sorted({issue for issue, _ in signals[1:] if issue != result.issue})
    else:
        result.reasons.append("NO_ANOMALY_IN_SCOPED_EVIDENCE")
    return result


def order_shipment_conflicts(order: dict[str, Any], shipment: dict[str, Any]) -> list[dict]:
    """Cross-source check between the order row and the shipment summary."""
    pairs = (
        ("order_status", "order_status", "order_status"),
        ("delivered_carrier_at", "order_delivered_carrier_date", "delivered_carrier_at"),
        ("delivered_customer_at", "order_delivered_customer_date", "delivered_customer_at"),
        ("estimated_delivery_at", "order_estimated_delivery_date", "estimated_delivery_at"),
    )
    conflicts = []
    for name, order_key, shipment_key in pairs:
        left, right = order.get(order_key), shipment.get(shipment_key)
        left_ts, right_ts = parse_ts(left), parse_ts(right)
        same = (left_ts == right_ts) if (left_ts or right_ts) else (left == right)
        if not same:
            conflicts.append(
                {
                    "field": name,
                    "sources": ["get_order", "get_shipment_summary"],
                    "selected_source": "get_order",
                    "resolution_code": "PREFER_AUTHORITATIVE_ORDER_ROW",
                }
            )
    return conflicts
