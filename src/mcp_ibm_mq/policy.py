"""Governance policy: which queues an agent may touch, and how.

Everything is deny-by-default. An agent can only see queues that are
explicitly allow-listed, can only write when the server is not read-only
and the queue is on the write list, and can only consume (destructive get)
when that is switched on separately.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class PolicyError(PermissionError):
    """Raised when an agent asks for something the policy does not allow."""


def _split(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(q.strip() for q in value.split(",") if q.strip())


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Policy:
    read_queues: frozenset[str] = field(default_factory=frozenset)
    write_queues: frozenset[str] = field(default_factory=frozenset)
    read_only: bool = True
    allow_destructive_get: bool = False
    max_message_bytes: int = 64 * 1024
    max_browse: int = 20

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Policy":
        env = dict(os.environ if env is None else env)
        return cls(
            read_queues=_split(env.get("MQ_READ_QUEUES")),
            write_queues=_split(env.get("MQ_WRITE_QUEUES")),
            read_only=_flag(env.get("MQ_READ_ONLY"), True),
            allow_destructive_get=_flag(env.get("MQ_ALLOW_DESTRUCTIVE_GET"), False),
            max_message_bytes=int(env.get("MQ_MAX_MESSAGE_BYTES") or 64 * 1024),
            max_browse=int(env.get("MQ_MAX_BROWSE") or 20),
        )

    # -- checks -----------------------------------------------------------

    def visible_queues(self) -> list[str]:
        return sorted(self.read_queues | self.write_queues)

    def check_read(self, queue: str) -> None:
        if queue not in self.read_queues:
            raise PolicyError(f"Queue '{queue}' is not on the read allow-list.")

    def check_write(self, queue: str, body: bytes) -> None:
        if self.read_only:
            raise PolicyError("Server is read-only (MQ_READ_ONLY=true); writes are disabled.")
        if queue not in self.write_queues:
            raise PolicyError(f"Queue '{queue}' is not on the write allow-list.")
        if len(body) > self.max_message_bytes:
            raise PolicyError(
                f"Message is {len(body)} bytes; limit is {self.max_message_bytes}."
            )

    def check_destructive_get(self, queue: str) -> None:
        self.check_read(queue)
        if self.read_only or not self.allow_destructive_get:
            raise PolicyError(
                "Destructive get is disabled; use browse_messages, or set "
                "MQ_READ_ONLY=false and MQ_ALLOW_DESTRUCTIVE_GET=true."
            )

    def clamp_browse(self, requested: int) -> int:
        return max(1, min(int(requested), self.max_browse))
