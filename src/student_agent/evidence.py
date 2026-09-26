"""Per-case evidence ledger: the only path from agents to the MCP Evidence Gateway."""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# Which actor may call which MCP tool. Coordinator and verifier never touch the gateway.
TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    "order-agent": frozenset({"get_order", "get_order_items", "get_sellers"}),
    "payment-agent": frozenset({"get_payment_timeline", "get_refund_timeline"}),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "policy-agent": frozenset({"get_policy"}),
}

TRANSPORT_ATTEMPTS = 3
TOOL_ERROR_ATTEMPTS = 2
BACKOFF_SECONDS = 0.5

_discovered: weakref.WeakKeyDictionary[EvidenceGateway, frozenset[str]] = (
    weakref.WeakKeyDictionary()
)


class EvidenceUnavailable(RuntimeError):
    """The gateway did not return usable evidence (not found, out of scope or undiscovered)."""


@dataclass(frozen=True)
class Evidence:
    tool: str
    actor: str
    ref: str
    domain: str
    result_hash: str
    data: Any
    warnings: tuple[str, ...]


def _order_ids_in(tool: str, data: Any) -> set[str]:
    if tool in {"get_order", "get_shipment_summary", "get_payment_timeline", "get_refund_timeline"}:
        found = {str(data.get("order_id"))} if isinstance(data, dict) else {"<invalid>"}
        if isinstance(data, dict):
            for key in ("events", "payments"):
                found |= {
                    str(row.get("order_id")) for row in data.get(key) or [] if row.get("order_id")
                }
        return found
    if tool == "get_order_items":
        rows = data if isinstance(data, list) else []
        return {str(row.get("order_id")) for row in rows}
    return set()


class EvidenceLedger:
    def __init__(self, case_id: str, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace
        self.by_tool: dict[str, Evidence] = {}
        self.not_found: set[str] = set()

    async def _tools(self) -> frozenset[str]:
        cached = _discovered.get(self.gateway)
        if cached is None:
            cached = frozenset(await self.gateway.list_tools())
            _discovered[self.gateway] = cached
        return cached

    async def _call_with_retry(self, tool: str, arguments: dict[str, str]) -> dict[str, Any]:
        tool_errors = 0
        transport_errors = 0
        while True:
            try:
                return await self.gateway.call(tool, case_id=self.case_id, **arguments)
            except RuntimeError as exc:  # MCP isError: deterministic for "not found"
                tool_errors += 1
                if tool_errors >= TOOL_ERROR_ATTEMPTS:
                    raise EvidenceUnavailable(f"{tool}: {exc}") from exc
            except (TimeoutError, OSError, ConnectionError) as exc:
                transport_errors += 1
                if transport_errors >= TRANSPORT_ATTEMPTS:
                    raise EvidenceUnavailable(f"{tool}: transport failure {exc!r}") from exc
            await asyncio.sleep(BACKOFF_SECONDS * (tool_errors + transport_errors))

    async def fetch(self, actor: str, tool: str, **arguments: str) -> Evidence:
        if tool not in TOOL_PERMISSIONS.get(actor, frozenset()):
            raise PermissionError(f"{actor} is not allowed to call {tool}")
        if tool in self.by_tool:
            return self.by_tool[tool]
        if tool not in await self._tools():
            raise EvidenceUnavailable(f"{tool} was not advertised by tool discovery")
        try:
            raw = await self._call_with_retry(tool, arguments)
        except EvidenceUnavailable:
            self.not_found.add(tool)
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool,
                decision_code="EVIDENCE_NOT_FOUND",
                attributes={"outcome": "not_found"},
            )
            raise

        data = raw["data"]
        problem = None
        expected_order = arguments.get("order_id")
        if expected_order is not None and _order_ids_in(tool, data) - {expected_order}:
            problem = "returned rows for another order"
        if tool == "get_policy" and (
            not isinstance(data, dict) or data.get("policy_version") != arguments["policy_version"]
        ):
            problem = "returned a different policy version"
        if problem:
            self.not_found.add(tool)
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool,
                decision_code="EVIDENCE_REJECTED_OUT_OF_SCOPE",
                attributes={"outcome": "rejected"},
            )
            raise EvidenceUnavailable(f"{tool} {problem}")

        evidence = Evidence(
            tool=tool,
            actor=actor,
            ref=raw["evidence_ref"],
            domain=raw["domain"],
            result_hash=raw["result_hash"],
            data=data,
            warnings=tuple(raw.get("warnings") or ()),
        )
        self.by_tool[tool] = evidence
        rows = len(data) if isinstance(data, list) else 1
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool,
            evidence_refs=[evidence.ref],
            attributes={
                "domain": evidence.domain,
                "result_hash": evidence.result_hash,
                "rows": rows,
                "warnings": len(evidence.warnings),
            },
        )
        return evidence

    def ref(self, tool: str) -> str | None:
        evidence = self.by_tool.get(tool)
        return evidence.ref if evidence else None

    def refs(self, tools: list[str] | tuple[str, ...]) -> list[str]:
        return [ref for ref in (self.ref(tool) for tool in tools) if ref]

    @property
    def all_refs(self) -> set[str]:
        return {evidence.ref for evidence in self.by_tool.values()}
