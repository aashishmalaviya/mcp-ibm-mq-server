"""Audit trail: one JSON line per tool call.

Message bodies are never logged; only their size and a SHA-256 digest,
so the audit log can prove what was sent without leaking payload data.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from typing import Any, TextIO


class AuditLog:
    def __init__(self, path: str | None = None, stream: TextIO | None = None):
        self.path = path if path is not None else os.environ.get("MQ_AUDIT_LOG")
        self.stream = stream if stream is not None else sys.stderr
        self.actor = os.environ.get("MQ_AGENT_ID", "unknown-agent")

    @staticmethod
    def digest(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def record(self, tool: str, outcome: str, actor: str | None = None, **fields: Any) -> dict[str, Any]:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "audit_id": uuid.uuid4().hex,
            "actor": actor or self.actor,
            "tool": tool,
            "outcome": outcome,
            **fields,
        }
        line = json.dumps(entry, sort_keys=True)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        else:
            # stderr, never stdout: stdout carries the MCP protocol.
            print(line, file=self.stream, flush=True)
        return entry
