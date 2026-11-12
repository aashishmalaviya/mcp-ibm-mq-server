"""Human approval for high-risk writes.

A put to a queue on the approval list does not happen when the agent asks.
The server records an approval request and returns its ID. A person reviews
it outside the agent (the ``mcp-ibm-mq-approvals`` CLI, or your own workflow
tool reading the same store) and approves or rejects it. The agent then
repeats the put with the approval ID, and the server checks that:

* the request was approved, by someone other than the agent that asked,
* it has not expired (expiry never turns into approval),
* the queue and the exact message body (by SHA-256) match what was approved,
* it has not been used before (single use).

The agent has no tool that can approve anything.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

PENDING, APPROVED, REJECTED, EXECUTED, EXPIRED = "pending", "approved", "rejected", "executed", "expired"


class ApprovalError(PermissionError):
    """Raised when an approval cannot be created, decided or used."""


class ApprovalStore:
    """Approval requests, in memory or in a JSON file shared with the approver CLI."""

    def __init__(self, path: str | None = None, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self._lock = threading.Lock()
        self._mem: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_env(cls) -> "ApprovalStore":
        return cls(os.environ.get("MQ_APPROVAL_STORE") or None)

    # -- persistence -------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.path is None:
            return self._mem
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        if self.path is None:
            self._mem = data
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".approvals-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def _refresh(self, req: dict[str, Any]) -> dict[str, Any]:
        if req["status"] in (PENDING, APPROVED) and self.clock() >= req["expires_at"]:
            req["status"] = EXPIRED
        return req

    # -- operations --------------------------------------------------------

    def create(self, *, queue: str, sha256: str, size: int, requested_by: str,
               on_behalf_of: str | None, ttl_seconds: int, trace_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            now = self.clock()
            req = {
                "id": "apr_" + secrets.token_hex(8),
                "action": "put_message",
                "queue": queue,
                "sha256": sha256,
                "bytes": size,
                "requested_by": requested_by,
                "on_behalf_of": on_behalf_of,
                "created_at": now,
                "expires_at": now + ttl_seconds,
                "status": PENDING,
                "trace_id": trace_id,
            }
            data[req["id"]] = req
            self._save(data)
            return dict(req)

    def get(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._load()
            req = data.get(approval_id)
            if req is None:
                return None
            before = req["status"]
            self._refresh(req)
            if req["status"] != before:
                self._save(data)
            return dict(req)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            data = self._load()
            reqs = [self._refresh(r) for r in data.values()]
            self._save(data)
        return sorted((dict(r) for r in reqs if status is None or r["status"] == status),
                      key=lambda r: r["created_at"])

    def decide(self, approval_id: str, *, approver: str, approve: bool, reason: str) -> dict[str, Any]:
        if not approver or not approver.strip():
            raise ApprovalError("An approver name is required.")
        if not reason or not reason.strip():
            raise ApprovalError("A reason is required.")
        with self._lock:
            data = self._load()
            req = data.get(approval_id)
            if req is None:
                raise ApprovalError(f"No approval request '{approval_id}'.")
            self._refresh(req)
            if req["status"] != PENDING:
                self._save(data)
                raise ApprovalError(f"Request is {req['status']}, not pending.")
            if approver.strip() in {req["requested_by"], req.get("on_behalf_of")}:
                raise ApprovalError("The requester cannot approve their own request.")
            req.update(status=APPROVED if approve else REJECTED, decided_by=approver.strip(),
                       decided_at=self.clock(), reason=reason.strip())
            self._save(data)
            return dict(req)

    def consume(self, approval_id: str, *, queue: str, sha256: str, requested_by: str) -> dict[str, Any]:
        """Check an approval for one specific put and mark it used. Raises if anything is off."""
        with self._lock:
            data = self._load()
            req = data.get(approval_id)
            if req is None:
                raise ApprovalError(f"No approval request '{approval_id}'.")
            self._refresh(req)
            if req["status"] != APPROVED:
                self._save(data)
                raise ApprovalError(f"Approval {approval_id} is {req['status']}, not approved.")
            if req["queue"] != queue:
                raise ApprovalError("Approval was granted for a different queue.")
            if req["sha256"] != sha256:
                raise ApprovalError("Message body differs from the one that was approved.")
            if req["requested_by"] != requested_by:
                raise ApprovalError("Approval belongs to a different agent.")
            req.update(status=EXECUTED, executed_at=self.clock())
            self._save(data)
            return dict(req)
