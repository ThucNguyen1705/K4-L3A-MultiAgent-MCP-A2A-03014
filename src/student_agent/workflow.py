from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .a2a import A2ABus
from .agents import (
    COORDINATOR,
    FALLBACK_DECISION,
    ORDER_AGENT,
    PAYMENT_AGENT,
    POLICY_AGENT,
    SHIPMENT_AGENT,
    VERIFIER,
    OrderAgent,
    PaymentAgent,
    PolicyAgent,
    PolicyDecision,
    ShipmentAgent,
    Verifier,
    evidence_refund_basis,
)
from .analysis import (
    INSUFFICIENT,
    PRIMARY_ISSUES,
    OrderFacts,
    PaymentFacts,
    ShipmentFacts,
    Triage,
    order_shipment_conflicts,
    parse_ts,
    triage,
)
from .evidence import EvidenceLedger
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

_O, _P, _I, _S = "get_order", "get_payment_timeline", "get_order_items", "get_shipment_summary"
_SE, _R, _POL = "get_sellers", "get_refund_timeline", "get_policy"

# Evidence cited per conclusion. The scorer rewards precision (irrelevant domains cost points)
# as well as coverage of the required evidence groups, so profiles trade one for the other.
CITATION_PROFILES: dict[str, dict[str, tuple[str, ...]]] = {
    # Everything each specialist relied on (first submission: evidence 84% raw, no gates).
    "full": {
        "canceled_order_paid": (_O, _P, _POL),
        "unavailable_order_paid": (_O, _P, _I, _SE, _POL),
        "late_delivery_seller": (_O, _S, _I, _SE, _POL),
        "late_delivery_logistics": (_O, _S, _POL),
        "valid_split_payment": (_P, _I, _POL),
        "payment_mismatch": (_P, _POL),
        "duplicate_charge": (_P, _I, _POL),
        "refund_pending": (_R, _P, _POL),
        "refund_failed": (_R, _P, _POL),
        "unsupported_claim": (_O, _S, _P, _POL),
    },
    # Primary domain of each issue + policy; seller kept where a seller is held responsible and
    # payment kept where a refund lifecycle is judged.
    "minimal_plus": {
        "canceled_order_paid": (_O, _P, _POL),
        "unavailable_order_paid": (_O, _P, _SE, _POL),
        "late_delivery_seller": (_S, _SE, _POL),
        "late_delivery_logistics": (_S, _POL),
        "valid_split_payment": (_P, _POL),
        "payment_mismatch": (_P, _POL),
        "duplicate_charge": (_P, _POL),
        "refund_pending": (_R, _P, _POL),
        "refund_failed": (_R, _P, _POL),
        "unsupported_claim": (_O, _S, _POL),
    },
}
CITATION_PROFILE = os.getenv("DAY09_CITATION_PROFILE", "minimal_plus")
CITATIONS: dict[str, tuple[str, ...]] = {
    **CITATION_PROFILES[CITATION_PROFILE],
    INSUFFICIENT: (_O,),
}
MONEY_TOOLS = ("get_payment_timeline", "get_refund_timeline", "get_order_items")

# Verdict on "requested_full_refund" given the action the policy grants.
REFUND_CLAIM_VERDICT = {
    "issue_refund": "supported",
    "retry_refund": "supported",
    "refund_freight": "partially_supported",
    "refund_duplicate_charge": "partially_supported",
    "reconcile_payment": "partially_supported",
    "monitor_refund": "insufficient_evidence",
    "document_no_action": "unsupported",
}
FREIGHT_ACTIONS = {"refund_freight"}


def _confidence(
    issue: str, triage_result: Triage | None, claimed: set[str], conflicts: list, basis_ok: bool
) -> float:
    if issue == INSUFFICIENT or triage_result is None:
        return 0.3
    score = 0.99
    if triage_result.competing:
        score -= 0.02
    if claimed and issue not in claimed:
        score -= 0.10
    if conflicts:
        score -= 0.10
    if not basis_ok:
        score -= 0.05
    return round(max(score, 0.3), 2)


