"""Who is acting: the agent, the user it acts for, and what each is allowed.

Every tool call is checked against two things:

* the agent's own grant: its scopes (mq.read, mq.write, mq.consume), and
* the user's entitlements, when the agent acts on someone's behalf: which
  scopes and which queues that person may use.

The agent's effective access is the intersection of the two, never the
union. On top of that the server-wide Policy still applies, so a queue must
be allowed by the policy, the agent and the user.

Over stdio the identity comes from the environment (MQ_AGENT_ID,
MQ_AGENT_SCOPES, MQ_ON_BEHALF_OF). Over streamable HTTP it comes from a
verified OAuth access token for each request (see auth.py).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .policy import Policy, PolicyError

READ, WRITE, CONSUME = "mq.read", "mq.write", "mq.consume"
ALL_SCOPES = frozenset({READ, WRITE, CONSUME})

TOOL_SCOPES = {
    "list_queues": READ,
    "queue_depth": READ,
    "browse_messages": READ,
    "approval_status": READ,
    "put_message": WRITE,
    "get_message": CONSUME,
}


def parse_scopes(value: Any) -> frozenset[str]:
    """Accept "a b c", "a,b,c" or a list; keep only scopes this server knows."""
    if value is None:
        return frozenset()
    items = value if isinstance(value, (list, tuple, set, frozenset)) else str(value).replace(",", " ").split()
    return frozenset(s.strip() for s in items if s and s.strip() in ALL_SCOPES)


@dataclass(frozen=True)
class Identity:
    agent_id: str
    user_id: str | None = None
    scopes: frozenset[str] = ALL_SCOPES
    source: str = "env"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Identity":
        env = dict(os.environ if env is None else env)
        raw = env.get("MQ_AGENT_SCOPES")
        return cls(
            agent_id=env.get("MQ_AGENT_ID") or "unknown-agent",
            user_id=env.get("MQ_ON_BEHALF_OF") or None,
            scopes=ALL_SCOPES if raw is None or raw.strip() == "" else parse_scopes(raw),
            source="env",
        )

    @classmethod
    def anonymous(cls) -> "Identity":
        return cls(agent_id="anonymous", scopes=frozenset(), source="none")


@dataclass(frozen=True)
class UserGrant:
    scopes: frozenset[str] = frozenset()
    read_queues: frozenset[str] = frozenset()
    write_queues: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Entitlements:
    """What each user may do. When configured, an agent must act for a known user."""

    users: dict[str, UserGrant] = field(default_factory=dict)
    enabled: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entitlements":
        users = {
            uid: UserGrant(
                scopes=parse_scopes(g.get("scopes", [])),
                read_queues=frozenset(g.get("read_queues", [])),
                write_queues=frozenset(g.get("write_queues", [])),
            )
            for uid, g in (data.get("users") or {}).items()
        }
        return cls(users=users, enabled=True)

    @classmethod
    def from_env(cls) -> "Entitlements":
        path = os.environ.get("MQ_ENTITLEMENTS")
        if not path:
            return cls()
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def effective(self, who: Identity, policy: Policy, tool: str) -> Policy:
        """Return the policy narrowed to this agent and user, or raise if the tool is not allowed."""
        needed = TOOL_SCOPES[tool]
        if needed not in who.scopes:
            raise PolicyError(f"Agent '{who.agent_id}' lacks scope '{needed}' for {tool}.")
        if not self.enabled:
            return policy
        if not who.user_id:
            raise PolicyError("This server requires the agent to act on behalf of a user.")
        grant = self.users.get(who.user_id)
        if grant is None:
            raise PolicyError(f"User '{who.user_id}' has no entitlements on this server.")
        if needed not in grant.scopes:
            raise PolicyError(f"User '{who.user_id}' lacks scope '{needed}' for {tool}.")
        return replace(
            policy,
            read_queues=policy.read_queues & grant.read_queues,
            write_queues=policy.write_queues & grant.write_queues,
        )
