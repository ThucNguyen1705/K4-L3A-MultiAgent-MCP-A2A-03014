"""Specialist agents. Each one owns a narrow slice of MCP tools (see ``TOOL_PERMISSIONS``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .a2a import A2ABus, A2AMessage
from .analysis import (
    INSUFFICIENT,
    OrderFacts,
    PaymentFacts,
    ShipmentFacts,
    build_order_facts,
    build_payment_facts,
    build_shipment_facts,
    money,
)
from .evidence import EvidenceLedger, EvidenceUnavailable

COORDINATOR = "coordinator"
ORDER_AGENT = "order-agent"
PAYMENT_AGENT = "payment-agent"
SHIPMENT_AGENT = "shipment-agent"
POLICY_AGENT = "policy-agent"
VERIFIER = "verifier"


class OrderAgent:
    name = ORDER_AGENT

    async def investigate(
        self, task: A2AMessage, ledger: EvidenceLedger, bus: A2ABus, opened_at: datetime
    ) -> A2AMessage:
        order_id = task.payload["order_id"]
        try:
            order = await ledger.fetch(self.name, "get_order", order_id=order_id)
        except EvidenceUnavailable:
            return bus.handoff(self.name, COORDINATOR, "ORDER_NOT_FOUND", reply_to=task, facts=None)
        try:
            items = (await ledger.fetch(self.name, "get_order_items", order_id=order_id)).data
        except EvidenceUnavailable:
            items = []
        facts = build_order_facts(order.data, list(items or []), opened_at)
        return bus.handoff(
            self.name,
            COORDINATOR,
            "ORDER_CONTEXT_READY",
            evidence_refs=ledger.refs(["get_order", "get_order_items"]),
            reply_to=task,
            facts=facts,
            raw_order=order.data,
        )

    async def verify_sellers(
        self, task: A2AMessage, ledger: EvidenceLedger, bus: A2ABus
    ) -> A2AMessage:
        wanted = set(task.payload["seller_ids"])
        try:
            sellers = await ledger.fetch(
                self.name, "get_sellers", order_id=task.payload["order_id"]
            )
        except EvidenceUnavailable:
            return bus.handoff(
                self.name, COORDINATOR, "SELLER_UNCONFIRMED", reply_to=task, confirmed=[]
            )
        known = {str(row.get("seller_id")) for row in sellers.data or []}
        confirmed = sorted(wanted & known)
        code = "SELLER_CONFIRMED" if confirmed and wanted <= known else "SELLER_UNCONFIRMED"
        return bus.handoff(
            self.name,
            COORDINATOR,
            code,
            evidence_refs=[sellers.ref],
            reply_to=task,
            confirmed=confirmed,
        )


class PaymentAgent:
    name = PAYMENT_AGENT

    async def investigate(
        self, task: A2AMessage, ledger: EvidenceLedger, bus: A2ABus, order: OrderFacts
    ) -> A2AMessage:
        order_id = task.payload["order_id"]
        try:
            timeline = await ledger.fetch(self.name, "get_payment_timeline", order_id=order_id)
        except EvidenceUnavailable:
            return bus.handoff(
                self.name, COORDINATOR, "PAYMENT_EVIDENCE_MISSING", reply_to=task, facts=None
            )
        try:
            refunds = await ledger.fetch(self.name, "get_refund_timeline", order_id=order_id)
            refund_events: list[dict[str, Any]] | None = list(refunds.data.get("events") or [])
        except EvidenceUnavailable:
            refund_events = None
        facts = build_payment_facts(
            order.window, list(timeline.data.get("events") or []), refund_events
        )
        return bus.handoff(
            self.name,
            COORDINATOR,
            "PAYMENT_FACTS_READY",
            evidence_refs=ledger.refs(["get_payment_timeline", "get_refund_timeline"]),
            reply_to=task,
            facts=facts,
        )


class ShipmentAgent:
    name = SHIPMENT_AGENT

    async def investigate(
        self, task: A2AMessage, ledger: EvidenceLedger, bus: A2ABus, order: OrderFacts
    ) -> A2AMessage:
        try:
            summary = await ledger.fetch(
                self.name, "get_shipment_summary", order_id=task.payload["order_id"]
            )
        except EvidenceUnavailable:
            return bus.handoff(
                self.name, COORDINATOR, "SHIPMENT_EVIDENCE_MISSING", reply_to=task, facts=None
            )
        facts = build_shipment_facts(order.window, summary.data)
        if facts.delivered_at is None:
            code = "SHIPMENT_NOT_DELIVERED"
        elif facts.delivered_late:
            code = "SHIPMENT_LATE"
        else:
            code = "SHIPMENT_ON_TIME"
        return bus.handoff(
            self.name,
            COORDINATOR,
            code,
            evidence_refs=[summary.ref],
            reply_to=task,
            facts=facts,
            raw_shipment=summary.data,
        )


@dataclass(frozen=True)
class PolicyDecision:
    primary_issue: str
    case_status: str
    action: str
    refund_brl: Decimal
    responsible_parties: tuple[dict[str, str | None], ...]
    policy_ref: str | None


FALLBACK_DECISION = PolicyDecision(
    primary_issue=INSUFFICIENT,
    case_status="needs_investigation",
    action="escalate_manual_review",
    refund_brl=Decimal("0.00"),
    responsible_parties=({"party_type": "unknown", "party_id": None},),
    policy_ref=None,
)


class PolicyAgent:
    name = POLICY_AGENT

    async def decide(
        self, task: A2AMessage, ledger: EvidenceLedger, bus: A2ABus
    ) -> tuple[A2AMessage, PolicyDecision]:
        issue = task.payload["primary_issue"]
        seller_ids: list[str] = task.payload["seller_ids"]
        decision = FALLBACK_DECISION
        try:
            policy = await ledger.fetch(
                self.name, "get_policy", policy_version=task.payload["policy_version"]
            )
            rule = (policy.data.get("rules") or {}).get(issue)
        except EvidenceUnavailable:
            policy, rule = None, None
        if policy is not None and rule and issue != INSUFFICIENT:
            parties: list[dict[str, str | None]] = []
            for party in rule.get("responsible_parties") or []:
                party_type = party.get("party_type") or "unknown"
                if party_type == "seller":
                    # The policy template names an example seller; bind it to this order's seller.
                    ids = seller_ids or [None]
                    parties.extend({"party_type": "seller", "party_id": sid} for sid in ids)
                else:
                    parties.append({"party_type": party_type, "party_id": None})
            decision = PolicyDecision(
                primary_issue=issue,
                case_status=rule["case_status"],
                action=rule["recommended_action"],
                refund_brl=money(rule.get("refund_brl", 0)),
                responsible_parties=tuple(parties),
                policy_ref=policy.ref,
            )
        ledger.trace.emit(
            case_id=ledger.case_id,
            event_type="policy_decided",
            actor=self.name,
            decision_code=decision.action,
            evidence_refs=[decision.policy_ref] if decision.policy_ref else None,
            attributes={
                "primary_issue": decision.primary_issue,
                "case_status": decision.case_status,
                "refund_brl": float(decision.refund_brl),
            },
        )
        message = bus.handoff(
            self.name,
            COORDINATOR,
            "POLICY_APPLIED" if decision.policy_ref else "POLICY_UNAVAILABLE",
            evidence_refs=[decision.policy_ref] if decision.policy_ref else [],
            reply_to=task,
        )
        return message, decision


def evidence_refund_basis(
    issue: str, order: OrderFacts, payment: PaymentFacts | None
) -> Decimal | None:
    """Amount the evidence itself supports for the policy action, when one is derivable."""
    if payment is None:
        return None
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        return payment.anchored_total_brl
    if issue == "refund_failed":
        failed = payment.refunds_with_status("failed")
        return money(failed[0].get("amount_brl")) if failed else None
    if issue == "payment_mismatch" and payment.open_mismatches:
        return money(payment.open_mismatches[0].get("amount_brl"))
    if issue == "duplicate_charge" and payment.anchored_captures:
        return money(payment.anchored_captures[-1].get("amount_brl"))
    if issue == "late_delivery_seller" and order.items:
        return sum((money(i.get("freight_value")) for i in order.items), Decimal("0.00"))
    return None


class Verifier:
    name = VERIFIER

    def check(
        self,
        output: dict[str, Any],
        *,
        case: dict[str, Any],
        ledger: EvidenceLedger,
        decision: PolicyDecision,
    ) -> list[str]:
        failures: list[str] = []
        try:
            ledger.trace.contracts.validate_output(output, f"outputs/{case['case_id']}.json")
        except ValueError as exc:
            failures.append(f"SCHEMA:{exc}")
        if output.get("case_id") != case["case_id"]:
            failures.append("CASE_ID_MISMATCH")
        entities = output["affected_entities"]
        if entities["order_ids"] != [case["customer_request"]["claimed_order_id"]]:
            failures.append("ENTITY_SCOPE")
        refs = set(output["evidence_refs"])
        if not refs <= ledger.all_refs:
            failures.append("EVIDENCE_NOT_OWNED_BY_CASE")
        if decision.policy_ref and decision.policy_ref not in refs:
            failures.append("POLICY_EVIDENCE_NOT_CITED")
        expected_claims = {c["claim_id"] for c in case["customer_request"].get("claims") or []}
        claims = output.get("claim_assessments") or []
        if {c["claim_id"] for c in claims} != expected_claims:
            failures.append("CLAIM_COVERAGE")
        if any(not set(c["evidence_refs"]) <= refs for c in claims):
            failures.append("CLAIM_EVIDENCE_LINKAGE")
        finance = output["financial_resolution"]
        lines_total = sum(money(line["amount_brl"]) for line in finance["refund_lines"])
        if lines_total != money(finance["recommended_refund_brl"]):
            failures.append("MONEY_TOTALS")
        status = output["assessment"]["case_status"]
        refund = money(finance["recommended_refund_brl"])
        actions = output["resolution_actions"]
        if status in {"no_action", "needs_investigation"} and refund != 0:
            failures.append("STATUS_REFUND_INCONSISTENT")
        if status == "action_required" and refund <= 0:
            failures.append("STATUS_REFUND_INCONSISTENT")
        if status == "no_action" and actions != ["document_no_action"]:
            failures.append("STATUS_ACTION_INCONSISTENT")
        if not actions or len(actions) != len(set(actions)):
            failures.append("ACTIONS_INVALID")
        seller_ids = set(entities["seller_ids"])
        for party in output["root_cause_analysis"]["responsible_parties"]:
            if party["party_type"] == "seller" and party["party_id"] not in seller_ids:
                failures.append("SELLER_RESPONSIBILITY_UNLINKED")
        confidence = output["assessment"]["confidence"]
        if not 0 <= confidence <= 1:
            failures.append("CONFIDENCE_BOUNDS")
        return failures

    def report(
        self, task: A2AMessage, bus: A2ABus, failures: list[str], evidence_refs: list[str]
    ) -> A2AMessage:
        bus.trace.emit(
            case_id=bus.case_id,
            event_type="verification_completed",
            actor=self.name,
            decision_code="PASS" if not failures else "FAIL",
            evidence_refs=evidence_refs[:20] or None,
            attributes={
                "checks_failed": len(failures),
                "first_failure": failures[0][:120] if failures else None,
            },
        )
        return bus.handoff(
            self.name,
            COORDINATOR,
            "VERIFIED" if not failures else "REJECTED",
            reply_to=task,
            failures=failures,
        )


__all__ = [
    "COORDINATOR",
    "FALLBACK_DECISION",
    "ORDER_AGENT",
    "PAYMENT_AGENT",
    "POLICY_AGENT",
    "SHIPMENT_AGENT",
    "VERIFIER",
    "OrderAgent",
    "PaymentAgent",
    "PolicyAgent",
    "PolicyDecision",
    "ShipmentAgent",
    "ShipmentFacts",
    "Verifier",
    "evidence_refund_basis",
]
