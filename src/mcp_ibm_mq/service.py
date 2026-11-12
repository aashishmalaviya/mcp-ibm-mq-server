"""Tool logic, kept separate from MCP wiring so it is easy to test.

Each call resolves who is acting (agent, and the user it acts for), narrows
the policy to what both may do, checks it, and records the decision in the
audit log with both identities.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Callable

from .approvals import ApprovalError, ApprovalStore
from .audit import AuditLog
from .identity import TOOL_SCOPES, Entitlements, Identity
from .mq_client import Message, MQClient
from .policy import Policy, PolicyError
from .tracing import Span, incoming_traceparent, start_span

_span: ContextVar[Span | None] = ContextVar("mq_tool_span", default=None)


def _render(msg: Message, max_chars: int = 4000) -> dict[str, Any]:
    text = msg.body.decode("utf-8", errors="replace")
    truncated = len(text) > max_chars
    return {
        "msg_id": msg.msg_id,
        "correl_id": msg.correl_id,
        "put_time": msg.put_time,
        "bytes": len(msg.body),
        "body": text[:max_chars],
        "truncated": truncated,
    }


class _Denied(Exception):
    def __init__(self, response: dict[str, Any]):
        self.response = response


class MQService:
    def __init__(
        self,
        client: MQClient,
        policy: Policy,
        audit: AuditLog,
        approvals: ApprovalStore | None = None,
        identity: Callable[[], Identity] | None = None,
        entitlements: Entitlements | None = None,
    ):
        self.client = client
        self.policy = policy
        self.audit = audit
        self.approvals = approvals or ApprovalStore()
        # Default: one agent identity for the whole process (stdio), named by the audit log.
        self.identity = identity or (lambda: Identity(agent_id=self.audit.actor))
        self.entitlements = entitlements or Entitlements()

    # -- helpers -----------------------------------------------------------

    def _record(self, who: Identity, tool: str, outcome: str, **fields: Any) -> None:
        span = _span.get()
        trace = {} if span is None else {
            "trace_id": span.trace_id, "span_id": span.span_id,
            "parent_span_id": span.parent_span_id, "duration_ms": span.elapsed_ms(),
        }
        self.audit.record(tool, outcome, actor=who.agent_id, on_behalf_of=who.user_id,
                          scope=TOOL_SCOPES.get(tool), **trace, **fields)

    @staticmethod
    def current_trace_id() -> str | None:
        span = _span.get()
        return span.trace_id if span else None

    def _begin(self, tool: str, queue: str | None = None) -> tuple[Identity, Policy]:
        _span.set(start_span(incoming_traceparent.get()))
        who = self.identity()
        try:
            return who, self.entitlements.effective(who, self.policy, tool)
        except PolicyError as err:
            raise _Denied(self._deny(who, tool, queue, err)) from None

    def _deny(self, who: Identity, tool: str, queue: str | None, err: Exception) -> dict[str, Any]:
        self._record(who, tool, "denied", queue=queue, reason=str(err))
        return {"ok": False, "error": str(err), "trace_id": self.current_trace_id()}

    # -- tools -------------------------------------------------------------

    def list_queues(self) -> dict[str, Any]:
        try:
            who, policy = self._begin("list_queues")
        except _Denied as d:
            return d.response
        self._record(who, "list_queues", "ok")
        return {
            "ok": True,
            "read": sorted(policy.read_queues),
            "write": [] if policy.read_only else sorted(policy.write_queues),
            "read_only": policy.read_only,
            "agent": who.agent_id,
            "on_behalf_of": who.user_id,
        }

    def queue_depth(self, queue: str) -> dict[str, Any]:
        try:
            who, policy = self._begin("queue_depth", queue)
            policy.check_read(queue)
        except _Denied as d:
            return d.response
        except PolicyError as err:
            return self._deny(who, "queue_depth", queue, err)
        depth = self.client.depth(queue)
        self._record(who, "queue_depth", "ok", queue=queue, depth=depth)
        return {"ok": True, "queue": queue, "depth": depth}

    def browse_messages(self, queue: str, max_messages: int = 5) -> dict[str, Any]:
        try:
            who, policy = self._begin("browse_messages", queue)
            policy.check_read(queue)
        except _Denied as d:
            return d.response
        except PolicyError as err:
            return self._deny(who, "browse_messages", queue, err)
        limit = policy.clamp_browse(max_messages)
        msgs = self.client.browse(queue, limit)
        self._record(who, "browse_messages", "ok", queue=queue, count=len(msgs))
        return {"ok": True, "queue": queue, "messages": [_render(m) for m in msgs]}

    def put_message(
        self, queue: str, body: str, correl_id: str | None = None, approval_id: str | None = None
    ) -> dict[str, Any]:
        data = body.encode("utf-8")
        try:
            who, policy = self._begin("put_message", queue)
            policy.check_write(queue, data)
        except _Denied as d:
            return d.response
        except PolicyError as err:
            return self._deny(who, "put_message", queue, err)
        digest = self.audit.digest(data)
        approval = None
        if policy.needs_approval(queue):
            if not approval_id:
                req = self.approvals.create(
                    queue=queue, sha256=digest, size=len(data), requested_by=who.agent_id,
                    on_behalf_of=who.user_id, ttl_seconds=policy.approval_ttl_seconds,
                    trace_id=self.current_trace_id(),
                )
                self._record(who, "put_message", "approval_required", queue=queue, bytes=len(data),
                             sha256=digest, approval_id=req["id"])
                return {
                    "ok": False,
                    "status": "approval_required",
                    "approval_id": req["id"],
                    "expires_at": req["expires_at"],
                    "message": "A person must approve this message before it is sent. "
                               "Check approval_status, then call put_message again with the same "
                               "queue, the same body and this approval_id.",
                }
            try:
                approval = self.approvals.consume(
                    approval_id, queue=queue, sha256=digest, requested_by=who.agent_id
                )
            except ApprovalError as err:
                self._record(who, "put_message", "denied", queue=queue, reason=str(err),
                             approval_id=approval_id)
                return {"ok": False, "error": str(err)}
        elif approval_id:
            return self._deny(who, "put_message", queue, PolicyError(
                f"Queue '{queue}' does not use approvals; omit approval_id."))
        trace_id = self.current_trace_id()
        to_correl = policy.trace_to_correlid and not correl_id
        msg_id = self.client.put(queue, data, correl_id, trace_id if to_correl else None)
        extra = {"approval_id": approval["id"], "approved_by": approval["decided_by"]} if approval else {}
        self._record(who, "put_message", "ok", queue=queue, bytes=len(data), sha256=digest,
                     msg_id=msg_id, correl_from_trace=to_correl, **extra)
        return {"ok": True, "queue": queue, "msg_id": msg_id, "bytes": len(data), "trace_id": trace_id, **extra}

    def approval_status(self, approval_id: str) -> dict[str, Any]:
        try:
            who, _ = self._begin("approval_status")
        except _Denied as d:
            return d.response
        req = self.approvals.get(approval_id)
        if req is None or req["requested_by"] != who.agent_id:
            self._record(who, "approval_status", "denied", approval_id=approval_id)
            return {"ok": False, "error": f"No approval request '{approval_id}' for this agent."}
        self._record(who, "approval_status", "ok", approval_id=approval_id, status=req["status"])
        fields = ("id", "queue", "status", "expires_at", "decided_by", "reason")
        return {"ok": True, **{k: req.get(k) for k in fields}}

    def get_message(self, queue: str) -> dict[str, Any]:
        try:
            who, policy = self._begin("get_message", queue)
            policy.check_destructive_get(queue)
        except _Denied as d:
            return d.response
        except PolicyError as err:
            return self._deny(who, "get_message", queue, err)
        msg = self.client.get(queue)
        if msg is None:
            self._record(who, "get_message", "empty", queue=queue)
            return {"ok": True, "queue": queue, "message": None}
        self._record(
            who, "get_message", "ok", queue=queue, bytes=len(msg.body),
            sha256=self.audit.digest(msg.body), msg_id=msg.msg_id,
        )
        return {"ok": True, "queue": queue, "message": _render(msg)}
