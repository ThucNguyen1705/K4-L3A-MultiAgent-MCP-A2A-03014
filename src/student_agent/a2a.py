"""Minimal in-process A2A (agent-to-agent) protocol.

Every message carries the ``case_id`` as correlation key, an explicit sender/recipient and a
hop counter. The bus mirrors each message into the observable trace (``task_assigned`` or
``handoff``) and refuses messages that belong to another case or exceed the hop budget.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

from .trace import TraceWriter

MAX_HOPS_PER_CASE = 24


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class A2AMessage:
    case_id: str
    sender: str
    recipient: str
    kind: str  # "task" | "result"
    intent: str
    payload: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    hop: int = 0
    message_id: str = field(default_factory=lambda: f"msg_{secrets.token_hex(8)}")


class A2ABus:
    def __init__(self, case_id: str, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.trace = trace
        self.log: list[A2AMessage] = []

    def _admit(self, message: A2AMessage) -> A2AMessage:
        if message.case_id != self.case_id:
            raise ProtocolError(f"message for {message.case_id} sent on bus for {self.case_id}")
        if message.sender == message.recipient:
            raise ProtocolError("an agent cannot message itself")
        if len(self.log) >= MAX_HOPS_PER_CASE:
            raise ProtocolError(f"hop budget exhausted for {self.case_id}")
        self.log.append(message)
        return message

    def assign(self, sender: str, recipient: str, intent: str, **payload: Any) -> A2AMessage:
        message = self._admit(
            A2AMessage(
                case_id=self.case_id,
                sender=sender,
                recipient=recipient,
                kind="task",
                intent=intent,
                payload=payload,
                hop=len(self.log),
            )
        )
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor=sender,
            target=recipient,
            decision_code=intent,
            attributes={"message_id": message.message_id, "hop": message.hop},
        )
        return message

    def handoff(
        self,
        sender: str,
        recipient: str,
        decision_code: str,
        evidence_refs: list[str] | tuple[str, ...] = (),
        reply_to: A2AMessage | None = None,
        **payload: Any,
    ) -> A2AMessage:
        refs = tuple(dict.fromkeys(evidence_refs))
        message = self._admit(
            A2AMessage(
                case_id=self.case_id,
                sender=sender,
                recipient=recipient,
                kind="result",
                intent=decision_code,
                payload=payload,
                evidence_refs=refs,
                hop=len(self.log),
            )
        )
        attributes: dict[str, str | int | float | bool | None] = {
            "message_id": message.message_id,
            "hop": message.hop,
        }
        if reply_to is not None:
            attributes["in_reply_to"] = reply_to.message_id
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=sender,
            target=recipient,
            decision_code=decision_code,
            evidence_refs=list(refs)[:20] or None,
            attributes=attributes,
        )
        return message
