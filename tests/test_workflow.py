from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.analysis import (
    build_order_facts,
    build_payment_facts,
    build_shipment_facts,
    parse_ts,
    triage,
)
from student_agent.contracts import Contracts
from student_agent.evidence import EvidenceLedger
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
ORDER_ID = "0123456789abcdef0123456789abcdef"
OPENED = "2018-03-12T09:00:00-03:00"

POLICY = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V1",
    "rules": {
        "canceled_order_paid": {
            "case_status": "action_required",
            "recommended_action": "issue_refund",
            "refund_brl": 79.0,
            "responsible_parties": [{"party_id": None, "party_type": "platform"}],
        },
        "late_delivery_seller": {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 18.0,
            "responsible_parties": [{"party_id": "seller-example", "party_type": "seller"}],
        },
        "refund_failed": {
            "case_status": "action_required",
            "recommended_action": "retry_refund",
            "refund_brl": 52.0,
            "responsible_parties": [{"party_id": None, "party_type": "payment_provider"}],
        },
        "unsupported_claim": {
            "case_status": "no_action",
            "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
        "valid_split_payment": {
            "case_status": "no_action",
            "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
        "unavailable_order_paid": {
            "case_status": "action_required",
            "recommended_action": "issue_refund",
            "refund_brl": 89.0,
            "responsible_parties": [{"party_id": "seller-example", "party_type": "seller"}],
        },
    },
}


def order_row(status: str = "delivered", delivered: str | None = "2018-03-09T09:00:00-03:00"):
    return {
        "order_id": ORDER_ID,
        "customer_id": "customer-row-x",
        "order_status": status,
        "order_purchase_timestamp": "2018-02-28T09:00:00-03:00",
        "order_approved_at": "2018-02-28T10:00:00-03:00",
        "order_delivered_carrier_date": "2018-03-02T09:00:00-03:00",
        "order_delivered_customer_date": delivered,
        "order_estimated_delivery_date": "2018-03-10T09:00:00-03:00",
    }


def item(limit: str = "2018-03-03T09:00:00-03:00", freight: str = "10.00") -> dict[str, Any]:
    return {
        "order_id": ORDER_ID,
        "order_item_id": "item-1",
        "product_id": "product-1",
        "seller_id": "seller-1",
        "shipping_limit_date": limit,
        "price": "79.00",
        "freight_value": freight,
    }


def capture(at: str, amount: str) -> dict[str, Any]:
    return {
        "order_id": ORDER_ID,
        "event_at": at,
        "event_type": "captured",
        "amount_brl": amount,
        "status": "confirmed",
    }


def shipment_from(order: dict[str, Any], limits: list[str], events=()) -> dict[str, Any]:
    return {
        "order_id": ORDER_ID,
        "order_status": order["order_status"],
        "delivered_carrier_at": order["order_delivered_carrier_date"],
        "delivered_customer_at": order["order_delivered_customer_date"],
        "estimated_delivery_at": order["order_estimated_delivery_date"],
        "shipping_limits": [
            {"order_item_id": "item-1", "seller_id": "seller-1", "shipping_limit_at": limit}
            for limit in limits
        ],
        "events": list(events),
    }


def facts(order, items, pay_events, refund_events, shipment):
    order_facts = build_order_facts(order, items, parse_ts(OPENED))
    payment = build_payment_facts(order_facts.window, pay_events, refund_events)
    ship = build_shipment_facts(order_facts.window, shipment)
    return order_facts, payment, ship


def test_refund_failure_wins_over_colliding_split_decoy() -> None:
    order = order_row()
    pay = [
        capture("2018-02-28T10:00:00-03:00", "52.00"),
        capture("2018-02-28T10:00:00-03:00", "44.50"),
        capture("2018-02-28T11:00:00-03:00", "44.50"),
    ]
    refunds = [
        {
            "order_id": ORDER_ID,
            "event_at": "2018-03-11T09:00:00-03:00",
            "event_type": "refund_requested",
            "amount_brl": "52.00",
            "status": "failed",
        }
    ]
    result = triage(*facts(order, [item(), item()], pay, refunds, shipment_from(order, [])))
    assert result.issue == "refund_failed"


def test_identical_duplicate_rows_are_not_a_duplicate_charge() -> None:
    order = order_row(status="unavailable", delivered=None)
    pay = [capture("2018-02-28T10:00:00-03:00", "89.00")] * 2
    order_facts, payment, ship = facts(order, [item()] * 2, pay, None, shipment_from(order, []))
    assert len(payment.anchored_captures) == 1
    assert triage(order_facts, payment, ship).issue == "unavailable_order_paid"


def test_decoy_rows_outside_the_order_lifecycle_are_ignored() -> None:
    order = order_row()
    decoy_limit = "2018-05-14T09:00:00-03:00"  # after the promised delivery date
    pay = [
        capture("2018-02-28T10:00:00-03:00", "89.00"),
        capture("2018-05-11T10:00:00-03:00", "18"),
    ]
    late_event = {
        "order_id": ORDER_ID,
        "event_at": "2018-03-07T09:00:00-03:00",  # does not match the recorded delivery instant
        "event_type": "delivered_late",
        "actor": "logistics_provider",
        "status": "confirmed",
    }
    shipment = shipment_from(order, ["2018-03-03T09:00:00-03:00", decoy_limit], [late_event])
    order_facts, payment, ship = facts(
        order, [item(), item(decoy_limit, "18.00")], pay, None, shipment
    )
    assert order_facts.excluded_items == 1
    assert len(payment.captures) == 1
    assert ship.late_event_actors == ()
    assert triage(order_facts, payment, ship).issue == "unsupported_claim"


def test_late_delivery_is_attributed_to_seller_when_handoff_missed_limit() -> None:
    order = order_row(delivered="2018-03-11T09:00:00-03:00")
    order["order_delivered_carrier_date"] = "2018-03-05T09:00:00-03:00"
    shipment = shipment_from(order, ["2018-03-03T09:00:00-03:00"])
    pay = [capture("2018-02-28T10:00:00-03:00", "18.00")]
    result = triage(*facts(order, [item(freight="18.00")], pay, None, shipment))
    assert result.issue == "late_delivery_seller"


def test_split_payment_matches_order_total() -> None:
    order = order_row()
    pay = [
        capture("2018-02-28T10:00:00-03:00", "44.50"),
        capture("2018-02-28T11:00:00-03:00", "44.50"),
    ]
    result = triage(*facts(order, [item()], pay, None, shipment_from(order, [])))
    assert result.issue == "valid_split_payment"


class FakeGateway:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def list_tools(self) -> list[str]:
        return sorted([*self.responses, "get_refund_timeline"])

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        if tool_name not in self.responses:
            raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool {tool_name}")
        domain, data = self.responses[tool_name]
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name.replace('_', '')}{'x' * 20}",
            "result_hash": f"sha256:{digest}",
            "domain": domain,
            "data": data,
            "warnings": [],
        }


