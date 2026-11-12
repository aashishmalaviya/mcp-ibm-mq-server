"""Tool logic, kept separate from MCP wiring so it is easy to test."""

from __future__ import annotations

from typing import Any

from .approvals import ApprovalError, ApprovalStore
from .audit import AuditLog
from .mq_client import Message, MQClient
from .policy import Policy, PolicyError


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


class MQService:
    def __init__(self, client: MQClient, policy: Policy, audit: AuditLog,
                 approvals: ApprovalStore | None = None):
        self.client = client
        self.policy = policy
        self.audit = audit
        self.approvals = approvals or ApprovalStore()

    def _deny(self, tool: str, queue: str | None, err: PolicyError) -> dict[str, Any]:
        self.audit.record(tool, "denied", queue=queue, reason=str(err))
        return {"ok": False, "error": str(err)}

    def list_queues(self) -> dict[str, Any]:
        self.audit.record("list_queues", "ok")
        return {
            "ok": True,
            "read": sorted(self.policy.read_queues),
            "write": [] if self.policy.read_only else sorted(self.policy.write_queues),
            "read_only": self.policy.read_only,
        }

    def queue_depth(self, queue: str) -> dict[str, Any]:
        try:
            self.policy.check_read(queue)
        except PolicyError as err:
            return self._deny("queue_depth", queue, err)
        depth = self.client.depth(queue)
        self.audit.record("queue_depth", "ok", queue=queue, depth=depth)
        return {"ok": True, "queue": queue, "depth": depth}

    def browse_messages(self, queue: str, max_messages: int = 5) -> dict[str, Any]:
        try:
            self.policy.check_read(queue)
        except PolicyError as err:
            return self._deny("browse_messages", queue, err)
        limit = self.policy.clamp_browse(max_messages)
        msgs = self.client.browse(queue, limit)
        self.audit.record("browse_messages", "ok", queue=queue, count=len(msgs))
        return {"ok": True, "queue": queue, "messages": [_render(m) for m in msgs]}

    def put_message(
        self, queue: str, body: str, correl_id: str | None = None, approval_id: str | None = None
    ) -> dict[str, Any]:
        data = body.encode("utf-8")
        try:
            self.policy.check_write(queue, data)
        except PolicyError as err:
            return self._deny("put_message", queue, err)
        digest = self.audit.digest(data)
        approval = None
        if self.policy.needs_approval(queue):
            if not approval_id:
                req = self.approvals.create(
                    queue=queue, sha256=digest, size=len(data), requested_by=self.audit.actor,
                    on_behalf_of=None, ttl_seconds=self.policy.approval_ttl_seconds,
                )
                self.audit.record("put_message", "approval_required", queue=queue, bytes=len(data),
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
                    approval_id, queue=queue, sha256=digest, requested_by=self.audit.actor
                )
            except ApprovalError as err:
                self.audit.record("put_message", "denied", queue=queue, reason=str(err),
                                  approval_id=approval_id)
                return {"ok": False, "error": str(err)}
        elif approval_id:
            return self._deny("put_message", queue, PolicyError(
                f"Queue '{queue}' does not use approvals; omit approval_id."))
        msg_id = self.client.put(queue, data, correl_id)
        extra = {"approval_id": approval["id"], "approved_by": approval["decided_by"]} if approval else {}
        self.audit.record(
            "put_message", "ok", queue=queue, bytes=len(data), sha256=digest, msg_id=msg_id, **extra,
        )
        return {"ok": True, "queue": queue, "msg_id": msg_id, "bytes": len(data), **extra}

    def approval_status(self, approval_id: str) -> dict[str, Any]:
        req = self.approvals.get(approval_id)
        if req is None or req["requested_by"] != self.audit.actor:
            self.audit.record("approval_status", "denied", approval_id=approval_id)
            return {"ok": False, "error": f"No approval request '{approval_id}' for this agent."}
        self.audit.record("approval_status", "ok", approval_id=approval_id, status=req["status"])
        fields = ("id", "queue", "status", "expires_at", "decided_by", "reason")
        return {"ok": True, **{k: req.get(k) for k in fields}}

    def get_message(self, queue: str) -> dict[str, Any]:
        try:
            self.policy.check_destructive_get(queue)
        except PolicyError as err:
            return self._deny("get_message", queue, err)
        msg = self.client.get(queue)
        if msg is None:
            self.audit.record("get_message", "empty", queue=queue)
            return {"ok": True, "queue": queue, "message": None}
        self.audit.record(
            "get_message", "ok", queue=queue, bytes=len(msg.body),
            sha256=self.audit.digest(msg.body), msg_id=msg.msg_id,
        )
        return {"ok": True, "queue": queue, "message": _render(msg)}