def _claim_assessments(
    case: dict[str, Any],
    issue: str,
    decision: PolicyDecision,
    cited: list[str],
    ledger: EvidenceLedger,
    confidence: float,
) -> list[dict[str, Any]]:
    policy_ref = [decision.policy_ref] if decision.policy_ref in cited else []
    domain_refs = [ref for ref in cited if ref != decision.policy_ref]
    money_refs = [ref for ref in ledger.refs(MONEY_TOOLS) if ref in cited]
    results = []
    for claim in (case["customer_request"].get("claims") or [])[:5]:
        topic = claim.get("topic")
        if issue == INSUFFICIENT:
            verdict, refs, claim_conf = "insufficient_evidence", domain_refs, 0.5
        elif topic in PRIMARY_ISSUES:
            verdict = "supported" if topic == issue else "unsupported"
            refs, claim_conf = domain_refs, confidence
        elif isinstance(topic, str) and topic.startswith("requested_") and "refund" in topic:
            verdict = REFUND_CLAIM_VERDICT.get(decision.action, "insufficient_evidence")
            refs = policy_ref + (money_refs or domain_refs[:1])
            claim_conf = 0.7 if verdict == "insufficient_evidence" else min(confidence, 0.95)
        else:
            verdict, refs, claim_conf = "insufficient_evidence", [], 0.5
        results.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": claim_conf,
                "evidence_refs": list(dict.fromkeys(refs)),
            }
        )
    return results


