"""Tool logic, kept separate from MCP wiring so it is easy to test."""

from __future__ import annotations

from typing import Any

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
    def __init__(self, client: MQClient, policy: Policy, audit: AuditLog):
        self.client = client
        self.policy = policy
        self.audit = audit

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

    def put_message(self, queue: str, body: str, correl_id: str | None = None) -> dict[str, Any]:
        data = body.encode("utf-8")
        try:
            self.policy.check_write(queue, data)
        except PolicyError as err:
            return self._deny("put_message", queue, err)
        msg_id = self.client.put(queue, data, correl_id)
        self.audit.record(
            "put_message", "ok", queue=queue, bytes=len(data),
            sha256=self.audit.digest(data), msg_id=msg_id,
        )
        return {"ok": True, "queue": queue, "msg_id": msg_id, "bytes": len(data)}

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