def canceled_gateway() -> FakeGateway:
    order = order_row(status="canceled", delivered=None)
    return FakeGateway(
        {
            "get_order": ("order", order),
            "get_order_items": ("item", [item()]),
            "get_payment_timeline": (
                "payment",
                {
                    "order_id": ORDER_ID,
                    "payments": [],
                    "events": [capture("2018-02-28T10:00:00-03:00", "79.00")],
                },
            ),
            "get_shipment_summary": (
                "shipment",
                shipment_from(order, ["2018-03-03T09:00:00-03:00"]),
            ),
            "get_sellers": ("seller", [{"seller_id": "seller-1"}]),
            "get_policy": ("policy", POLICY),
        }
    )


def test_solve_case_end_to_end(tmp_path: Path) -> None:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = {
        "case_id": "L3A_CASE_T01",
        "opened_at": OPENED,
        "customer_request": {
            "language": "vi",
            "message": "test",
            "claimed_order_id": ORDER_ID,
            "claims": [
                {"claim_id": "claim-a", "topic": "canceled_order_paid"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }
    gateway = canceled_gateway()
    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]

    contracts.validate_output(output, "output")
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert output["resolution_actions"] == ["issue_refund"]
    assert {c["verdict"] for c in output["claim_assessments"]} == {"supported"}
    assert all(args["case_id"] == "L3A_CASE_T01" for _, args in gateway.calls)

    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    kinds = {e["event_type"] for e in events}
    assert {"task_assigned", "handoff", "tool_result_consumed", "verification_completed"} <= kinds
    traced_refs = {
        ref
        for e in events
        if e["event_type"] == "tool_result_consumed"
        for ref in e.get("evidence_refs", [])
    }
    assert set(output["evidence_refs"]) <= traced_refs
    assert len({e["actor"] for e in events}) >= 5


def test_agents_cannot_call_tools_outside_their_role(tmp_path: Path) -> None:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    ledger = EvidenceLedger(
        "L3A_CASE_T02", canceled_gateway(), TraceWriter(tmp_path / "t.jsonl", contracts)
    )  # type: ignore[arg-type]
    with pytest.raises(PermissionError):
        asyncio.run(ledger.fetch("payment-agent", "get_order", order_id=ORDER_ID))


def test_rejecting_gateway_aborts_instead_of_writing_fallbacks(tmp_path: Path) -> None:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = {
        "case_id": "L3A_CASE_T03",
        "opened_at": OPENED,
        "customer_request": {"claimed_order_id": ORDER_ID, "claims": []},
        "policy_version": "EC_POLICY_V1",
    }
    gateway = FakeGateway({"get_order": None, "get_policy": None})
    gateway.responses.clear()  # every advertised tool now fails like a closed run
    gateway.list_tools = lambda: _tools()  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="open the /l3a workspace"):
        asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]


async def _tools() -> list[str]:
    return ["get_order", "get_order_items", "get_policy", "get_refund_timeline"]