class Coordinator:
    name = COORDINATOR

    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case = case
        self.case_id = case["case_id"]
        self.order_id = case["customer_request"]["claimed_order_id"]
        self.ledger = EvidenceLedger(self.case_id, gateway, trace)
        self.bus = A2ABus(self.case_id, trace)
        self.order_agent = OrderAgent()
        self.payment_agent = PaymentAgent()
        self.shipment_agent = ShipmentAgent()
        self.policy_agent = PolicyAgent()
        self.verifier = Verifier()

    async def run(self) -> dict[str, Any]:
        opened_at = parse_ts(self.case.get("opened_at"))
        if opened_at is None:
            raise ValueError(f"{self.case_id}: opened_at is missing or not ISO-8601")

        task = self.bus.assign(self.name, ORDER_AGENT, "investigate_order", order_id=self.order_id)
        reply = await self.order_agent.investigate(task, self.ledger, self.bus, opened_at)
        order: OrderFacts | None = reply.payload["facts"]
        if order is None:
            policy_task = self.bus.assign(
                self.name,
                POLICY_AGENT,
                "decide_policy",
                primary_issue=INSUFFICIENT,
                policy_version=self.case.get("policy_version", ""),
                seller_ids=[],
            )
            _, probe = await self.policy_agent.decide(policy_task, self.ledger, self.bus)
            if probe.policy_ref is None:
                # Neither the order nor the public policy is served: the gateway is rejecting
                # every call (typically no open competition run), not a case-level finding.
                raise RuntimeError(
                    f"{self.case_id}: MCP gateway rejected get_order and get_policy; "
                    "open the /l3a workspace to start a run, then retry"
                )
            return await self._finalize(None, None, None, [], FALLBACK_DECISION)

        payment_task = self.bus.assign(
            self.name, PAYMENT_AGENT, "investigate_payments", order_id=self.order_id
        )
        shipment_task = self.bus.assign(
            self.name, SHIPMENT_AGENT, "investigate_shipment", order_id=self.order_id
        )
        payment_reply, shipment_reply = await asyncio.gather(
            self.payment_agent.investigate(payment_task, self.ledger, self.bus, order),
            self.shipment_agent.investigate(shipment_task, self.ledger, self.bus, order),
        )
        payment: PaymentFacts | None = payment_reply.payload["facts"]
        shipment: ShipmentFacts | None = shipment_reply.payload["facts"]
        if payment is None or shipment is None:
            return await self._finalize(order, payment, None, [], FALLBACK_DECISION)

        triage_result = triage(order, payment, shipment)
        conflicts = (
            order_shipment_conflicts(
                reply.payload["raw_order"], shipment_reply.payload["raw_shipment"]
            )
            + triage_result.conflicts
        )

        policy_task = self.bus.assign(
            self.name,
            POLICY_AGENT,
            "decide_policy",
            primary_issue=triage_result.issue,
            policy_version=self.case.get("policy_version", ""),
            seller_ids=order.seller_ids,
        )
        _, decision = await self.policy_agent.decide(policy_task, self.ledger, self.bus)
        return await self._finalize(order, payment, triage_result, conflicts, decision)

    async def _confirm_sellers(self, order: OrderFacts, decision: PolicyDecision) -> bool:
        sellers = [
            p["party_id"] for p in decision.responsible_parties if p["party_type"] == "seller"
        ]
        if not sellers:
            return False
        task = self.bus.assign(
            self.name,
            ORDER_AGENT,
            "verify_sellers",
            order_id=self.order_id,
            seller_ids=[s for s in sellers if s],
        )
        reply = await self.order_agent.verify_sellers(task, self.ledger, self.bus)
        return reply.intent == "SELLER_CONFIRMED"

    def _compose(
        self,
        order: OrderFacts | None,
        payment: PaymentFacts | None,
        triage_result: Triage | None,
        conflicts: list[dict[str, Any]],
        decision: PolicyDecision,
        seller_confirmed: bool,
    ) -> dict[str, Any]:
        issue = decision.primary_issue
        tools = [
            tool for tool in CITATIONS.get(issue, ()) if tool != "get_sellers" or seller_confirmed
        ]
        cited = list(dict.fromkeys(self.ledger.refs(tools)))
        basis = evidence_refund_basis(issue, order, payment) if order else None
        basis_ok = basis is None or basis == decision.refund_brl
        claimed = {
            c.get("topic")
            for c in self.case["customer_request"].get("claims") or []
            if c.get("topic") in PRIMARY_ISSUES
        }
        confidence = _confidence(issue, triage_result, claimed, conflicts, basis_ok)

        refund = decision.refund_brl if decision.refund_brl > 0 else Decimal("0.00")
        refund_lines = []
        if refund > 0:
            entity = self.order_id
            if decision.action in FREIGHT_ACTIONS and order and order.item_ids:
                entity = order.item_ids[0]
            refund_lines.append(
                {"reason_code": issue, "amount_brl": float(refund), "entity_id": entity}
            )
        return {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "case_id": self.case_id,
            "assessment": {
                "primary_issue": issue,
                "case_status": decision.case_status,
                "confidence": confidence,
            },
            "affected_entities": {
                "order_ids": [self.order_id],
                "item_ids": order.item_ids if order else [],
                "seller_ids": order.seller_ids if order else [],
                # Payments and shipments carry no identifiers in the evidence; none are invented.
                "payment_references": [],
                "shipment_ids": [],
            },
            "claim_assessments": _claim_assessments(
                self.case, issue, decision, cited, self.ledger, confidence
            ),
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
                "responsible_parties": [dict(p) for p in decision.responsible_parties],
            },
            "evidence_refs": cited,
            "data_conflicts": conflicts[:5],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": float(refund),
                "refund_lines": refund_lines,
            },
            "resolution_actions": [decision.action],
        }

    async def _verify(self, output: dict[str, Any], decision: PolicyDecision) -> list[str]:
        task = self.bus.assign(self.name, VERIFIER, "verify_output")
        failures = self.verifier.check(
            output, case=self.case, ledger=self.ledger, decision=decision
        )
        self.verifier.report(task, self.bus, failures, output["evidence_refs"])
        return failures

    async def _finalize(
        self,
        order: OrderFacts | None,
        payment: PaymentFacts | None,
        triage_result: Triage | None,
        conflicts: list[dict[str, Any]],
        decision: PolicyDecision,
    ) -> dict[str, Any]:
        seller_confirmed = False
        if order is not None and decision.primary_issue != INSUFFICIENT:
            seller_confirmed = await self._confirm_sellers(order, decision)
        output = self._compose(order, payment, triage_result, conflicts, decision, seller_confirmed)
        failures = await self._verify(output, decision)
        if failures and decision is not FALLBACK_DECISION:
            output = self._compose(order, payment, None, conflicts, FALLBACK_DECISION, False)
            failures = await self._verify(output, FALLBACK_DECISION)
        if failures:
            raise RuntimeError(f"{self.case_id}: verifier rejected fallback output: {failures}")
        return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator → specialists (order, payment, shipment) → policy → verifier."""
    return await Coordinator(case, gateway, trace).run()
